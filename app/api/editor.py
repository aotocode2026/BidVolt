"""在线编辑会话（Issue #2 #41-#43）：租约 + 服务端检查点 + 完成生成新版本。"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.deliverable import Deliverable
from app.models.editor import EditorSession
from app.services import deliverable_service
from app.services.audit import write_audit
from app.services.deliverable_service import VersionConflict

router = APIRouter(prefix="/deliverables", tags=["editor"])

LEASE_MINUTES = 30


async def _owned_deliverable(
    session: AsyncSession,
    user: UserContext,
    deliverable_id: int,
) -> Deliverable:
    d = await session.scalar(
        select(Deliverable).where(
            Deliverable.id == deliverable_id,
            Deliverable.enterprise_id == user.enterprise_id,
        )
    )
    if d is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成果不存在")
    return d


async def _active_session(
    session: AsyncSession,
    user: UserContext,
    deliverable_id: int,
    session_id: int,
    lease_token: str | None = None,
) -> EditorSession:
    row = await session.scalar(
        select(EditorSession).where(
            EditorSession.id == session_id,
            EditorSession.enterprise_id == user.enterprise_id,
            EditorSession.deliverable_id == deliverable_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="编辑会话不存在")
    if lease_token is not None and not secrets.compare_digest(row.lease_token, lease_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话租约无效")
    if row.status != 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="会话已结束")
    if row.lease_expires_at:
        expires_at = row.lease_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < datetime.now(UTC):
            row.status = 4
            await session.commit()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="会话租约已过期")
    return row


@router.post("/{deliverable_id}/editor-sessions", status_code=status.HTTP_201_CREATED)
async def create_editor_session(
    deliverable_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    d = await _owned_deliverable(session, user, deliverable_id)
    existing = await session.scalar(
        select(EditorSession).where(
            EditorSession.deliverable_id == deliverable_id,
            EditorSession.enterprise_id == user.enterprise_id,
            EditorSession.status == 1,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该成果已有进行中的编辑会话")
    _, content = await deliverable_service.get_version_content(session, d.id, d.current_version_no)
    now = datetime.now(UTC)
    row = EditorSession(
        enterprise_id=user.enterprise_id,
        project_id=d.project_id,
        deliverable_id=d.id,
        base_version_no=d.current_version_no,
        status=1,
        lease_token=secrets.token_urlsafe(32),
        lease_expires_at=now + timedelta(minutes=LEASE_MINUTES),
        last_activity_at=now,
        created_by=user.user_id,
    )
    session.add(row)
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=d.project_id,
        action="editor.session.create",
        object_type="editor_session",
        object_id=row.id,
    )
    await session.commit()
    return {
        "session_id": row.id,
        "deliverable_id": d.id,
        "base_version_no": row.base_version_no,
        "lease_token": row.lease_token,
        "lease_expires_at": row.lease_expires_at.isoformat(),
        "content": content,
    }


@router.get("/{deliverable_id}/editor-sessions")
async def list_editor_sessions(
    deliverable_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    await _owned_deliverable(session, user, deliverable_id)
    rows = (
        await session.scalars(
            select(EditorSession)
            .where(
                EditorSession.deliverable_id == deliverable_id,
                EditorSession.enterprise_id == user.enterprise_id,
            )
            .order_by(EditorSession.id.desc())
            .limit(50)
        )
    ).all()
    return {
        "items": [
            {
                "session_id": r.id,
                "status": r.status,
                "base_version_no": r.base_version_no,
                "completed_version_no": r.completed_version_no,
                "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/{deliverable_id}/editor-sessions/{session_id}")
async def get_editor_session(
    deliverable_id: int,
    session_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    await _owned_deliverable(session, user, deliverable_id)
    row = await session.scalar(
        select(EditorSession).where(
            EditorSession.id == session_id,
            EditorSession.enterprise_id == user.enterprise_id,
            EditorSession.deliverable_id == deliverable_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="编辑会话不存在")
    return {
        "session_id": row.id,
        "status": row.status,
        "base_version_no": row.base_version_no,
        "completed_version_no": row.completed_version_no,
        "checkpoint": row.checkpoint,
        "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else None,
    }


@router.put("/{deliverable_id}/editor-sessions/{session_id}/checkpoint")
async def save_checkpoint(
    deliverable_id: int,
    session_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    await _owned_deliverable(session, user, deliverable_id)
    row = await _active_session(
        session, user, deliverable_id, session_id, lease_token=body.get("lease_token")
    )
    content = body.get("content")
    if not isinstance(content, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="content 必须是对象")
    now = datetime.now(UTC)
    row.checkpoint = content
    row.last_activity_at = now
    row.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
    await session.commit()
    return {"session_id": row.id, "checkpoint_saved": True, "lease_expires_at": row.lease_expires_at.isoformat()}


@router.post("/{deliverable_id}/editor-sessions/{session_id}/complete", status_code=status.HTTP_201_CREATED)
async def complete_editor_session(
    deliverable_id: int,
    session_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    d = await _owned_deliverable(session, user, deliverable_id)
    row = await _active_session(
        session, user, deliverable_id, session_id, lease_token=body.get("lease_token")
    )
    content = body.get("content")
    if not isinstance(content, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="content 必须是对象")
    try:
        version = await deliverable_service.save_version(
            session,
            d,
            content,
            version_type=4,
            created_by=user.user_id,
            expected_version_no=body.get("expected_version_no"),
            idempotency_key=body.get("idempotency_key"),
        )
    except VersionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    row.status = 2
    row.completed_version_no = version.version_no
    row.checkpoint = content
    row.last_activity_at = datetime.now(UTC)
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=d.project_id,
        action="editor.session.complete",
        object_type="editor_session",
        object_id=row.id,
        payload={"version_no": version.version_no},
    )
    await session.commit()
    return {"session_id": row.id, "version_no": version.version_no, "status": row.status}


@router.post("/{deliverable_id}/editor-sessions/{session_id}/cancel")
async def cancel_editor_session(
    deliverable_id: int,
    session_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.DELIVERABLE_EDIT)),
) -> dict:
    await _owned_deliverable(session, user, deliverable_id)
    row = await _active_session(
        session, user, deliverable_id, session_id, lease_token=body.get("lease_token")
    )
    row.status = 3
    await session.commit()
    return {"session_id": row.id, "status": row.status}
