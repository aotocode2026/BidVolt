"""文档解析（4.3）：文本/表格提取为 doc_block（M3 起由任务队列调度）。"""

from __future__ import annotations

from pathlib import Path


def parse_to_blocks(path: Path, ext: str) -> list[dict]:
    """返回 [{block_type, page_no, block_index, text_content, extra}]。"""
    ext = ext.lower()
    if ext in (".txt", ".csv"):
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return [{"block_type": "paragraph", "page_no": None, "block_index": 0, "text_content": text}]

    if ext == ".ofd":
        return _parse_ofd(path)

    if ext == ".docx":
        from docx import Document

        doc = Document(str(path))
        blocks: list[dict] = []
        idx = 0
        for para in doc.paragraphs:
            if para.text.strip():
                blocks.append(
                    {"block_type": "paragraph", "page_no": None, "block_index": idx, "text_content": para.text}
                )
                idx += 1
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text for cell in row.cells]
                blocks.append(
                    {
                        "block_type": "table",
                        "page_no": None,
                        "block_index": idx,
                        "text_content": " | ".join(cells),
                        "extra": {"cols": cells},
                    }
                )
                idx += 1
        return blocks

    if ext == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        blocks = []
        idx = 0
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                vals = ["" if v is None else str(v) for v in row]
                if any(vals):
                    blocks.append(
                        {
                            "block_type": "table",
                            "page_no": None,
                            "block_index": idx,
                            "text_content": " | ".join(vals),
                            "extra": {"sheet": ws.title, "cols": vals},
                        }
                    )
                    idx += 1
        wb.close()
        return blocks

    if ext == ".pdf":
        try:
            import fitz  # pymupdf
        except ImportError as exc:  # pragma: no cover - 容器环境安装
            raise ValueError("PDF 解析库未安装（pymupdf）") from exc
        doc = fitz.open(str(path))
        blocks = []
        idx = 0
        for page_no, page in enumerate(doc, start=1):
            text = page.get_text().strip()
            if text:
                blocks.append(
                    {
                        "block_type": "paragraph",
                        "page_no": page_no,
                        "block_index": idx,
                        "text_content": text,
                    }
                )
                idx += 1
        doc.close()
        return blocks

    raise ValueError(f"不支持的格式：{ext}")


def _parse_ofd(path: Path) -> list[dict]:
    """OFD 文本层提取（纯标准库，zip+XML）。

    OFD 版式规范：OFD.xml -> Doc_0/Document.xml -> Pages/Page_N/Content.xml，
    TextObject/TextCode 承载文本。命名空间各实现不一（标准 http://www.ofdspec.org/2016、
    easyofd 自生成 http://blog.yuanhaiying.cn 等），按本地名匹配。
    """
    import xml.etree.ElementTree as ET
    import zipfile

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def elem_text(e: ET.Element) -> str:
        return "".join(e.itertext()).strip()

    blocks: list[dict] = []
    idx = 0
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        doc_root = None
        if "OFD.xml" in names:
            root = ET.fromstring(zf.read("OFD.xml"))
            for el in root.iter():
                if local(el.tag) == "DocRoot":
                    doc_root = elem_text(el) or None
                    if doc_root:
                        break
        if not doc_root:
            doc_root = next((n for n in names if n.endswith("Document.xml")), None)
        if not doc_root:
            raise ValueError("OFD 缺少 Document.xml")

        doc_xml = zf.read(doc_root)
        doc = ET.fromstring(doc_xml)
        page_locs: list[str] = []
        tpl_locs: list[str] = []
        for el in doc.iter():
            name = local(el.tag)
            if name == "Page":
                loc = (el.get("BaseLoc") or elem_text(el) or "").strip()
                if loc and loc not in page_locs:
                    page_locs.append(loc)
            elif name == "TemplatePage":
                loc = (el.get("BaseLoc") or elem_text(el) or "").strip()
                if loc and loc not in tpl_locs:
                    tpl_locs.append(loc)

        # 公共资源（字体等）不作为文档块，仅用于定位内容文件
        content_locs: list[str] = []
        for loc in page_locs:
            if loc not in content_locs:
                content_locs.append(loc)
        for loc in tpl_locs:
            if loc not in content_locs:
                content_locs.append(loc)

        def resolve(loc: str) -> str | None:
            if loc in names:
                return loc
            norm = loc.replace("\\", "/")
            for n in names:
                if n.replace("\\", "/").endswith("/" + norm):
                    return n
            return None

        for loc in content_locs:
            resolved = resolve(loc)
            if not resolved:
                continue
            page_xml = ET.fromstring(zf.read(resolved))
            page_no: int | None = None
            for part in resolved.split("/"):
                if not part.lower().startswith("page"):
                    continue
                for seg in part.split("_"):
                    if seg.isdigit():
                        page_no = int(seg)
                        break
                if page_no is not None:
                    break
            texts: list[str] = []
            for el in page_xml.iter():
                if local(el.tag) != "TextCode":
                    continue
                text = elem_text(el)
                if text:
                    texts.append(text)
            if texts:
                blocks.append(
                    {
                        "block_type": "paragraph",
                        "page_no": page_no,
                        "block_index": idx,
                        "text_content": "\n".join(texts),
                        "extra": {"source": "ofd-text-layer", "page": page_no},
                    }
                )
                idx += 1

    if not blocks:
        raise ValueError("OFD 无文本层（扫描版请走视觉模型）")
    return blocks
