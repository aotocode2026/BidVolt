"""Issue #12 回归测试：用产品复测的真实材料作为测试用例。

- 真实 docx（product-review-issue12/ 下的产品上传文件，不进 git）：解析块数量正常、无 6 倍重复；
- 重解析清旧块：同一文件二次解析不累积重复块；
- 抽取提示词去重辅助函数；
- 终检要求覆盖 + 文档质量。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PRODUCT_DOCX = REPO / "product-review-issue12" / "中国电力科学研究院2026变电站土建工程谈判采购-一次性采购结果.docx"

needs_fixture = pytest.mark.skipif(not PRODUCT_DOCX.exists(), reason="产品材料未就位（product-review-issue12/，不进 git）")


@needs_fixture
def test_product_docx_parses_without_duplication():
    """产品真实 docx：单次解析块数应在合理范围（此前 6 轮累积 2370 块），同一文本不出现 6 次。"""
    from collections import Counter

    from app.services.parser import parse_to_blocks

    blocks = parse_to_blocks(PRODUCT_DOCX, ".docx")
    assert 50 <= len(blocks) <= 900, f"块数异常：{len(blocks)}"
    texts = [b["text_content"].strip() for b in blocks if b["text_content"].strip()]
    counts = Counter(texts)
    over = {t: n for t, n in counts.items() if n > 3}
    # 同一文本最多重复 3 次（表格表头/合并单元格允许少量重复；6 倍重复是历史 bug 特征）
    assert len(over) == 0, f"{len(over)} 个文本重复超 3 次，示例：{list(over.items())[:5]}"
    joined = "\n".join(texts)
    assert "中国电力科学研究院" in joined


@needs_fixture
def test_product_docx_dedup_text_is_stable():
    """同一文件解析两次：去重后的文本应当一致（解析确定性）。"""
    from types import SimpleNamespace

    from app.services.parser import parse_to_blocks
    from app.services.task_service import _dedup_block_texts

    def to_blocks():
        return [SimpleNamespace(text_content=b["text_content"]) for b in parse_to_blocks(PRODUCT_DOCX, ".docx")]

    first = _dedup_block_texts(to_blocks())
    second = _dedup_block_texts(to_blocks())
    assert first == second
    assert len(first) > 1000


def test_dedup_block_texts_drops_duplicates():
    from app.services.task_service import _dedup_block_texts

    class B:
        def __init__(self, t):
            self.text_content = t

    out = _dedup_block_texts([B("甲"), B("甲"), B(""), B("乙"), B("乙"), B("丙")])
    assert out == "甲\n乙\n丙"


def test_reparse_clears_old_blocks(tmp_path, monkeypatch):
    """重解析同一文件必须清除旧块（此前 6 轮解析累积 6 倍重复块的根因）。"""
    import asyncio

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.doc import DocBlock
    from app.models.file import FileObject
    from app.services import file_service

    txt = tmp_path / "t.txt"
    txt.write_text("第一行\n第二行", encoding="utf-8")
    db = tmp_path / "r.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    import app.models  # noqa: F401  注册全部模型
    from app.models.base import Base

    async def _create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_schema())

    class FakeStorage:
        def open(self, bucket, key):
            return txt

    monkeypatch.setattr(file_service, "storage", FakeStorage())

    async def scenario():
        async with factory() as session:
            f = FileObject(
                enterprise_id=1, project_id=1, owner_type=2,
                bucket="b", object_key="k", sha256="s", original_name="t.txt",
                size_bytes=10, mime_type="text/plain", ext=".txt", status=2,
            )
            session.add(f)
            await session.commit()
            for _ in range(3):
                await file_service._parse_file(session, f)
                await session.commit()
            count = await session.scalar(select(func.count()).select_from(DocBlock).where(DocBlock.file_id == f.id))
            return count

    count = asyncio.run(scenario())
    engine.sync_engine.dispose()
    assert count == 1  # txt 单块；三次解析不应累积为 3 块
