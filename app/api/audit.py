"""审计查看接口（4.11.3）：按租户只读查询，需 audit.view 权限。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.audit import AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/logs")
async def list_audit_logs(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.AUDIT_VIEW)),
    project_id: int | None = Query(None),
    action: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> dict:
    query = select(AuditLog).where(AuditLog.enterprise_id == user.enterprise_id)
    count_query = select(func.count()).select_from(AuditLog).where(AuditLog.enterprise_id == user.enterprise_id)
    if project_id is not None:
        query = query.where(AuditLog.project_id == project_id)
        count_query = count_query.where(AuditLog.project_id == project_id)
    if action:
        query = query.where(AuditLog.action == action)
        count_query = count_query.where(AuditLog.action == action)
    total = await session.scalar(count_query)
    rows = await session.scalars(query.order_by(AuditLog.id.desc()).offset((page - 1) * size).limit(size))
    return {
        "items": [
            {
                "id": r.id,
                "action": r.action,
                "project_id": r.project_id,
                "user_id": r.user_id,
                "object_type": r.object_type,
                "object_id": r.object_id,
                "payload": r.payload,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": total or 0,
        "page": page,
        "size": size,
    }
