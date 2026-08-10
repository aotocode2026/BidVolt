"""审计写入（4.11.3）。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


async def write_audit(
    session: AsyncSession,
    *,
    enterprise_id: int,
    action: str,
    user_id: int | None = None,
    project_id: int | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
    payload: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            enterprise_id=enterprise_id,
            project_id=project_id,
            user_id=user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            payload=payload,
        )
    )
