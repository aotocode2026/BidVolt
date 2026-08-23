"""搜索与引用接口（4.9.2）。"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_capability, require_permission
from app.config import settings
from app.constants import Permission
from app.db import get_session
from app.models.deliverable import Deliverable
from app.models.search import Citation, SearchSource
from app.services import search_service

router = APIRouter(tags=["search"])


@router.post("/searches")
async def search_web(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("search_web")),
) -> dict:
    query = body.get("query", "")
    if settings.search_mode == "anysearch":
        if not search_service.search_gate_open():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="搜索门禁关闭（P1）")
        results = search_service.AnySearchProvider().query(query, body.get("scope"))
        return {"provider": "anysearch", "results": results}
    results = search_service.MockSearchProvider().query(query, body.get("scope"))
    return {"provider": "mock", "results": results}


@router.post("/searches/minimax")
async def search_web_minimax(
    body: dict,
    user: UserContext = Depends(require_capability("search_web_minimax")),
) -> dict:
    """MiniMax 原生联网搜索（全领域开放；来源批注走 save_source/link_citation）。"""
    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 query")
    try:
        results = search_service.minimax_search(query, limit=int(body.get("limit") or 10))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {"provider": "minimax", "results": results}


@router.post("/vision/minimax")
async def vision_minimax(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("vision_analyze_minimax")),
) -> dict:
    """MiniMax 官方视觉理解（与主模型无关；支持 image_url 或项目材料 file_id）。"""
    import base64 as _b64

    from app.models.file import FileObject
    from app.services.storage import StorageProvider

    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 prompt")
    data_url = str(body.get("image_url") or "").strip()
    file_id = body.get("file_id")
    if not data_url and file_id:
        fobj = await session.get(FileObject, int(file_id))
        if fobj is None or fobj.enterprise_id != user.enterprise_id or fobj.is_deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="材料文件不存在")
        ext = (fobj.ext or "").strip(".").lower()
        mime = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
        }.get(ext)
        if mime is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"文件类型 {ext or '未知'} 不是可看图的图片（支持 png/jpg/gif/webp/bmp）",
            )
        raw = StorageProvider().open(fobj.bucket, fobj.object_key).read_bytes()
        data_url = f"data:{mime};base64,{_b64.b64encode(raw).decode('ascii')}"
    if not data_url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 image_url 或 file_id")
    try:
        content = search_service.minimax_vision(prompt, data_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {"provider": "minimax-vlm", "content": content}


@router.post("/search-sources", status_code=status.HTTP_201_CREATED)
async def save_source(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("save_source")),
) -> dict:
    domain = (urlparse(body["url"]).hostname or "").lower()
    row = SearchSource(
        enterprise_id=user.enterprise_id,
        project_id=body.get("project_id"),
        query=body.get("query"),
        url=body["url"],
        title=body.get("title"),
        snippet=body.get("snippet"),
        trust_level=search_service.trust_level(body["url"]),
        domain=domain,
        extra=body.get("extra"),
    )
    session.add(row)
    await session.commit()
    return {"source_id": row.id, "trust_level": row.trust_level, "domain": domain}


@router.get("/search-sources/{source_id}")
async def get_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    row = await session.scalar(
        select(SearchSource).where(
            SearchSource.id == source_id,
            SearchSource.enterprise_id == user.enterprise_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="来源不存在")
    return {
        "source_id": row.id,
        "url": row.url,
        "title": row.title,
        "trust_level": row.trust_level,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
    }


@router.post("/deliverables/{deliverable_id}/citations", status_code=status.HTTP_201_CREATED)
async def link_citation(
    deliverable_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("link_citation")),
) -> dict:
    deliverable = await session.scalar(
        select(Deliverable).where(
            Deliverable.id == deliverable_id,
            Deliverable.enterprise_id == user.enterprise_id,
        )
    )
    if deliverable is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成果不存在")
    source = await session.scalar(
        select(SearchSource).where(
            SearchSource.id == body["source_id"],
            SearchSource.enterprise_id == user.enterprise_id,
        )
    )
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="引用来源不存在")
    citation = Citation(
        enterprise_id=user.enterprise_id,
        project_id=deliverable.project_id,
        deliverable_id=deliverable_id,
        version_no=body["version_no"],
        node_id=body.get("node_id"),
        source_id=source.id,
        quote_text=body.get("quote_text"),
    )
    session.add(citation)
    await session.commit()
    return {"citation_id": citation.id, "version_no": citation.version_no}


@router.get("/deliverables/{deliverable_id}/references")
async def deliverable_references(
    deliverable_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> list[dict]:
    deliverable = await session.scalar(
        select(Deliverable).where(
            Deliverable.id == deliverable_id,
            Deliverable.enterprise_id == user.enterprise_id,
        )
    )
    if deliverable is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成果不存在")
    rows = await session.scalars(
        select(Citation)
        .where(
            Citation.deliverable_id == deliverable_id,
            Citation.enterprise_id == user.enterprise_id,
        )
        .order_by(Citation.id.desc())
    )
    result = []
    for citation in rows:
        source = await session.get(SearchSource, citation.source_id)
        result.append(
            {
                "citation_id": citation.id,
                "version_no": citation.version_no,
                "node_id": citation.node_id,
                "quote_text": citation.quote_text,
                "source": {
                    "url": source.url if source else None,
                    "title": source.title if source else None,
                    "trust_level": source.trust_level if source else None,
                },
            }
        )
    return result
