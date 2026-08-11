"""资料匹配接口（4.2.6 material_match_result）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission, UserContext
from app.constants import Permission
from app.db import get_session
from app.models.project_material import MaterialMatchResult

router = APIRouter(prefix="/projects", tags=["matches"])


@router.get("/{project_id}/material-matches")
async def list_matches(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> list[dict]:
    rows = await session.scalars(
        select(MaterialMatchResult).where(
            MaterialMatchResult.enterprise_id == user.enterprise_id,
            MaterialMatchResult.project_id == project_id,
        )
    )
    return [
        {
            "result_id": r.id,
            "requirement_id": r.requirement_id,
            "asset_id": r.asset_id,
            "matched": r.matched,
            "gap_desc": r.gap_desc,
            "affected_score_item": r.affected_score_item,
            "suggestion": r.suggestion,
        }
        for r in rows
    ]


@router.post("/{project_id}/material-matches", status_code=201)
async def save_matches(
    project_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    saved: list[int] = []
    for item in body.get("results", []):
        row = MaterialMatchResult(
            enterprise_id=user.enterprise_id,
            project_id=project_id,
            requirement_id=item.get("requirement_id"),
            asset_id=item.get("asset_id"),
            matched=item["matched"],
            gap_desc=item.get("gap_desc"),
            affected_score_item=item.get("affected_score_item"),
            impact_score=item.get("impact_score"),
            suggestion=item.get("suggestion"),
            source_task_id=item.get("source_task_id"),
        )
        session.add(row)
        await session.flush()
        saved.append(row.id)
    await session.commit()
    return {"saved": saved, "count": len(saved)}
