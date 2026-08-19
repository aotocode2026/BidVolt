from __future__ import annotations

import zipfile
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


def _write_ofd(
    tmp_path: Path,
    text: str,
    size_attr: str = "Size",
    docroot_loc: str = "Doc_0/Document.xml",
    page_base_loc: str = "Doc_0/Pages/Page_1/Content.xml",
) -> Path:
    """构造最小 OFD（zip+XML），按 TextObject 属性大小写区分标准/easyofd 风格。"""
    p = tmp_path / "a.ofd"
    ns = "http://www.ofdspec.org/2016"
    ofd_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<ofd:OFD xmlns:ofd="{ns}">
  <ofd:DocBody>
    <ofd:DocInfo><ofd:DocID>12345678901234567890123456789012</ofd:DocID></ofd:DocInfo>
    <ofd:DocRoot><ofd:BaseLoc>{docroot_loc}</ofd:BaseLoc></ofd:DocRoot>
  </ofd:DocBody>
</ofd:OFD>
'''
    document_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<ofd:Document xmlns:ofd="{ns}">
  <ofd:CommonData>
    <ofd:PageArea><ofd:PhysicalBox>0 0 595.0 842.0</ofd:PhysicalBox></ofd:PageArea>
    <ofd:PublicRes>Doc_0/PublicRes.xml</ofd:PublicRes>
    <ofd:DocumentRes>Doc_0/DocumentRes.xml</ofd:DocumentRes>
  </ofd:CommonData>
  <ofd:Pages>
    <ofd:Page ID="1"><ofd:BaseLoc>{page_base_loc}</ofd:BaseLoc></ofd:Page>
  </ofd:Pages>
</ofd:Document>
'''
    content_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<ofd:Page xmlns:ofd="{ns}">
  <ofd:Content>
    <ofd:Layer ID="4" Type="Foreground">
      <ofd:TextObject ID="5" Font="3" {size_attr}="12" Boundary="0 0 100 20">
        <ofd:TextCode X="0" Y="12">{text}</ofd:TextCode>
      </ofd:TextObject>
    </ofd:Layer>
  </ofd:Content>
</ofd:Page>
'''
    publicres_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<ofd:Res xmlns:ofd="{ns}">
  <ofd:Fonts><ofd:Font ID="3" FontName="SimSun" FamilyName="SimSun"/></ofd:Fonts>
</ofd:Res>
'''
    documentres_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<ofd:Res xmlns:ofd="{ns}"><ofd:MultiMedias/></ofd:Res>
'''
    files = {
        "OFD.xml": ofd_xml,
        "Doc_0/Document.xml": document_xml,
        "Doc_0/Pages/Page_1/Content.xml": content_xml,
        "Doc_0/PublicRes.xml": publicres_xml,
        "Doc_0/DocumentRes.xml": documentres_xml,
    }
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return p


def test_parse_ofd_standard(tmp_path):
    p = _write_ofd(tmp_path, "招标技术要求：电压等级 10kV", size_attr="Size")
    blocks = parser.parse_to_blocks(p, ".ofd")
    assert len(blocks) == 1
    assert blocks[0]["page_no"] == 1
    assert "10kV" in blocks[0]["text_content"]


def test_parse_ofd_easyofd_lowercase_size(tmp_path):
    """easyofd 生成的文件用小写 size 属性，解析端需兼容。"""
    p = _write_ofd(tmp_path, "商务响应：交付周期 30 天", size_attr="size")
    blocks = parser.parse_to_blocks(p, ".ofd")
    assert any("交付周期" in b.get("text_content") for b in blocks)


def test_parse_ofd_no_text_layer(tmp_path):
    import pytest

    p = tmp_path / "scan.ofd"
    ns = "http://www.ofdspec.org/2016"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(
            "OFD.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><ofd:OFD xmlns:ofd="{ns}"><ofd:DocBody>'
            f'<ofd:DocRoot><ofd:BaseLoc>Doc_0/Document.xml</ofd:BaseLoc></ofd:DocRoot>'
            f"</ofd:DocBody></ofd:OFD>",
        )
        zf.writestr(
            "Doc_0/Document.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><ofd:Document xmlns:ofd="{ns}"><ofd:Pages>'
            f'<ofd:Page ID="1"><ofd:BaseLoc>Doc_0/Pages/Page_0/Content.xml</ofd:BaseLoc></ofd:Page>'
            f"</ofd:Pages></ofd:Document>",
        )
        zf.writestr(
            "Doc_0/Pages/Page_0/Content.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><ofd:Page xmlns:ofd="{ns}"><ofd:Content><ofd:Layer ID="4"/>'
            f"</ofd:Content></ofd:Page>",
        )
    with pytest.raises(ValueError, match="无文本层"):
        parser.parse_to_blocks(p, ".ofd")


def test_parse_ofd_leading_slash_loc(tmp_path):
    """真实 OFD（iOFD/dms360 生成）DocRoot 与 Page BaseLoc 使用 / 前缀绝对路径，需兼容。"""
    p = _write_ofd(
        tmp_path,
        "带签章的 OFD：投标文件",
        docroot_loc="/Doc_0/Document.xml",
        page_base_loc="/Doc_0/Pages/Page_1/Content.xml",
    )
    blocks = parser.parse_to_blocks(p, ".ofd")
    assert len(blocks) == 1
    assert "投标文件" in blocks[0]["text_content"]
    assert blocks[0]["page_no"] == 1


def _write_minimal_pptx(tmp_path: Path, slides: list[str]) -> Path:
    """构造最小 pptx（zip + slideN.xml，a:t 文本），供标准库提取路径测试。"""
    p = tmp_path / "a.pptx"
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        for i, text in enumerate(slides, start=1):
            zf.writestr(
                f"ppt/slides/slide{i}.xml",
                f'<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                f'xmlns:a="{ns}"><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:sld>',
            )
    return p


def test_parse_pptx_stdlib(tmp_path):
    """Issue #13：供应商注意事项 .pptx 曾报"不支持的格式"——标准库 zip+XML 提取 slide 文本。"""
    p = _write_minimal_pptx(tmp_path, ["投标注意事项：保证金 5%", "第二页内容：交付周期 30 天"])
    blocks = parser._parse_pptx_stdlib(p)
    assert len(blocks) == 2
    assert blocks[0]["page_no"] == 1
    assert blocks[1]["page_no"] == 2
    assert "保证金 5%" in blocks[0]["text_content"]
    assert "交付周期 30 天" in blocks[1]["text_content"]


def test_parse_pptx_no_text_layer(tmp_path):
    import pytest

    p = tmp_path / "empty.pptx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(
            "ppt/slides/slide1.xml",
            '<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sp/></p:sld>',
        )
    with pytest.raises(ValueError, match="无文本层"):
        parser._parse_pptx_stdlib(p)
