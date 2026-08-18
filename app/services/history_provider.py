"""HistoryPriceProvider：历史报价只读 Provider（真实 AnySearch + Mock 兜底）+ 快照。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from hashlib import sha256

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quote import HistoryPriceSnapshot


class HistoryPriceProvider:
    """只暴露三类查询：query_history / get_material_samples / get_source_metadata。"""

    provider_id = "mock_history"

    async def query_history(self, params: dict) -> list[dict]:
        raise NotImplementedError

    async def get_material_samples(self, material_ref: str) -> list[dict]:
        raise NotImplementedError

    async def get_source_metadata(self) -> list[dict]:
        raise NotImplementedError


class MockHistoryPriceProvider(HistoryPriceProvider):
    """V1 Mock：合成样本（单位/币种/含税口径一致）。"""

    def __init__(self) -> None:
        today = date(2026, 8, 1)
        self._samples = [
            {
                "material_ref": "CABLE-YJV-3x95",
                "material_name": "电力电缆 YJV 3x95",
                "spec": "3x95",
                "region": "华东",
                "win_price": 118.0,
                "win_date": today - timedelta(days=i * 30),
                "unit": "元",
                "currency": "CNY",
                "tax_included": True,
            }
            for i in range(8)
        ]
        for i, sample in enumerate(self._samples):
            sample["win_price"] = round(115.0 + i * 1.2, 2)

    async def query_history(self, params: dict) -> list[dict]:
        material_ref = params.get("material_ref")
        if material_ref:
            return [s for s in self._samples if s["material_ref"] == material_ref]
        return self._samples

    async def get_material_samples(self, material_ref: str) -> list[dict]:
        return await self.query_history({"material_ref": material_ref})

    async def get_source_metadata(self) -> list[dict]:
        return [
            {
                "provider_id": self.provider_id,
                "source_name": "Mock 历史中标库",
                "fetched_at": date.today().isoformat(),
                "coverage": "华东地区电缆样本",
                "update_policy": "只读，外部维护",
                "readonly_verified": True,
            }
        ]


class AnySearchHistoryPriceProvider(HistoryPriceProvider):
    """真实数据源（路线图项）：AnySearch 检索公开中标/成交公告 + LLM 抽取成交价样本。

    抽取不到/门禁关闭/调用失败返回空列表——由 get_samples_with_fallback 兜底 Mock。
    """

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


async def get_samples_with_fallback(material_ref: str) -> tuple[list[dict], str]:
    """真实数据源优先：AnySearch+LLM 抽取 ≥3 条时采用；否则 Mock 兜底（V1 兼容）。
    返回 (样本列表, provider_id)，调用方记录来源以保证可追溯。"""
    real = await AnySearchHistoryPriceProvider().get_material_samples(material_ref)
    if len(real) >= 3:
        return real, "anysearch_public"
    mock = await MockHistoryPriceProvider().get_material_samples(material_ref)
    return mock, "mock_history"


def _source_hash(sample: dict) -> str:
    payload = json.dumps(sample, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


async def snapshot_samples(
    session: AsyncSession, enterprise_id: int, samples: list[dict]
) -> list[int]:
    """把本次实际采用的样本写入本地快照，返回 snapshot id 列表（审计/复算）。"""
    ids: list[int] = []
    for s in samples:
        row = HistoryPriceSnapshot(
            enterprise_id=enterprise_id,
            provider_id="mock_history",
            material_name=s["material_name"],
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
