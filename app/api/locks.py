"""项目编辑锁（4.1）。"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission, UserContext
from app.constants import Permission
from app.db import get_session
from app.models.auth import ProjectEditLock
from app.services.security import ensure_aware

router = APIRouter(prefix="/projects/{project_id}/edit-lock", tags=["locks"])

LOCK_TTL = timedelta(minutes=2)
HEARTBEAT_TTL = timedelta(minutes=2)


@router.post("")
async def acquire_lock(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    lock = await session.get(ProjectEditLock, project_id)
    now = datetime.now(timezone.utc)
    if lock is not None and ensure_aware(lock.expires_at) > now:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"holder": lock.holder_user_id, "expires_at": lock.expires_at.isoformat()},
        )
    token = secrets.token_urlsafe(32)
    if lock is None:
        session.add(
            ProjectEditLock(
                project_id=project_id,
                lock_token=token,
                holder_user_id=user.user_id,
                expires_at=now + LOCK_TTL,
            )
        )
    else:
        lock.lock_token = token
        lock.holder_user_id = user.user_id
        lock.expires_at = now + LOCK_TTL
    await session.commit()
    return {"lock_id": token, "expires_at": (now + LOCK_TTL).isoformat()}


@router.put("/heartbeat")
async def heartbeat(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    lock = await session.get(ProjectEditLock, project_id)
    if lock is None or lock.holder_user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="未持有编辑锁")
    lock.expires_at = datetime.now(timezone.utc) + HEARTBEAT_TTL
    await session.commit()
    return {"expires_at": lock.expires_at.isoformat()}


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def release_lock(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> None:
    lock = await session.get(ProjectEditLock, project_id)
    if lock is not None and lock.holder_user_id == user.user_id:
        await session.execute(delete(ProjectEditLock).where(ProjectEditLock.project_id == project_id))
        await session.commit()
