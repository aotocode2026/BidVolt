"""HistoryPriceProvider：外部历史报价只读 Provider（V1 Mock）+ 快照。"""

from __future__ import annotations

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
