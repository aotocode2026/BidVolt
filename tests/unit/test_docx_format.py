"""DOCX 导出回归（Issue #8 + 产品要求：唯一生成路径=底稿式，不存在节点式生成）。"""

from __future__ import annotations

import io

from docx import Document

from app.services.export_service import docx_from_template


def _make_source() -> bytes:
    doc = Document()
    doc.add_paragraph("第一章 投标人须知")
    doc.add_paragraph("本采购由（采购人）实施，项目为（项目名称）。")
    doc.add_paragraph("供应商名称：（响应供应商名称）")
    doc.add_paragraph("联系电话：____")
    tb = doc.add_table(rows=1, cols=2)
    tb.rows[0].cells[0].text = "条款"
    tb.rows[0].cells[1].text = "说明"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_draft_export_keeps_whole_source_and_fills(tmp_path):
    src = tmp_path / "source.docx"
    src.write_bytes(_make_source())
    model = {
        "buyer": "测试招标人",
        "project_name": "测试采购项目",
        "supplier_name": "测试供应商",
        "supplement_nodes": [
            {"type": "heading", "text": "一、总体响应说明"},
            {"type": "paragraph", "text": "我方完全响应采购文件全部要求。"},
        ],
    }
    data = docx_from_template(str(src), model)
    doc = Document(io.BytesIO(data))
    texts = [p.text for p in doc.paragraphs]
    joined = "\n".join(texts)
    # 整本保留：原段落都在，表格保留
    assert "第一章 投标人须知" in texts
    assert len(doc.tables) == 1
    # 已知字段回填
    assert "本采购由测试招标人实施，项目为测试采购项目。" in joined
    assert "供应商名称：测试供应商" in joined
    # 下划线空位 → 待补充
    assert "联系电话：【待补充】" in joined
    # 补充节追加
    assert any("补充响应内容" in t for t in texts)
    assert "一、总体响应说明" in texts
    assert "我方完全响应采购文件全部要求。" in texts


def test_draft_export_fills_unknown_with_placeholder(tmp_path):
    src = tmp_path / "source.docx"
    src.write_bytes(_make_source())
    model = {"buyer": "", "project_name": "", "supplier_name": "", "supplement_nodes": []}
    data = docx_from_template(str(src), model)
    joined = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    assert "【待补充：招标人名称】" in joined
    assert "【待补充：项目名称】" in joined
    assert "【待补充：供应商名称】" in joined


def test_draft_export_supplement_headings_bold_without_style_dependency(tmp_path):
    """追加标题不依赖源文档是否含内置 Heading 样式（国网模板曾因此 KeyError 回退）。"""
    src = tmp_path / "source.docx"
    src.write_bytes(_make_source())
    model = {
        "buyer": "",
        "project_name": "",
        "supplier_name": "",
        "supplement_nodes": [
            {"type": "heading", "text": "补充节标题"},
            {"type": "paragraph", "text": "（采购人）应对本项目负责。"},
        ],
    }
    data = docx_from_template(str(src), model)
    out = Document(io.BytesIO(data))
    heads = [p for p in out.paragraphs if p.text == "补充节标题"]
    assert heads and heads[0].runs[0].bold is True
    # 补充节文本同样过填空
    joined = "\n".join(p.text for p in out.paragraphs)
    assert "【待补充：招标人名称】应对本项目负责。" in joined
