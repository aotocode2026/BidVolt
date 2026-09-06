"""AI 企业资产分类服务。

分类优先级：
1. 图片资产：复用 image_description（qwen-vl 已缓存）的 doc_type；
   无缓存时调用 DashScopeVLClient 描述后映射。
2. 文档资产：读取 doc_block 文本，交给 MiniMax 文本模型分类。
3. 压缩包/其他：保留文件名规则作为兜底。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.doc import DocBlock
from app.models.enterprise_domain import EnterpriseAsset
from app.models.file import FileObject, ImageDescription
from app.services.enterprise_service import classify_asset_name
from app.services.storage import StorageProvider

storage = StorageProvider()

STANDARD_CATEGORIES = ("证照", "资质", "业绩", "人员", "产品参数", "检测报告", "其他")

_DOCTYPE_CATEGORY = {
    "营业执照": "证照",
    "资质证书": "资质",
    "业绩合同页": "业绩",
    "发票": "业绩",
    "社保记录": "人员",
    "人员证件": "人员",
    "审计报告页": "检测报告",
    "检测报告": "检测报告",
    "流程图": "其他",
    "表格": "其他",
    "其他": "其他",
}

_TEXT_CLASSIFY_SYSTEM = (
    "你是企业投标资料分类器。请根据文件名和正文内容，判断这份资料属于哪个分类。"
    "分类只能从以下值中选择：证照、资质、业绩、人员、产品参数、检测报告、其他。"
    "只输出一个 JSON 对象，不要输出其他文字："
    '{"category":"证照|资质|业绩|人员|产品参数|检测报告|其他",'
    '"confidence":0.0到1.0之间的数字,'
    '"reason":"简短说明"}。'
)


def _normalize_category(category: str | None) -> str:
    category = str(category or "").strip()
    if category in STANDARD_CATEGORIES:
        return category
    # 允许模型输出“人员/资质”等近似词
    alias = {
        "证书": "资质",
        "认证": "资质",
        "合同": "业绩",
        "中标": "业绩",
        "发票": "业绩",
        "营业执照": "证照",
        "执照": "证照",
        "社保": "人员",
        "身份证": "人员",
    }
    for key, value in alias.items():
        if key in category:
            return value
    return "其他"


def _map_doctype_to_category(desc: dict) -> str:
    doc_type = str((desc or {}).get("doc_type") or "").strip()
    return _DOCTYPE_CATEGORY.get(doc_type, "其他")


async def _classify_image_asset(session: AsyncSession, fobj: FileObject) -> dict:
    from app.services.image_desc import describe_image_bytes

    desc_row = await session.scalar(
        select(ImageDescription).where(ImageDescription.sha256 == fobj.sha256)
    )
    desc = desc_row.description if desc_row is not None and isinstance(desc_row.description, dict) else None
    if desc is None:
        data = storage.open(fobj.bucket, fobj.object_key).read_bytes()
        desc = await describe_image_bytes(data)
    category = _map_doctype_to_category(desc or {})
    return {
        "category": category,
        "confidence": 0.92 if desc else 0.55,
        "source": "vision",
        "evidence": {"doc_type": (desc or {}).get("doc_type")},
    }
async def _classify_document_asset(session: AsyncSession, fobj: FileObject) -> dict:
    texts = list(
        await session.scalars(
            select(DocBlock.text_content).where(DocBlock.file_id == fobj.id)
        )
    )
    text = "\n".join(t or "" for t in texts)[:6000]
    if not text.strip():
        category = classify_asset_name(fobj.original_name)[0]
        return {
            "category": category,
            "confidence": 0.35,
            "source": "filename",
            "evidence": {"reason": "未提取到正文，回退文件名规则"},
        }

    from app.services.llm import LLMClient, llm_enabled, try_extract_json

    if not llm_enabled():
        category = classify_asset_name(fobj.original_name)[0]
        return {
            "category": category,
            "confidence": 0.35,
            "source": "filename",
            "evidence": {"reason": "文本模型未启用，回退文件名规则"},
        }
    user_prompt = f"文件名：{fobj.original_name}\n\n正文摘录：\n{text}"
    try:
        raw = await LLMClient().chat(_TEXT_CLASSIFY_SYSTEM, user_prompt)
        parsed = try_extract_json(raw)
        if isinstance(parsed, dict):
            category = _normalize_category(parsed.get("category"))
            try:
                confidence = float(parsed.get("confidence") or 0.6)
            except (TypeError, ValueError):
                confidence = 0.6
            return {
                "category": category,
                "confidence": max(0.0, min(1.0, confidence)),
                "source": "text_llm",
                "evidence": parsed,
            }
    except Exception:
        pass
    category = classify_asset_name(fobj.original_name)[0]
    return {
        "category": category,
        "confidence": 0.35,
        "source": "filename",
        "evidence": {"reason": "文本模型调用失败，回退文件名规则"},
    }


async def classify_asset_with_ai(
    session: AsyncSession, asset: EnterpriseAsset
) -> dict:
    """按资产实际文件内容智能分类。"""
    fobj = await session.get(FileObject, asset.source_file_id) if asset.source_file_id else None
    if fobj is None:
        category = classify_asset_name(asset.name)[0]
        return {
            "category": category,
            "confidence": 0.2,
            "source": "filename",
            "evidence": {"reason": "源文件缺失"},
        }

    ext = (fobj.ext or "").lower()
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff"):
        return await _classify_image_asset(session, fobj)
    if ext in (".docx", ".pdf", ".doc", ".txt", ".md", ".pptx", ".xlsx"):
        return await _classify_document_asset(session, fobj)
    category = classify_asset_name(asset.name)[0]
    return {
        "category": category,
        "confidence": 0.25,
        "source": "filename",
        "evidence": {"reason": f"不支持直接读取的扩展名：{ext or '未知'}"},
    }
