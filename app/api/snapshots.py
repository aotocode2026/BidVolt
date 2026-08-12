"""项目快照（Issue #2 #13/#14）：列表 + 详情 + 不可变 manifest。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission, UserContext
from app.config import settings
from app.constants import Permission
from app.db import get_session
from app.models.deliverable import Deliverable, DeliverableContent, DeliverableVersion
from app.models.file import FileObject
from app.models.project_material import ProjectMaterial, ProjectSnapshot
from app.models.requirement import Requirement
from app.services.review_service import RULESET_VERSION

router = APIRouter(tags=["snapshots"])


async def _owned_snapshot(
    session: AsyncSession,
    enterprise_id: int,
    project_id: int,
    snapshot_id: int,
) -> ProjectSnapshot:
    row = await session.scalar(
        select(ProjectSnapshot).where(
            ProjectSnapshot.id == snapshot_id,
            ProjectSnapshot.enterprise_id == enterprise_id,
            ProjectSnapshot.project_id == project_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="快照不存在")
    return row


async def _build_manifest(
    session: AsyncSession,
    enterprise_id: int,
    project_id: int,
) -> dict:
    """按当前冻结输入组装不可变 manifest（材料哈希/要求版本/成果版本/规则与模型版本）。"""
    materials = (
        await session.scalars(
            select(ProjectMaterial).where(
                ProjectMaterial.enterprise_id == enterprise_id,
                ProjectMaterial.project_id == project_id,
            )
        )
    ).all()
    files: dict[int, FileObject] = {}
    file_ids = [m.file_id for m in materials]
    if file_ids:
        rows = await session.scalars(select(FileObject).where(FileObject.id.in_(file_ids)))
        files = {f.id: f for f in rows}
    materials_list = [
        {
            "material_id": m.id,
            "file_id": m.file_id,
            "name": files[m.file_id].original_name if m.file_id in files else None,
            "sha256": files[m.file_id].sha256 if m.file_id in files else None,
            "size_bytes": files[m.file_id].size_bytes if m.file_id in files else None,
            "status": m.status,
        }
        for m in materials
    ]

    reqs = (
        await session.scalars(
            select(Requirement).where(
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
            )
        )
    ).all()
    requirements_list = [
        {
            "req_id": r.id,
            "req_type": r.req_type,
            "content": r.content,
            "revision": r.revision,
            "source_file_id": r.source_file_id,
            "confidence": float(r.confidence) if r.confidence is not None else None,
        }
        for r in reqs
    ]

    dels = (
        await session.scalars(
            select(Deliverable).where(
                Deliverable.enterprise_id == enterprise_id,
                Deliverable.project_id == project_id,
            )
        )
    ).all()
    latest: dict[int, DeliverableVersion] = {}
    if dels:
        dv_rows = await session.scalars(
            select(DeliverableVersion)
            .where(DeliverableVersion.deliverable_id.in_([d.id for d in dels]))
            .order_by(DeliverableVersion.version_no.desc())
        )
        for v in dv_rows:
            latest.setdefault(v.deliverable_id, v)
    contents: dict[int, DeliverableContent] = {}
    content_ids = [v.content_id for v in latest.values()]
    if content_ids:
        cc = await session.scalars(
            select(DeliverableContent).where(DeliverableContent.id.in_(content_ids))
        )
        contents = {c.id: c for c in cc}
    deliverables_list = [
        {
            "deliverable_id": d.id,
            "deliverable_type": d.deliverable_type,
            "title": d.title,
            "current_version_no": d.current_version_no,
            "version_content_hash": (
                contents[latest[d.id].content_id].content_hash
                if d.id in latest and latest[d.id].content_id in contents
                else None
            ),
        }
        for d in dels
    ]

    return {
        "project_id": project_id,
        "materials": materials_list,
        "requirements": requirements_list,
        "deliverables": deliverables_list,
        "rules": {
            "ruleset": RULESET_VERSION,
            "review_model": settings.minimax_model,
            "vision_model": settings.dashscope_vl_model,
        },
        "built_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/projects/{project_id}/snapshots")
async def list_snapshots(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.SCORE_VIEW)),
) -> dict:
    rows = (
        await session.scalars(
            select(ProjectSnapshot)
            .where(
                ProjectSnapshot.enterprise_id == user.enterprise_id,
                ProjectSnapshot.project_id == project_id,
            )
            .order_by(ProjectSnapshot.id.desc())
            .limit(100)
        )
    ).all()
    return {
        "items": [
            {
                "snapshot_id": s.id,
                "snapshot_type": s.snapshot_type,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "input_refs": s.input_refs or {},
                "rules_version": s.rules_version or {},
            }
            for s in rows
        ]
    }


@router.get("/projects/{project_id}/snapshots/{snapshot_id}")
async def snapshot_detail(
    project_id: int,
    snapshot_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.SCORE_VIEW)),
) -> dict:
    snap = await _owned_snapshot(session, user.enterprise_id, project_id, snapshot_id)
    manifest = snap.manifest or await _build_manifest(session, user.enterprise_id, project_id)
    return {
        "snapshot_id": snap.id,
        "snapshot_type": snap.snapshot_type,
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
        "input_refs": snap.input_refs or {},
        "external_samples": snap.external_samples or {},
        "rules_version": snap.rules_version or {},
        "manifest": manifest,
    }
