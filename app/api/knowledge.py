"""企业知识检索接口（Issue #4 MVP）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_permission
from app.constants import Permission
from app.db import get_session
from app.services import knowledge_service
from app.services.audit import write_audit

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class KnowledgeSearchRequest(BaseModel):
    query: str
    project_id: int | None = None  # 传入时默认排除本项目自身材料（避免自我引用）
    top_k: int = 10
    include_assets: bool = True


@router.post("/search")
async def search(
    body: KnowledgeSearchRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    if not body.query.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="query 不能为空")
    result = await knowledge_service.search_knowledge(
        session,
        enterprise_id=user.enterprise_id,
        query=body.query.strip(),
        project_id=body.project_id,
        top_k=body.top_k,
        include_assets=body.include_assets,
    )
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=body.project_id,
        action="knowledge.search",
        object_type="knowledge",
        object_id=0,
        payload={"query": body.query.strip(), "hits": len(result["items"])},
    )
    await session.commit()
    return result
