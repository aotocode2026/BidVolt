"""投标行情内容库接口（Issue #34）：导入/上传/列表/详情/提炼状态/重试/删除。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_capability, require_permission
from app.config import settings
from app.constants import Permission
from app.db import get_session
from app.models.file import FileObject
from app.models.market_knowledge import (
    MarketKnowledgeArticle,
    MarketKnowledgeImage,
    MarketKnowledgePoint,
)
from app.services import market_knowledge
from app.services.audit import write_audit
from app.services.quota_service import QuotaExceeded

router = APIRouter(prefix="/market-knowledge", tags=["market-knowledge"])


class ImportUrlRequest(BaseModel):
    url: str


def _article_dict(a: MarketKnowledgeArticle) -> dict:
    return {
        "article_id": a.id,
        "title": a.title,
        "category": a.category,
        "source_type": a.source_type,
        "source_url": a.source_url,
        "file_id": a.file_id,
        "extract_status": a.extract_status,  # pending/processing/done/failed
        "extract_error": a.extract_error,
        "rules_version": a.rules_version,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.post("/import-url", status_code=status.HTTP_201_CREATED)
async def import_url(
    body: ImportUrlRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.MARKET_KNOWLEDGE_MANAGE)),
) -> dict:
    """管理员粘贴公开网页/公众号文章 URL：同步抓取正文与图片入库，AI 提炼后台执行。"""
    if not body.url.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="url 不能为空")
    try:
        article = await market_knowledge.import_from_url(session, user, body.url.strip())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except QuotaExceeded as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=None,
        action="market_knowledge.import_url",
        object_type="market_knowledge_article",
        object_id=article.id,
        payload={"url": body.url.strip(), "title": article.title},
    )
    await session.commit()
    return _article_dict(article)


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.MARKET_KNOWLEDGE_MANAGE)),
) -> dict:
    """管理员上传文档/PDF/图片作为行情资料。"""
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件超过大小上限（{settings.max_upload_bytes} 字节）",
        )
    try:
        article = await market_knowledge.create_from_upload(
            session, user, data, file.filename or "未命名资料"
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except QuotaExceeded as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=None,
        action="market_knowledge.upload",
        object_type="market_knowledge_article",
        object_id=article.id,
        payload={"filename": file.filename, "category": article.category},
    )
    await session.commit()
    return _article_dict(article)


@router.get("")
async def list_articles(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    query = select(MarketKnowledgeArticle).where(
        MarketKnowledgeArticle.deleted_at.is_(None),
    )
    if category:
        query = query.where(MarketKnowledgeArticle.category == category)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            (MarketKnowledgeArticle.title.ilike(like))
            | MarketKnowledgeArticle.id.in_(
                select(MarketKnowledgePoint.article_id).where(
                    MarketKnowledgePoint.deleted_at.is_(None),
                    MarketKnowledgePoint.content.ilike(like),
                )
            )
        )
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (
        await session.scalars(
            query.order_by(MarketKnowledgeArticle.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    ids = [a.id for a in rows]
    point_counts: dict[int, int] = {}
    if ids:
        counts = (
            await session.execute(
                select(MarketKnowledgePoint.article_id, func.count())
                .where(
                    MarketKnowledgePoint.article_id.in_(ids),
                    MarketKnowledgePoint.deleted_at.is_(None),
                )
                .group_by(MarketKnowledgePoint.article_id)
            )
        ).all()
        point_counts = {int(article_id): int(cnt) for article_id, cnt in counts}
    items = []
    for a in rows:
        item = _article_dict(a)
        item["points_count"] = point_counts.get(a.id, 0)
        items.append(item)
    return {"items": items, "total": int(total), "page": page, "size": size}


@router.get("/points")
async def list_points(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("search_market_knowledge")),
) -> dict:
    """Agent 参考出口：全量返回本企业所有已提炼要点（当前 strategy=all）。"""
    ref = await market_knowledge.collect_reference_points(session)
    return {
        "total": ref["count"],
        "strategy": ref["strategy"],
        "rules_version": ref["rules_version"],
        "items": ref["items"],
    }


@router.get("/{article_id}")
async def article_detail(
    article_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    article = await session.scalar(
        select(MarketKnowledgeArticle).where(
            MarketKnowledgeArticle.id == article_id,
            MarketKnowledgeArticle.deleted_at.is_(None),
        )
    )
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="行情资料不存在")
    result = _article_dict(article)
    result["text_content"] = article.text_content
    points = (
        await session.scalars(
            select(MarketKnowledgePoint)
            .where(
                MarketKnowledgePoint.article_id == article.id,
                MarketKnowledgePoint.deleted_at.is_(None),
            )
            .order_by(MarketKnowledgePoint.ordinal)
        )
    ).all()
    result["points"] = [
        {"point_id": p.id, "content": p.content, "ordinal": p.ordinal} for p in points
    ]
    image_rows = (
        await session.execute(
            select(MarketKnowledgeImage, FileObject)
            .join(FileObject, FileObject.id == MarketKnowledgeImage.file_id)
            .where(
                MarketKnowledgeImage.article_id == article.id,
            )
            .order_by(MarketKnowledgeImage.ordinal)
        )
    ).all()
    result["images"] = [
        {
            "file_id": fobj.id,
            "ordinal": link.ordinal,
            "name": fobj.original_name,
            "size": fobj.size_bytes,
        }
        for link, fobj in image_rows
    ]
    return result


@router.post("/{article_id}/re-extract")
async def re_extract(
    article_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.MARKET_KNOWLEDGE_MANAGE)),
) -> dict:
    article = await session.scalar(
        select(MarketKnowledgeArticle).where(
            MarketKnowledgeArticle.id == article_id,
            MarketKnowledgeArticle.deleted_at.is_(None),
        )
    )
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="行情资料不存在")
    article.extract_attempt += 1
    article.extract_status = "pending"
    article.extract_error = None
    await session.flush()
    await market_knowledge.enqueue_extract(session, article)
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=None,
        action="market_knowledge.re_extract",
        object_type="market_knowledge_article",
        object_id=article.id,
    )
    await session.commit()
    return _article_dict(article)


@router.delete("/{article_id}")
async def delete_article(
    article_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.MARKET_KNOWLEDGE_MANAGE)),
) -> dict:
    try:
        await market_knowledge.delete_article(session, article_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=None,
        action="market_knowledge.delete",
        object_type="market_knowledge_article",
        object_id=article_id,
    )
    await session.commit()
    return {"deleted": True, "article_id": article_id}
