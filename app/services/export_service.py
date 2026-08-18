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


def run_final_check(deliverables, requirements=None, contents=None, structure=None) -> dict:
    """终检（Issue #12 / 产品验收）：完整性 + 结构合规 + 要求覆盖 + 文档质量。

    - deliverables: Deliverable 列表（必填）
    - requirements: Requirement 列表或含 content 字段的 dict 列表（可选；提供时检查要求覆盖）
    - contents: {deliverable_id: {"version_no": n, "model": dict}}（可选；提供时检查正文质量）
    - structure: [{"role": "business|technical|price", "title": 章节名}]（可选；提供时检查结构合规）
    """
    import re

    role_type = {"business": 1, "technical": 2, "price": 3}
    existing = {d.deliverable_type for d in deliverables}
    issues: list[dict] = []
    for dtype, name in DELIVERABLE_NAMES.items():
        if dtype not in existing:
            issues.append({"type": "完整性", "severity": "error", "message": f"缺少{name}", "locate": None})

    # 文档质量：仅记录/无正文、占位草稿、Markdown 残留、正文过短
    stub_marker = "草稿由 BidVolt 确定性生成"
    doc_texts: dict[int, str] = {}
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
        doc_texts[d.deliverable_type] = text + sheet_text
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

    # 结构合规：招标文件要求的章节必须逐章出现在对应成果中
    if structure:
        for item in structure:
            title = str(item.get("title") or "").strip()
            role = item.get("role")
            if not title or role not in role_type:
                continue
            text = doc_texts.get(role_type[role], "")
            if title not in text:
                issues.append(
                    {
                        "type": "结构合规",
                        "severity": "error",
                        "message": f"{DELIVERABLE_NAMES.get(role_type[role], role)}缺少招标文件要求的章节：{title}",
                        "locate": None,
                    }
                )

    # 要求覆盖：有要求但成果整体无正文/占位时，必须拦截
    if requirements is not None:
        n = len(requirements)
        if n and contents is not None and not contents:
            issues.append({"type": "要求覆盖", "severity": "error", "message": f"已解析 {n} 条要求，但成果全部无正文", "locate": None})
        if n and any(stub_marker in ("\n".join(str(x.get("text") or "") for x in ((contents or {}).get(d.id) or {}).get("model", {}).get("nodes") or [])) for d in deliverables):
            issues.append({"type": "要求覆盖", "severity": "error", "message": f"已解析 {n} 条要求，但成果仍为占位草稿，未逐条响应", "locate": None})
        if not n:
            issues.append({"type": "要求覆盖", "severity": "warning", "message": "项目未解析出招标要求，成果可能无法逐条响应招标文件", "locate": None})
        # 逐条要求覆盖（对标的要求）：技术要求→技术标、资格要求→商务标逐条核对
        for req in requirements:
            req_type = req.get("req_type")
            content = str(req.get("content") or "").strip()
            if not content:
                continue
            if req_type == "tech_requirement":
                text = doc_texts.get(2, "")
                target_name = "技术标"
            elif req_type == "qualification":
                text = doc_texts.get(1, "")
                target_name = "商务标"
            else:
                continue
            if not text or content[:10] not in text:
                issues.append(
                    {
                        "type": "要求覆盖",
                        "severity": "error",
                        "message": f"{target_name}未响应要求：{content[:40]}",
                        "locate": req.get("id"),
                    }
                )

    # 文字质量（Issue #12 终检 v2）：重复段落与待补充占位统计
    words: dict[str, int] = {}
    pending_counts: dict[str, int] = {}
    for d in deliverables:
        name = DELIVERABLE_NAMES.get(d.deliverable_type, str(d.deliverable_type))
        text = doc_texts.get(d.deliverable_type, "")
        words[name] = len(text)
        pending_counts[name] = text.count("【待补充】")
        paras = [p.strip() for p in re.split(r"\n+", text) if len(p.strip()) >= 40]
        dup = {p: paras.count(p) for p in set(paras) if paras.count(p) >= 2}
        for p, cnt in list(dup.items())[:3]:
            issues.append(
                {
                    "type": "文字质量",
                    "severity": "error",
                    "message": f"{name}存在重复段落（出现 {cnt} 次）：{p[:30]}…",
                    "locate": d.id,
                }
            )
        if pending_counts[name] > 0:
            issues.append(
                {
                    "type": "文字质量",
                    "severity": "warning",
                    "message": f"{name}存在 {pending_counts[name]} 处【待补充】占位（资料不足处，需人工补齐）",
                    "locate": d.id,
                }
            )

    passed = not any(i["severity"] == "error" for i in issues)
    return {"passed": passed, "issues": issues, "words": words, "pending": pending_counts}


def build_manifest(project_id: int, files: list[dict], checks: dict) -> dict:
    return {
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "checks": checks,
        "exemptions": [],
    }
