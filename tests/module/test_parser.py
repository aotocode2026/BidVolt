from __future__ import annotations

from pathlib import Path

from app.services import parser


def test_parse_txt(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("第一行\n第二行", encoding="utf-8")
    blocks = parser.parse_to_blocks(p, ".txt")
    assert len(blocks) == 1
    assert "第一行" in blocks[0]["text_content"]


def test_parse_docx(tmp_path):
    from docx import Document

    p = tmp_path / "a.docx"
    doc = Document()
    doc.add_paragraph("商务响应测试")
    doc.add_paragraph("技术响应测试")
    doc.save(str(p))
    blocks = parser.parse_to_blocks(p, ".docx")
    texts = [b["text_content"] for b in blocks]
    assert "商务响应测试" in texts
    assert "技术响应测试" in texts


def test_parse_xlsx(tmp_path):
    from openpyxl import Workbook

    p = tmp_path / "a.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["材料", "数量"])
    ws.append(["电缆", "100"])
    wb.save(str(p))
    blocks = parser.parse_to_blocks(p, ".xlsx")
    assert any("电缆" in (b.get("text_content") or "") for b in blocks)


def test_parse_pdf(tmp_path):
    try:
        import fitz  # pymupdf
    except ImportError:
        import pytest

        pytest.skip("pymupdf 未安装")
    p = tmp_path / "a.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "招标技术要求：电压等级 10kV")
    doc.save(str(p))
    doc.close()
    blocks = parser.parse_to_blocks(p, ".pdf")
    assert any("10kV" in (b.get("text_content") or "") for b in blocks)
