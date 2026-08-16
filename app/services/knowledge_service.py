"""企业知识检索（Issue #4 第一阶段 MVP）。

范围与边界：
- 检索对象：本企业历史项目的已解析材料文本块（DocBlock）、企业资料文本块与已确认企业事实；
  默认排除当前项目自身材料（避免自我引用）；跨企业永不互通（enterprise_id 过滤 + RLS）。
- 匹配方式：关键词/双字 bigram 重叠打分（中文无分词依赖），返回来源可追溯的片段
  （文件/项目/页码/块索引/资料类型/角色），供生成、校核、评审共同引用；
- 企业事实（enterprise_fact）单独返回，标注 confirmed，供"事实只来自企业资料"约束使用；
- 向量/混合检索、成果正文检索列入第二阶段（数据规模扩大后再引入）。

历史内容仅作经验素材：调用方（生成任务/Agent）不得把检索片段中的项目名、金额、工期、
人员等直接当作当前项目事实（Skill 提示词已约束）。
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.doc import DocBlock
from app.models.enterprise_domain import EnterpriseAsset, EnterpriseFact
from app.models.file import FileObject

MAX_SCAN_BLOCKS = 8000


def tokenize_query(query: str) -> list[str]:
    tokens: list[str] = []
    for t in re.findall(r"[a-zA-Z0-9]+", query.lower()):
        if len(t) >= 2:
            tokens.append(t)
    for seg in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(seg) <= 3:
            tokens.extend(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    if not tokens:
        tokens = [query.strip().lower()]
    return list(dict.fromkeys(tokens))


def score_text(text: str, tokens: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for t in tokens if t in lowered)


def make_snippet(text: str, tokens: list[str], window: int = 160) -> str:
    lowered = text.lower()
    first = min((lowered.find(t) for t in tokens if lowered.find(t) >= 0), default=0)
    start = max(0, first - 40)
    snippet = text[start : start + window]
    return snippet


async def search_knowledge(
    session: AsyncSession,
    *,
    enterprise_id: int,
    query: str,
    project_id: int | None = None,
    top_k: int = 10,
    include_assets: bool = True,
) -> dict:
    """关键词检索历史项目材料/企业资料文本块 + 已确认企业事实，结果可追溯。"""
    tokens = tokenize_query(query)
    top_k = max(1, min(top_k, 50))

    rows = (
        await session.execute(
            select(DocBlock, FileObject)
            .join(FileObject, FileObject.id == DocBlock.file_id)
            .where(
                FileObject.enterprise_id == enterprise_id,
                FileObject.is_deleted.is_(False),
                FileObject.status == 3,  # 仅已解析
            )
            .order_by(DocBlock.id.desc())
            .limit(MAX_SCAN_BLOCKS)
        )
    ).all()

    scored: list[dict] = []
    for block, fobj in rows:
        if fobj.owner_type == 2 and project_id is not None and fobj.project_id == project_id:
            continue  # 默认排除当前项目材料，避免自我引用
        if fobj.owner_type == 1 and not include_assets:
            continue
        score = score_text(block.text_content or "", tokens)
        if score <= 0:
            continue
        asset = None
        if fobj.owner_type == 1:
            asset = await session.scalar(
                select(EnterpriseAsset).where(EnterpriseAsset.source_file_id == fobj.id)
            )
        scored.append(
            {
                "source_type": "enterprise_asset" if fobj.owner_type == 1 else "project_material",
                "file_id": fobj.id,
                "file_name": fobj.original_name,
                "project_id": fobj.project_id,
                "asset_id": asset.id if asset else None,
                "category": fobj.category or (asset.asset_type if asset else None),
                "document_role": fobj.document_role,
                "block_id": block.id,
                "page_no": block.page_no,
                "block_index": block.block_index,
                "snippet": make_snippet(block.text_content or "", tokens),
                "score": score,
            }
        )
    scored.sort(key=lambda item: (-item["score"], -item["block_id"]))
    items = scored[:top_k]

    facts: list[dict] = []
    if include_assets:
        fact_rows = (
            await session.scalars(
                select(EnterpriseFact)
                .where(
                    EnterpriseFact.enterprise_id == enterprise_id,
                    EnterpriseFact.status == 2,  # 已确认
                )
                .order_by(EnterpriseFact.id.desc())
                .limit(500)
            )
        ).all()
        for fact in fact_rows:
            value_text = (
                fact.fact_value.get("value", "")
                if isinstance(fact.fact_value, dict)
                else str(fact.fact_value or "")
            )
            if score_text(fact.fact_key or "", tokens) + score_text(str(value_text), tokens) > 0:
                facts.append(
                    {
                        "fact_id": fact.id,
                        "fact_key": fact.fact_key,
                        "fact_value": fact.fact_value,
                        "asset_id": fact.asset_id,
                    }
                )
                if len(facts) >= top_k:
                    break
    return {"items": items, "facts": facts, "tokens": tokens}
