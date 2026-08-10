"""招标要求接口（4.3.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission, UserContext
from app.constants import Permission
from app.db import get_session
from app.models.requirement import Requirement
from app.services import requirement_service

router = APIRouter(prefix="/requirements", tags=["requirements"])
projects_router = APIRouter(prefix="/projects", tags=["requirements"])


def _to_dict(req: Requirement) -> dict:
    return {
        "req_id": req.id,
        "req_type": req.req_type,
        "content": req.content,
        "structured": req.structured,
        "coordinates": req.coordinates,
        "confidence": float(req.confidence) if req.confidence is not None else None,
        "revision": req.revision,
        "source_file_id": req.source_file_id,
    }


@router.get("")
async def list_requirements(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> list[dict]:
    rows = await requirement_service.list_requirements(session, user.enterprise_id, project_id)
    return [_to_dict(r) for r in rows]


@router.get("/{req_id}")
async def get_requirement(
    req_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
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
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
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
            content=item["content"],
            structured=item.get("structured"),
            coordinates=item.get("coordinates"),
            confidence=item.get("confidence"),
            source_file_id=item.get("source_file_id"),
        )
        created.append(req.id)
    await session.commit()
    return {"created": created, "count": len(created)}
