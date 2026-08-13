"""项目编辑锁（4.1）。"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.auth import ProjectEditLock
from app.models.project import Project
from app.services.security import ensure_aware

router = APIRouter(prefix="/projects/{project_id}/edit-lock", tags=["locks"])

LOCK_TTL = timedelta(minutes=2)
HEARTBEAT_TTL = timedelta(minutes=2)


async def _owned_project(session: AsyncSession, project_id: int, enterprise_id: int) -> None:
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.enterprise_id == enterprise_id,
        )
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")


@router.post("")
async def acquire_lock(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    await _owned_project(session, project_id, user.enterprise_id)
    lock = await session.get(ProjectEditLock, project_id)
    now = datetime.now(UTC)
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
    await _owned_project(session, project_id, user.enterprise_id)
    lock = await session.get(ProjectEditLock, project_id)
    if lock is None or lock.holder_user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="未持有编辑锁")
    lock.expires_at = datetime.now(UTC) + HEARTBEAT_TTL
    await session.commit()
    return {"expires_at": lock.expires_at.isoformat()}


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def release_lock(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> None:
    await _owned_project(session, project_id, user.enterprise_id)
    lock = await session.get(ProjectEditLock, project_id)
    if lock is not None and lock.holder_user_id == user.user_id:
        await session.execute(delete(ProjectEditLock).where(ProjectEditLock.project_id == project_id))
        await session.commit()
