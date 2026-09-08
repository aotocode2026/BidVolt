"""招标公告 URL 导入接口（Issue #6 P0 + Issue #32 逐附件下载）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.file import UploadBatchItem
from app.models.tender_notice import TenderNotice
from app.services.audit import write_audit
from app.services.tender_service import TenderImportError, import_tender_notice

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
        "import_batch_id": n.import_batch_id,
        "error_code": n.error_code,
        "error_message": n.error_message,
        "imported_at": n.imported_at.isoformat() if n.imported_at else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


def _attachment_item(i: UploadBatchItem) -> dict:
    return {
        "item_id": i.id,
        "filename": i.filename,
        "file_id": i.file_id,
        "status": i.status,  # accepted/duplicate/expanded/error/skipped
        "message": i.message,
        "document_role": i.document_role,
        "source_url": i.source_url,
        "source_archive_file_id": i.source_archive_file_id,
        "archive_path": i.archive_path,
        "parse_status": i.parse_status,
    }


@router.post("/{project_id}/tender-notices/import-url", status_code=status.HTTP_201_CREATED)
async def import_notice_url(
    project_id: int,
    body: ImportNoticeRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    """安全导入招标公告 URL：秒回创建导入任务，正文与附件由后台逐文件下载入库。"""
    if not body.url.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="url 不能为空")
    try:
        notice = await import_tender_notice(
            session, user=user, project_id=project_id, url=body.url.strip()
        )
    except TenderImportError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc
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
        payload={
            "url": body.url.strip(),
            "status": notice.status,
            "import_batch_id": notice.import_batch_id,
        },
    )
    await session.commit()
    result = _to_dict(notice)
    result["batch_id"] = notice.import_batch_id
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
    result = _to_dict(notice)
    rows = (
        await session.scalars(
            select(UploadBatchItem)
            .where(UploadBatchItem.notice_id == notice.id)
            .order_by(UploadBatchItem.id)
        )
    ).all()
    result["attachments"] = [_attachment_item(i) for i in rows]
    return result
