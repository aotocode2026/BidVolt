"""路线图项回归：真实价格数据源兜底（AnySearch 不可用时 Mock）。"""
from __future__ import annotations

import asyncio

from app.services.history_provider import AnySearchHistoryPriceProvider, get_samples_with_fallback


def test_anysearch_provider_returns_empty_without_llm(monkeypatch):
    async def run():
        return await AnySearchHistoryPriceProvider().get_material_samples("CABLE-YJV-3x95")

    # 门禁关闭（默认测试环境）→ 空样本
    assert asyncio.run(run()) == []


def test_samples_fallback_to_mock_when_real_unavailable():
    async def run():
        samples, source = await get_samples_with_fallback("CABLE-YJV-3x95")
        return samples, source

    samples, source = asyncio.run(run())
    assert source == "mock_history"  # LLM 门禁关闭 → 真实源不可用 → Mock 兜底
    assert len(samples) >= 3
    assert all(isinstance(s["win_price"], float) for s in samples)
