"""导出与终检（4.10）：DOCX/XLSX 生成、一致性检查、manifest、交付包。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO

logger = logging.getLogger(__name__)

DELIVERABLE_NAMES = {1: "商务标", 2: "技术标", 3: "报价单"}


def docx_from_template(source_path, model: dict) -> bytes:
    """底稿式导出（Issue #8 验收铁律）：复制采购文件原 docx，整本保留——
    不抽取、不裁剪、不删改，只在底稿上做两件事：
    1) 全局填空（已知字段回填，未知空位原位【待补充】）；
    2) 文末追加"补充响应内容"（LLM 增补节）。
    页面规格/字体/表格样式/分页/页眉页脚/全部原文天然保留。"""
    import re as _re

    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document(str(source_path))

    buyer = str(model.get("buyer") or "").strip()
    project_name = str(model.get("project_name") or "").strip()
    supplier = str(model.get("supplier_name") or "").strip()

    _PLACEHOLDER_KEYS = ("（采购人）", "（响应供应商名称）", "（项目名称）")

    def _fill_text(text: str) -> str:
        out = text
        out = out.replace("（采购人）", buyer or "【待补充：招标人名称】")
        out = out.replace("（响应供应商名称）", supplier or "【待补充：供应商名称】")
        out = out.replace("（项目名称）", project_name or "【待补充：项目名称】")
        out = _re.sub(r"_{2,}", "【待补充】", out)
        return out

    def _fill_paragraph_el(p_el) -> None:
        """整段填充：遍历段落内全部 w:t 文本节点（含表格/控件/文本框内段落）。
        占位符完整落在单个节点内时原位替换（保留格式）；
        跨节点拆分的占位符合并到首个节点（该段格式随之归一）。"""
        t_nodes = list(p_el.iter(qn("w:t")))
        if not t_nodes:
            return
        full = "".join(t.text or "" for t in t_nodes)
        if not any(k in full for k in _PLACEHOLDER_KEYS) and not _re.search(r"_{2,}", full):
            return
        for t in t_nodes:
            text = t.text or ""
            if any(k in text for k in _PLACEHOLDER_KEYS) or _re.search(r"_{2,}", text):
                t.text = _fill_text(text)
        remaining = "".join(t.text or "" for t in t_nodes)
        if any(k in remaining for k in _PLACEHOLDER_KEYS) or _re.search(r"_{2,}", remaining):
            merged = _fill_text(remaining)
            for i, t in enumerate(t_nodes):
                t.text = merged if i == 0 else ""

    # 全文档（含表格、控件、文本框）逐段填充
    for p_el in doc.element.body.iter(qn("w:p")):
        _fill_paragraph_el(p_el)
    # 兜底：段落容器之外的裸 w:t 文本节点（如控件内未包段落的 run）逐节点替换
    for t_el in doc.element.body.iter(qn("w:t")):
        text = t_el.text or ""
        if any(k in text for k in _PLACEHOLDER_KEYS) or _re.search(r"_{2,}", text):
            t_el.text = _fill_text(text)

    def _add_heading_safe(doc, text: str, level: int):
        """向底稿追加标题：不依赖源文档是否含 'Heading n' 样式——
        国网等采购文件底稿常无内置标题样式，doc.add_heading 会因
        'no style with name Heading 1' 抛 KeyError 导致整份导出回退。
        改用普通段落 + 手动加粗/字号 + outlineLvl 保持 Word 导航层级。"""
        from docx.oxml import OxmlElement

        p = doc.add_paragraph()
        run = p.add_run(str(text))
        run.bold = True
        run.font.name = "Times New Roman"
        run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "黑体")
        run.font.size = Pt(16 if level == 1 else 14)
        p_pr = p._p.get_or_add_pPr()
        outline = OxmlElement("w:outlineLvl")
        outline.set(qn("w:val"), str(max(int(level) - 1, 0)))
        p_pr.append(outline)
        return p

    doc.add_page_break()
    _add_heading_safe(doc, "补充响应内容（在采购文件模板基础上增加，格式自拟部分）", 1)
    for node in model.get("supplement_nodes") or []:
        t = str(node.get("text") or "")
        ntype = node.get("type")
        if ntype == "heading":
            _add_heading_safe(doc, _fill_text(t), 2)
        elif ntype == "table" and node.get("rows"):
            rows = node["rows"]
            tb = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            tb.style = "Table Grid"
            for ri, row in enumerate(rows):
                for ci in range(len(tb.rows[ri].cells)):
                    cell = tb.rows[ri].cells[ci]
                    cell.text = ""
                    run = cell.paragraphs[0].add_run(_fill_text(str(row[ci])) if ci < len(row) else "")
                    run.font.size = Pt(10.5)
                    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
        elif t.strip():
            doc.add_paragraph(_fill_text(t))

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def docx_bytes_with_source(session, enterprise_id: int, model: dict, format_spec=None) -> bytes:
    """导出统一入口：模型带模板源文件（底稿式）时复制原 docx 直接改；
    否则回退节点式生成。"""
    fid = model.get("template_source_file_id") if isinstance(model, dict) else None
    if fid:
        from app.models.file import FileObject
        from app.services.storage import StorageProvider

        fobj = await session.get(FileObject, int(fid))
        if fobj is not None and fobj.enterprise_id == enterprise_id:
            try:
                return docx_from_template(StorageProvider().open(fobj.bucket, fobj.object_key), model)
            except Exception as exc:  # noqa: BLE001 底稿损坏/缺失时回退节点式
                logger.warning(
                    "底稿式导出失败，回退节点式 file=%s/%s: %s",
                    fobj.bucket, fobj.object_key, type(exc).__name__,
                    exc_info=True,
                )
    return docx_bytes(model, format_spec=format_spec)


_CN_SIZES = {
    "初号": 42, "小初": 36, "一号": 26, "小一": 24, "二号": 22, "小二": 18,
    "三号": 16, "小三": 15, "四号": 14, "小四": 12, "五号": 10.5, "小五": 9, "六号": 7.5,
}


def _resolve_format(spec: dict | None) -> dict:
    """招标文件格式要求 → 排版参数；未要求处用默认（正文宋体小四、1.5 倍行距）。"""
    if not spec:
        return {"font": "宋体", "size": 12, "line_spacing": 1.5}
    font = str(spec.get("font") or "").strip() or "宋体"
    if "黑体" in font:
        font = "黑体"
    elif "仿宋" in font:
        font = "仿宋"
    elif "楷" in font:
        font = "楷体"
    else:
        font = "宋体"
    size_raw = str(spec.get("size") or "").strip()
    size = _CN_SIZES.get(size_raw)
    if size is None:
        import re as _re

        m = _re.search(r"(\d+(?:\.\d+)?)", size_raw)
        size = float(m.group(1)) if m else 12
    spacing_raw = str(spec.get("line_spacing") or "")
    line_spacing = 1.5
    if "单倍" in spacing_raw or "1.0" in spacing_raw:
        line_spacing = 1.0
    elif "2" in spacing_raw and "倍" in spacing_raw:
        line_spacing = 2.0
    return {"font": font, "size": size, "line_spacing": line_spacing}


def docx_bytes(model: dict, format_spec: dict | None = None) -> bytes:
    """DOCX 生成（Issue #8 反馈"字体大小粗细不一致、无标题目录结构"）：
    - 字体/字号/行距优先按招标文件格式要求（format_spec，解析自响应文件格式章节），
      未要求处默认正文宋体小四、1.5 倍行距；
    - 标题分三级（文件标题 / 章 / 节），黑体加粗黑色，Word 导航窗格可见层级；
    - 文首输出目录页（章节清单）。"""
    import re

    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    fmt = _resolve_format(format_spec)
    body_size = float(fmt["size"])
    doc = Document()

    def _style_font(name, east, size, bold):
        style = doc.styles[name]
        style.font.name = "Times New Roman"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), east)
        style.font.size = Pt(size)
        style.font.bold = bold
        style.font.color.rgb = RGBColor(0, 0, 0)

    _style_font("Normal", fmt["font"], body_size, False)
    _style_font("Title", "黑体", body_size + 6, True)
    _style_font("Heading 1", "黑体", body_size + 4, True)
    _style_font("Heading 2", "黑体", body_size + 2, True)
    _style_font("Heading 3", "黑体", body_size + 1, True)
    doc.styles["Normal"].paragraph_format.line_spacing = float(fmt["line_spacing"])

    def _heading_level(text: str, is_first: bool) -> int:
        if is_first and text.strip() in ("商务标", "技术标", "报价单"):
            return 0  # Title
        if re.match(r"^第[一二三四五六七八九十百0-9]+[章部分节]", text.strip()):
            return 1
        if re.match(r"^[一二三四五六七八九十]+、", text.strip()):
            return 1
        return 2

    nodes = model.get("nodes") or []
    # 目录页：收集全部标题
    toc: list[tuple[int, str]] = []
    first_heading_seen = False
    for node in nodes:
        if node.get("type") == "heading":
            text = str(node.get("text") or "").strip()
            if not text:
                continue
            toc.append((_heading_level(text, not first_heading_seen), text))
            first_heading_seen = True

    if toc:
        doc.add_paragraph("目录", style="Heading 1")
        for level, title in toc:
            if level == 0:
                continue
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(0 if level == 1 else 18)
            run = p.add_run(title)
            run.font.size = Pt(12 if level == 1 else 10.5)
            run.font.bold = level == 1
            run.font.name = "Times New Roman"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    first_heading_seen = False
    for node in nodes:
        text = str(node.get("text") or "")
        ntype = node.get("type")
        if ntype == "heading":
            level = _heading_level(text.strip(), not first_heading_seen)
            first_heading_seen = True
            if level == 0:
                p = doc.add_paragraph(text, style="Title")
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            else:
                doc.add_heading(text, level=level)
        elif ntype == "table":
            # 模板表格照搬（Issue #8：表格不能拍平成文字）
            rows = node.get("rows") or []
            if rows:
                t = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
                t.style = "Table Grid"
                for ri, row in enumerate(rows):
                    for ci in range(len(t.rows[ri].cells)):
                        val = row[ci] if ci < len(row) else ""
                        cell = t.rows[ri].cells[ci]
                        cell.text = ""
                        run = cell.paragraphs[0].add_run(str(val))
                        run.font.name = "Times New Roman"
                        run._element.rPr.rFonts.set(qn("w:eastAsia"), fmt["font"])
                        run.font.size = Pt(max(body_size - 1, 9))
        else:
            if text.startswith("- "):
                doc.add_paragraph(text[2:], style="List Bullet")
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
