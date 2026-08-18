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


def run_final_check(deliverables, requirements=None, contents=None) -> dict:
    """终检（Issue #12 / 产品验收）：完整性 + 要求覆盖 + 文档质量。

    - deliverables: Deliverable 列表（必填）
    - requirements: Requirement 列表或含 content 字段的 dict 列表（可选；提供时检查要求覆盖）
    - contents: {deliverable_id: {"version_no": n, "model": dict}}（可选；提供时检查正文质量）
    """
    import re

    existing = {d.deliverable_type for d in deliverables}
    issues: list[dict] = []
    for dtype, name in DELIVERABLE_NAMES.items():
        if dtype not in existing:
            issues.append({"type": "完整性", "severity": "error", "message": f"缺少{name}", "locate": None})

    # 文档质量：仅记录/无正文、占位草稿、Markdown 残留、正文过短
    stub_marker = "草稿由 BidVolt 确定性生成"
    for d in deliverables:
        name = DELIVERABLE_NAMES.get(d.deliverable_type, f"成果{d.deliverable_type}")
        content = (contents or {}).get(d.id)
        if d.current_version_no == 0 or content is None:
            issues.append({"type": "文档质量", "severity": "error", "message": f"{name}：仅有成果记录，尚无正文", "locate": d.id})
            continue
        model = content.get("model") or {}
        nodes = model.get("nodes") or []
        text = "\n".join(str(n.get("text") or "") for n in nodes)
        sheet_text = "\n".join(
            str(c or "")
            for sh in model.get("sheets") or []
            for row in sh.get("rows") or []
            for c in row
        )
        if d.deliverable_type in (1, 2):  # 商务标/技术标按正文检查
            if stub_marker in text:
                issues.append({"type": "文档质量", "severity": "error", "message": f"{name}：仍为占位草稿（未经真实生成）", "locate": d.id})
            if re.search(r"(^|\n)\s*#{1,6}\s", text) or "**" in text:
                issues.append({"type": "文档质量", "severity": "error", "message": f"{name}：正文残留 Markdown 标记（#/**），需重新生成", "locate": d.id})
            if len(text.strip()) < 100:
                issues.append({"type": "文档质量", "severity": "error", "message": f"{name}：正文过短（{len(text.strip())} 字，不足 100 字）", "locate": d.id})
        else:  # 报价单按表格检查
            if stub_marker in sheet_text:
                issues.append({"type": "文档质量", "severity": "error", "message": f"{name}：仍为占位草稿", "locate": d.id})
            if "待报价测算" in sheet_text:
                issues.append({"type": "文档质量", "severity": "warning", "message": f"{name}：尚未录入真实成本（请到报价页测算并应用）", "locate": d.id})

    # 要求覆盖：有要求但成果整体无正文/占位时，必须拦截
    if requirements is not None:
        n = len(requirements)
        if n and contents is not None and not contents:
            issues.append({"type": "要求覆盖", "severity": "error", "message": f"已解析 {n} 条要求，但成果全部无正文", "locate": None})
        if n and any(stub_marker in ("\n".join(str(x.get("text") or "") for x in ((contents or {}).get(d.id) or {}).get("model", {}).get("nodes") or [])) for d in deliverables):
            issues.append({"type": "要求覆盖", "severity": "error", "message": f"已解析 {n} 条要求，但成果仍为占位草稿，未逐条响应", "locate": None})
        if not n:
            issues.append({"type": "要求覆盖", "severity": "warning", "message": "项目未解析出招标要求，成果可能无法逐条响应招标文件", "locate": None})

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
