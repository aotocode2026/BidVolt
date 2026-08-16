"""招标公告 URL 导入接口（Issue #6 P0）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.tender_notice import TenderNotice
from app.services.audit import write_audit
from app.services.tender_service import import_tender_notice

router = APIRouter(prefix="/projects", tags=["tender-notices"])


class ImportNoticeRequest(BaseModel):
    url: str


def _to_dict(n: TenderNotice) -> dict:
    return {
        "tender_notice_id": n.id,
        "project_id": n.project_id,
        "source_url": n.source_url,
        "title": n.title,
        "status": n.status,  # 1 导入中 2 已导入 3 失败
        "file_id": n.file_id,
        "error_code": n.error_code,
        "error_message": n.error_message,
        "imported_at": n.imported_at.isoformat() if n.imported_at else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.post("/{project_id}/tender-notices/import-url", status_code=status.HTTP_201_CREATED)
async def import_notice_url(
    project_id: int,
    body: ImportNoticeRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    """安全导入招标公告 URL：正文仅进入本项目材料（document_role=招标公告），绝不写企业资料库。"""
    if not body.url.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="url 不能为空")
    try:
        notice = await import_tender_notice(
            session, user=user, project_id=project_id, url=body.url.strip()
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project_id,
        action="tender_notice.import",
        object_type="tender_notice",
        object_id=notice.id,
        payload={"url": body.url.strip(), "status": notice.status},
    )
    await session.commit()
    result = _to_dict(notice)
    if notice.status == 3:
        result["_error"] = True
    return result


@router.get("/{project_id}/tender-notices")
async def list_notices(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    rows = (
        await session.scalars(
            select(TenderNotice)
            .where(
                TenderNotice.enterprise_id == user.enterprise_id,
                TenderNotice.project_id == project_id,
            )
            .order_by(TenderNotice.id.desc())
            .limit(100)
        )
    ).all()
    return {"items": [_to_dict(n) for n in rows]}


@router.get("/{project_id}/tender-notices/{notice_id}")
async def notice_detail(
    project_id: int,
    notice_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    notice = await session.scalar(
        select(TenderNotice).where(
            TenderNotice.id == notice_id,
            TenderNotice.enterprise_id == user.enterprise_id,
            TenderNotice.project_id == project_id,
        )
    )
    if notice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="导入记录不存在")
    return _to_dict(notice)
