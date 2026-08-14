"""导出与终检（4.10）：DOCX/XLSX 生成、一致性检查、manifest、交付包。"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO

DELIVERABLE_NAMES = {1: "商务标", 2: "技术标", 3: "报价单"}


def docx_bytes(model: dict) -> bytes:
    from docx import Document

    doc = Document()
    for node in model.get("nodes", []):
        text = node.get("text", "")
        if node.get("type") == "heading":
            doc.add_heading(text, level=1)
        else:
            doc.add_paragraph(text)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def xlsx_bytes(model: dict) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for sheet in model.get("sheets", []):
        ws = wb.create_sheet(str(sheet.get("name", "Sheet"))[:31])
        for row in sheet.get("rows", []):
            ws.append(["" if c is None else c for c in row])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def run_final_check(deliverables) -> dict:
    existing = {d.deliverable_type for d in deliverables}
    issues = []
    for dtype, name in DELIVERABLE_NAMES.items():
        if dtype not in existing:
            issues.append({"type": "完整性", "severity": "error", "message": f"缺少{name}", "locate": None})
    passed = not any(i["severity"] == "error" for i in issues)
    return {"passed": passed, "issues": issues}


def build_manifest(project_id: int, files: list[dict], checks: dict) -> dict:
    return {
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "checks": checks,
        "exemptions": [],
    }
