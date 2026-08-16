"""招标要求接口（4.3.2，含用户确认/修正闭环 Issue #6 P0）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_capability, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.requirement import Requirement
from app.services import requirement_service
from app.services.audit import write_audit

router = APIRouter(prefix="/requirements", tags=["requirements"])
projects_router = APIRouter(prefix="/projects", tags=["requirements"])


class ConfirmRequirementRequest(BaseModel):
    expected_revision: int
    confirmed: bool = True


class CorrectRequirementRequest(BaseModel):
    expected_revision: int
    content: str
    coordinates: list | None = None
    confidence: float | None = None
    structured: dict | None = None


def _to_dict(req: Requirement) -> dict:
    return {
        "req_id": req.id,
        "req_type": req.req_type,
        "req_key": req.req_key,
        "content": req.content,
        "structured": req.structured,
        "coordinates": req.coordinates,
        "confidence": float(req.confidence) if req.confidence is not None else None,
        "revision": req.revision,
        "supersedes": req.supersedes,
        "source_file_id": req.source_file_id,
        "confirm_status": req.confirm_status,
        "confirmed_at": req.confirmed_at.isoformat() if req.confirmed_at else None,
    }


@router.get("")
async def list_requirements(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("list_requirements")),
) -> list[dict]:
    rows = await requirement_service.list_requirements(session, user.enterprise_id, project_id)
    return [_to_dict(r) for r in rows]


@router.get("/{req_id}")
async def get_requirement(
    req_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_requirement")),
) -> dict:
    req = await session.scalar(
        select(Requirement).where(
            Requirement.id == req_id,
            Requirement.enterprise_id == user.enterprise_id,
        )
    )
    if req is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="要求不存在")
    return _to_dict(req)


@projects_router.post("/{project_id}/requirements/upsert", status_code=status.HTTP_201_CREATED)
async def upsert_requirements(
    project_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("upsert_requirements")),
) -> dict:
    created: list[int] = []
    for item in body.get("requirements", []):
        if not item.get("coordinates"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="coordinates 为空视为失败（P1 坐标必填）",
            )
        req = await requirement_service.upsert_requirement(
            session,
            enterprise_id=user.enterprise_id,
            project_id=project_id,
            req_type=item["req_type"],
            req_key=item.get("req_key"),
            content=item["content"],
            structured=item.get("structured"),
            coordinates=item.get("coordinates"),
            confidence=item.get("confidence"),
            source_file_id=item.get("source_file_id"),
        )
        created.append(req.id)
    await session.commit()
    return {"created": created, "count": len(created)}


@projects_router.put("/{project_id}/requirements/{req_id}/confirm")
async def confirm_requirement(
    project_id: int,
    req_id: int,
    body: ConfirmRequirementRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    """用户确认/拒绝单条要求（Issue #6 P0）：expected_revision CAS，冲突 409，落审计。"""
    try:
        req = await requirement_service.confirm_requirement(
            session,
            enterprise_id=user.enterprise_id,
            project_id=project_id,
            req_id=req_id,
            expected_revision=body.expected_revision,
            confirmed=body.confirmed,
        )
    except requirement_service.RevisionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project_id,
        action="requirement.confirm",
        object_type="requirement",
        object_id=req.id,
        payload={"confirmed": body.confirmed, "revision": req.revision},
    )
    await session.commit()
    return _to_dict(req)


@projects_router.put("/{project_id}/requirements/{req_id}/correct")
async def correct_requirement(
    project_id: int,
    req_id: int,
    body: CorrectRequirementRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    """用户修正单条要求（Issue #6 P0）：supersede 生成新 revision，CAS 保护，落审计。"""
    try:
        req = await requirement_service.correct_requirement(
            session,
            enterprise_id=user.enterprise_id,
            project_id=project_id,
            req_id=req_id,
            expected_revision=body.expected_revision,
            content=body.content,
            coordinates=body.coordinates,
            confidence=body.confidence,
            structured=body.structured,
        )
    except requirement_service.RevisionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project_id,
        action="requirement.correct",
        object_type="requirement",
        object_id=req.id,
        payload={"supersedes": req.supersedes, "revision": req.revision},
    )
    await session.commit()
    return _to_dict(req)
