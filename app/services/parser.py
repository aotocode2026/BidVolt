"""文档解析（4.3）：文本/表格提取为 doc_block（M3 起由任务队列调度）。"""

from __future__ import annotations

from pathlib import Path


def parse_to_blocks(path: Path, ext: str) -> list[dict]:
    """返回 [{block_type, page_no, block_index, text_content, extra}]。"""
    ext = ext.lower()
    if ext in (".txt", ".csv"):
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return [{"block_type": "paragraph", "page_no": None, "block_index": 0, "text_content": text}]

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
