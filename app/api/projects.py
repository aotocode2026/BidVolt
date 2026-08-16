"""项目 CRUD 与状态机（4.1.6，含 Issue #6 P1：buyer/q 搜索/可解释摘要）。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, get_current_user, require_permission
from app.constants import PROJECT_TRANSITIONS, Permission, ProjectStatus
from app.db import get_session
from app.models.deliverable import Deliverable
from app.models.file import FileObject
from app.models.project import Project
from app.models.review import ReviewRun, ScoreRecord
from app.schemas.project import (
    Page,
    ProjectCreate,
    ProjectResponse,
    ProjectStatusUpdate,
    ProjectUpdate,
)
from app.services.audit import write_audit

router = APIRouter(prefix="/projects", tags=["projects"])


async def _summaries(session: AsyncSession, project_ids: list[int]) -> dict[int, dict]:
    """批量可解释摘要（避免 N+1）：材料数 / 成果数 / 评审数 / 最新评分与缺失项。"""
    out: dict[int, dict] = {pid: {
        "material_count": 0,
        "deliverable_count": 0,
        "review_run_count": 0,
        "latest_total_score": None,
        "missing_count": None,
        "risk_level": None,
    } for pid in project_ids}
    if not project_ids:
        return out
    for pid, count in (
        await session.execute(
            select(FileObject.project_id, func.count())
            .where(FileObject.project_id.in_(project_ids), FileObject.is_deleted.is_(False))
            .group_by(FileObject.project_id)
        )
    ).all():
        out[pid]["material_count"] = count
    for pid, count in (
        await session.execute(
            select(Deliverable.project_id, func.count())
            .where(Deliverable.project_id.in_(project_ids))
            .group_by(Deliverable.project_id)
        )
    ).all():
        out[pid]["deliverable_count"] = count
    for pid, count in (
        await session.execute(
            select(ReviewRun.project_id, func.count())
            .where(ReviewRun.project_id.in_(project_ids))
            .group_by(ReviewRun.project_id)
        )
    ).all():
        out[pid]["review_run_count"] = count
    latest_ids = (
        select(func.max(ScoreRecord.id))
        .where(ScoreRecord.project_id.in_(project_ids))
        .group_by(ScoreRecord.project_id)
    ).scalar_subquery()
    for score in (
        await session.scalars(
            select(ScoreRecord).where(ScoreRecord.id.in_(select(latest_ids)))
        )
    ).all():
        summary = out[score.project_id]
        summary["latest_total_score"] = float(score.total_score) if score.total_score is not None else None
        summary["missing_count"] = score.missing_count
        summary["risk_level"] = score.missing_count if score.missing_count else 0
    return out


def _to_response(p: Project, summary: dict | None = None) -> ProjectResponse:
    return ProjectResponse(
        project_id=p.id,
        name=p.name,
        tender_no=p.tender_no,
        buyer=p.buyer,
        deadline=p.deadline,
        status=p.status,
        note=p.note,
        updated_at=p.updated_at,
        summary=summary,
    )


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> ProjectResponse:
    project = Project(
        enterprise_id=user.enterprise_id,
        name=body.name,
        tender_no=body.tender_no,
        buyer=body.buyer,
        deadline=body.deadline,
        note=body.note,
        status=int(ProjectStatus.DRAFT),
    )
    session.add(project)
    await session.flush()
    project.updated_at = datetime.now(timezone.utc)
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project.id,
        action="project.create",
        object_type="project",
        object_id=project.id,
    )
    await session.commit()
    return _to_response(project)


@router.get("", response_model=Page)
async def list_projects(
    page: int = 1,
    size: int = 20,
    status_filter: int | None = None,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> Page:
    query = select(Project).where(
        Project.enterprise_id == user.enterprise_id, Project.is_deleted.is_(False)
    )
    if status_filter is not None:
        query = query.where(Project.status == status_filter)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                Project.name.ilike(like),
                Project.tender_no.ilike(like),
                Project.buyer.ilike(like),
            )
        )
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(Project.updated_at.desc()).offset((page - 1) * size).limit(size))
    projects = list(rows)
    summaries = await _summaries(session, [p.id for p in projects])
    return Page(
        items=[_to_response(p, summaries.get(p.id)) for p in projects],
        total=total or 0,
        page=page,
        size=size,
    )


async def _get_owned_project(session: AsyncSession, enterprise_id: int, project_id: int) -> Project:
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.enterprise_id == enterprise_id,
        )
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


def _ensure_not_archived(project: Project) -> None:
    if project.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="归档项目只读")


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> ProjectResponse:
    project = await _get_owned_project(session, user.enterprise_id, project_id)
    summary = (await _summaries(session, [project.id])).get(project.id)
    return _to_response(project, summary)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    body: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> ProjectResponse:
    project = await _get_owned_project(session, user.enterprise_id, project_id)
    _ensure_not_archived(project)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project.id,
        action="project.update",
        object_type="project",
        object_id=project.id,
    )
    project.updated_at = datetime.now(timezone.utc)
    await session.commit()
    summary = (await _summaries(session, [project.id])).get(project.id)
    return _to_response(project, summary)


@router.post("/{project_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_project(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> None:
    project = await _get_owned_project(session, user.enterprise_id, project_id)
    if project.is_deleted:
        return  # 幂等：已归档
    project.is_deleted = True
    project.status = int(ProjectStatus.ARCHIVED)
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project.id,
        action="project.archive",
        object_type="project",
        object_id=project.id,
    )
    await session.commit()


@router.put("/{project_id}/status", response_model=ProjectResponse)
async def update_status(
    project_id: int,
    body: ProjectStatusUpdate,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> ProjectResponse:
    project = await _get_owned_project(session, user.enterprise_id, project_id)
    _ensure_not_archived(project)
    try:
        target = ProjectStatus(body.status)
        current = ProjectStatus(project.status)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="非法状态值") from exc
    if target not in PROJECT_TRANSITIONS.get(current, set()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"非法状态流转：{current.name} → {target.name}")
    project.status = target.value
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project.id,
        action="project.status_change",
        object_type="project",
        object_id=project.id,
        payload={"from": current.value, "to": target.value},
    )
    project.updated_at = datetime.now(timezone.utc)
    await session.commit()
    summary = (await _summaries(session, [project.id])).get(project.id)
    return _to_response(project, summary)
