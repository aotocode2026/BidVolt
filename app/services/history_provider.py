"""HistoryPriceProvider：AnySearch 公开抽取 + 快照。行情库（公共/私有）见 history_library。"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from hashlib import sha256

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quote import HistoryPriceSnapshot


class HistoryPriceProvider:
    """只暴露三类查询：query_history / get_material_samples / get_source_metadata。"""

    provider_id = "abstract"

    async def query_history(self, params: dict) -> list[dict]:
        raise NotImplementedError

    async def get_material_samples(self, material_ref: str) -> list[dict]:
        raise NotImplementedError

    async def get_source_metadata(self) -> list[dict]:
        raise NotImplementedError


class AnySearchHistoryPriceProvider(HistoryPriceProvider):
    """真实数据源（兜底链第三级）：AnySearch 检索公开中标/成交公告 + LLM 抽取成交价样本。

    抽取不到/门禁关闭/调用失败返回空列表——由调用方如实报「样本不足」，绝不编造。"""

    provider_id = "anysearch_public"

    async def get_material_samples(self, material_ref: str) -> list[dict]:
        from app.services.llm import LLMClient, extract_json, llm_enabled
        from app.services.search_service import AnySearchProvider

        if not llm_enabled():
            return []
        query = f"{material_ref} 中标 成交价 元 中标结果公告"
        try:
            results = await asyncio.to_thread(AnySearchProvider().query, query, None, 6)
        except Exception:  # noqa: BLE001 门禁/额度/网络失败→空
            return []
        if not results:
            return []
        snippets = "\n".join(f"- {r.get('title')}：{r.get('snippet')}" for r in results[:6])[:3000]
        try:
            reply = await LLMClient().chat(
                "你是价格数据抽取助手。从给定检索结果中抽取与材料相关的成交/中标价格样本，输出 JSON 数组："
                '[{"material_name": "...", "spec": "...", "region": "...", "win_price": 数值, '
                '"unit": "元", "win_date": "YYYY-MM-DD", "url": "来源URL"}]。'
                "只依据给定结果，抽不到输出 []；禁止编造。",
                f"材料：{material_ref}\n检索结果：\n{snippets}",
            )
            parsed = extract_json(reply)
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(parsed, list):
            return []
        samples: list[dict] = []
        for s in parsed[:10]:
            if not isinstance(s, dict):
                continue
            price = s.get("win_price")
            if not isinstance(price, (int, float)) or float(price) <= 0:
                continue
            try:
                win_date = date.fromisoformat(str(s.get("win_date") or "")) if s.get("win_date") else date.today()
            except ValueError:
                win_date = date.today()
            samples.append(
                {
                    "material_ref": material_ref,
                    "material_name": str(s.get("material_name") or material_ref),
                    "spec": str(s.get("spec") or ""),
                    "region": str(s.get("region") or "公开信息"),
                    "win_price": float(price),
                    "win_date": win_date,
                    "unit": str(s.get("unit") or "元"),
                    "currency": "CNY",
                    "tax_included": True,
                    "source_url": str(s.get("url") or ""),
                }
            )
        return samples

    async def query_history(self, params: dict) -> list[dict]:
        return await self.get_material_samples(params.get("material_ref") or "")

    async def get_source_metadata(self) -> list[dict]:
        return [
            {
                "provider_id": self.provider_id,
                "source_name": "AnySearch 公开中标/成交公告",
                "fetched_at": date.today().isoformat(),
                "coverage": "公开采购信息检索",
                "update_policy": "实时检索，LLM 抽取（可追溯 URL）",
                "readonly_verified": True,
            }
        ]


async def get_samples_with_fallback(session: AsyncSession, enterprise_id: int, material_ref: str) -> tuple[list[dict], str]:
    """数据源链：行情库（私有+公共）→ AnySearch 公开抽取 → 样本不足如实报（无 Mock，不编造）。"""
    from app.services.history_library import db_samples_for_quote

    db = await db_samples_for_quote(session, enterprise_id, material_ref)
    if len(db) >= 3:
        return db, "history_price_library"
    real = await AnySearchHistoryPriceProvider().get_material_samples(material_ref)
    if len(real) >= 3:
        return real, "anysearch_public"
    return db + real, "insufficient_samples"


def _source_hash(sample: dict) -> str:
    payload = json.dumps(sample, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


async def snapshot_samples(
    session: AsyncSession, enterprise_id: int, samples: list[dict]
) -> list[int]:
    """把本次实际采用的样本写入本地快照，返回 snapshot id 列表（审计/复算）。"""
    ids: list[int] = []
    for s in samples:
        if s.get("scope") in ("public", "private"):
            provider = "snapshot:history_price_library"  # 快照副本，不进行情库查询（避免重复计数）
        else:
            provider = "snapshot:anysearch_public"
        row = HistoryPriceSnapshot(
            enterprise_id=enterprise_id,
            provider_id=provider,
            material_name=(str(s.get("material_name") or ""))[:200],
            material_code=s.get("material_code"),
            spec=s.get("spec"),
            region=s.get("region"),
            win_price=s["win_price"],
            win_date=s["win_date"],
            source_hash=_source_hash(s),
        )
        session.add(row)
        await session.flush()
        ids.append(row.id)
    return ids
