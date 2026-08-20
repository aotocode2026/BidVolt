"""DOCX 导出排版回归（Issue #8：字体统一、标题分级、目录）。"""

from __future__ import annotations

import io

from docx import Document

from app.services.export_service import docx_bytes


def test_docx_fonts_and_heading_levels():
    model = {
        "nodes": [
            {"id": "h0", "type": "heading", "text": "技术标"},
            {"id": "h1", "type": "heading", "text": "一、技术方案总体说明"},
            {"id": "p1", "type": "paragraph", "text": "项目理解与总体思路。"},
            {"id": "h2", "type": "heading", "text": "第五章 技术和服务要求响应表"},
            {"id": "h3", "type": "heading", "text": "1.1 逐条响应"},
            {"id": "p2", "type": "paragraph", "text": "- 电压等级 10kV：满足。"},
        ]
    }
    data = docx_bytes(model)
    doc = Document(io.BytesIO(data))

    # 统一字体：Normal 样式含中文字体设置（eastAsia=宋体），西文 Times New Roman
    normal = doc.styles["Normal"]
    assert normal.font.name == "Times New Roman"
    from docx.oxml.ns import qn

    assert normal.element.rPr.rFonts.get(qn("w:eastAsia")) == "宋体"
    assert normal.font.size is not None and normal.font.size.pt == 12

    # 目录页存在，且包含章节标题
    paras = [p for p in doc.paragraphs]
    texts = [p.text for p in paras]
    assert "目录" in texts
    assert "一、技术方案总体说明" in texts
    assert "第五章 技术和服务要求响应表" in texts

    # 标题层级：文件标题 Title 居中；章 Heading 1；节 Heading 2
    styles = {p.text: p.style.name for p in paras}
    assert styles.get("技术标") == "Title"
    assert styles.get("一、技术方案总体说明") == "Heading 1"
    assert styles.get("第五章 技术和服务要求响应表") == "Heading 1"
    assert styles.get("1.1 逐条响应") == "Heading 2"

    # 列表项转 bullet 且去掉 "- " 前缀
    assert any(p.style.name == "List Bullet" and p.text == "电压等级 10kV：满足。" for p in paras)


def test_docx_prefers_tender_format_spec():
    """Issue #8：排版优先按招标文件格式要求；未要求处才用默认。"""
    model = {
        "nodes": [
            {"id": "h0", "type": "heading", "text": "商务标"},
            {"id": "p1", "type": "paragraph", "text": "应答内容。"},
        ]
    }
    spec = {"font": "仿宋", "size": "四号", "line_spacing": "单倍行距", "toc_required": True}
    data = docx_bytes(model, format_spec=spec)
    doc = Document(io.BytesIO(data))
    from docx.oxml.ns import qn

    normal = doc.styles["Normal"]
    assert normal.element.rPr.rFonts.get(qn("w:eastAsia")) == "仿宋"
    assert normal.font.size.pt == 14  # 四号
    assert normal.paragraph_format.line_spacing == 1.0


def test_docx_default_format_without_spec():
    model = {"nodes": [{"id": "p1", "type": "paragraph", "text": "正文"}]}
    data = docx_bytes(model)
    doc = Document(io.BytesIO(data))
    from docx.oxml.ns import qn

    normal = doc.styles["Normal"]
    assert normal.element.rPr.rFonts.get(qn("w:eastAsia")) == "宋体"
    assert normal.font.size.pt == 12  # 小四
    assert normal.paragraph_format.line_spacing == 1.5
