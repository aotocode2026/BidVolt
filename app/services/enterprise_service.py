"""企业资料共享逻辑：按文件名分类 + 分类目录确保存在（上传自动入库与 /ingest 共用同一套）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enterprise_domain import EnterpriseAssetCategory

# (关键词元组, 分类名, 事实模板[(fact_key, confidence)])——fact_value 按资料名填充
_CLASSIFY_RULES: tuple[tuple[tuple[str, ...], str, tuple[tuple[str, float], ...]], ...] = (
    (("营业执照", "执照"), "证照", (("credit_code", 0.9),)),
    (("资质", "许可证"), "资质", (("qualification", 0.6),)),
    (("业绩", "合同", "中标"), "业绩", (("performance", 0.6),)),
    (("身份证", "证书"), "人员", (("personnel", 0.6),)),
    (("检测", "报告"), "检测报告", (("test_report", 0.6),)),
    (("参数", "产品"), "产品参数", (("product_param", 0.6),)),
)

_CATEGORY_NAMES = ("证照", "资质", "业绩", "人员", "产品参数", "检测报告", "其他")


def classify_asset_name(name: str) -> tuple[str, list[tuple[str, str, float]]]:
    """按资料名分类并给出初始事实（key, value, confidence）；无匹配 → ("其他", [])。"""
    lower = name.lower()
    for keywords, category, fact_templates in _CLASSIFY_RULES:
        if any(k in lower for k in keywords):
            return category, [(key, name, confidence) for key, confidence in fact_templates]
    return "其他", []


async def ensure_asset_categories(session: AsyncSession, enterprise_id: int) -> dict[str, int]:
    """确保标准分类目录存在，返回 {分类名: id}。"""
    existing = await session.scalars(
        select(EnterpriseAssetCategory).where(EnterpriseAssetCategory.enterprise_id == enterprise_id)
    )
    mapping = {c.name: c.id for c in existing}
    for name in _CATEGORY_NAMES:
        if name not in mapping:
            cat = EnterpriseAssetCategory(enterprise_id=enterprise_id, name=name)
            session.add(cat)
            await session.flush()
            mapping[name] = cat.id
    return mapping
