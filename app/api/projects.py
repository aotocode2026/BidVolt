"""项目 CRUD 与状态机（4.1.6）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission, UserContext
from app.constants import Permission, PROJECT_TRANSITIONS, ProjectStatus
from app.db import get_session
from app.models.project import Project
from app.schemas.project import (
    Page,
    ProjectCreate,
    ProjectResponse,
    ProjectStatusUpdate,
    ProjectUpdate,
)
from app.services.audit import write_audit

router = APIRouter(prefix="/projects", tags=["projects"])


def _to_response(p: Project) -> ProjectResponse:
    return ProjectResponse(
        project_id=p.id,
        name=p.name,
        tender_no=p.tender_no,
        deadline=p.deadline,
        status=p.status,
        note=p.note,
        updated_at=p.updated_at,
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
        deadline=body.deadline,
        note=body.note,
        status=int(ProjectStatus.DRAFT),
    )
    session.add(project)
    await session.flush()
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
    await session.refresh(project)
    return _to_response(project)


@router.get("", response_model=Page)
async def list_projects(
    page: int = 1,
    size: int = 20,
    status_filter: int | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> Page:
    query = select(Project).where(
        Project.enterprise_id == user.enterprise_id, Project.is_deleted.is_(False)
    )
    if status_filter is not None:
        query = query.where(Project.status == status_filter)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(Project.updated_at.desc()).offset((page - 1) * size).limit(size))
    return Page(items=[_to_response(p) for p in rows], total=total or 0, page=page, size=size)


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
    return _to_response(project)


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
    await session.commit()
    await session.refresh(project)
    return _to_response(project)


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
    await session.commit()
    await session.refresh(project)
    return _to_response(project)
