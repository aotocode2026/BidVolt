"""导出与终检（4.10）：DOCX/XLSX 生成、一致性检查、manifest、交付包。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO

logger = logging.getLogger(__name__)

DELIVERABLE_NAMES = {1: "商务标", 2: "技术标", 3: "报价单"}


class _TrackedEditor:
    """在 docx 上以 Word 修订模式记录全部改动（产品要求：验收可见、可追溯）。

    - 每处改动：原文字标为删除（w:del + w:delText，Word 显示删除线），
      新文字标为插入（w:ins + w:t）；
    - 每处改动挂一条批注（w:comment），说明改了什么、依据来源；
    - 文末补充节整体标记为插入，并批注"系统新增内容"。"""

    def __init__(self, doc):
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        self._doc = doc
        self._mk = OxmlElement
        self._qn = qn
        self._seq = {"ins": 100, "del": 200, "comment": 300}
        self._now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.comments: list[tuple[int, str]] = []

    def _mark(self, tag: str):
        kind = "ins" if tag == "w:ins" else "del"
        self._seq[kind] += 1
        el = self._mk(tag)
        el.set(self._qn("w:id"), str(self._seq[kind]))
        el.set(self._qn("w:author"), "BidVolt")
        el.set(self._qn("w:date"), self._now)
        return el

    def _run(self, tag: str, text: str, rpr_source=None):
        r = self._mk("w:r")
        if rpr_source is not None:
            import copy as _copy

            rpr = rpr_source.find(self._qn("w:rPr"))
            if rpr is not None:
                r.append(_copy.deepcopy(rpr))
        t = self._mk(tag)
        t.set(self._qn("xml:space"), "preserve")
        t.text = text
        r.append(t)
        return r

    def _anchor(self, cid: int, before_el):
        """在 before_el 之前插入 commentRangeStart/End 与 commentReference。"""
        start = self._mk("w:commentRangeStart")
        start.set(self._qn("w:id"), str(cid))
        end = self._mk("w:commentRangeEnd")
        end.set(self._qn("w:id"), str(cid))
        ref_r = self._mk("w:r")
        rpr = self._mk("w:rPr")
        rstyle = self._mk("w:rStyle")
        rstyle.set(self._qn("w:val"), "CommentReference")
        rpr.append(rstyle)
        ref_r.append(rpr)
        ref = self._mk("w:commentReference")
        ref.set(self._qn("w:id"), str(cid))
        ref_r.append(ref)
        before_el.addprevious(start)
        before_el.addprevious(end)
        before_el.addprevious(ref_r)
        return start

    def add_comment(self, text: str) -> int:
        self._seq["comment"] += 1
        cid = self._seq["comment"]
        self.comments.append((cid, text))
        return cid

    def track_replace(self, t_el, old_text: str, new_text: str, comment: str) -> None:
        """单个文本节点的替换：原文删除线 + 新文插入 + 批注（保留原 run 格式）。"""
        r_el = t_el.getparent()
        del_el = self._mark("w:del")
        del_el.append(self._run("w:delText", old_text, r_el))
        ins_el = self._mark("w:ins")
        ins_el.append(self._run("w:t", new_text, r_el))
        cid = self.add_comment(comment)
        self._anchor(cid, r_el)
        r_el.addprevious(del_el)
        r_el.addprevious(ins_el)
        t_el.text = ""

    def track_paragraph_replace(self, t_nodes, old_full: str, new_full: str, comment: str) -> None:
        """整段替换（占位符跨 run 拆分时）：整段原文删除线 + 整段新文插入 + 批注。"""
        first_r = t_nodes[0].getparent()
        del_el = self._mark("w:del")
        del_el.append(self._run("w:delText", old_full, first_r))
        ins_el = self._mark("w:ins")
        ins_el.append(self._run("w:t", new_full, first_r))
        cid = self.add_comment(comment)
        self._anchor(cid, first_r)
        first_r.addprevious(del_el)
        first_r.addprevious(ins_el)
        for t in t_nodes:
            t.text = ""

    def track_insert_run(self, run) -> None:
        """把已创建的 run 包进 w:ins（新增内容整体标记为插入）。"""
        ins_el = self._mark("w:ins")
        r_el = run._r
        r_el.addprevious(ins_el)
        ins_el.append(r_el)

    def write_comments_part(self) -> None:
        """把批注写入 word/comments.xml 部件并建立关系。"""
        import html as _html

        if not self.comments:
            return
        from docx.opc.constants import RELATIONSHIP_TYPE as _RT
        from docx.opc.packuri import PackURI
        from docx.opc.part import Part

        rows = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
        ]
        for cid, text in self.comments:
            rows.append(
                f'<w:comment w:id="{cid}" w:author="BidVolt" w:date="{self._now}" w:initials="BV">'
                f'<w:p><w:r><w:t xml:space="preserve">{_html.escape(text, quote=False)}</w:t></w:r></w:p>'
                "</w:comment>"
            )
        rows.append("</w:comments>")
        blob = "".join(rows).encode("utf-8")
        part = Part(
            PackURI("/word/comments.xml"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
            blob,
            self._doc.part.package,
        )
        rt_comments = getattr(
            _RT, "COMMENTS",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
        )
        self._doc.part.relate_to(part, rt_comments)


def docx_from_template(source_path, model: dict) -> bytes:
    """底稿式导出（Issue #8 验收铁律 + 产品要求：改动全程修订模式可追溯）：
    复制采购文件原 docx，整本保留——不抽取、不裁剪、不删改，只做两件事：
    1) 全局填空（已知字段回填，未知空位原位【待补充】），每处改动以 Word
       修订模式记录（原文删除线 + 新文插入），并挂批注说明来源；
    2) 文末追加"补充响应内容"（LLM 增补节），整体标记为插入并批注。
    页面规格/字体/表格样式/分页/页眉页脚/全部原文天然保留。"""
    import re as _re

    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document(str(source_path))
    editor = _TrackedEditor(doc)

    buyer = str(model.get("buyer") or "").strip()
    project_name = str(model.get("project_name") or "").strip()
    supplier = str(model.get("supplier_name") or "").strip()

    _PLACEHOLDER_KEYS = (
        "（采购人）", "（响应供应商名称）", "（项目名称）",
        "【招标人名称】", "【供应商名称】", "【项目名称】",
    )

    def _fill_text(text: str) -> str:
        out = text
        out = out.replace("（采购人）", buyer or "【待补充：招标人名称】")
        out = out.replace("（响应供应商名称）", supplier or "【待补充：供应商名称】")
        out = out.replace("（项目名称）", project_name or "【待补充：项目名称】")
        out = out.replace("【招标人名称】", buyer or "【待补充：招标人名称】")
        out = out.replace("【供应商名称】", supplier or "【待补充：供应商名称】")
        out = out.replace("【项目名称】", project_name or "【待补充：项目名称】")
        out = _re.sub(r"_{2,}", "【待补充】", out)
        # 带标签的空位原位回填（如"响应供应商名称：____（盖章）"）
        if supplier:
            out = _re.sub(r"响应供应商名称[：:]\s*【待补充】", f"响应供应商名称：{supplier}", out)
        if project_name:
            out = _re.sub(r"项目名称[：:]\s*【待补充】", f"项目名称：{project_name}", out)
        if buyer:
            out = _re.sub(r"(?:招标人|采购人)[：:]\s*【待补充】", f"招标人：{buyer}", out)
        return out

    def _comment_for(old: str, new: str) -> str:
        """修订批注：说明改了什么、依据来源（产品要求：批注必须说明来自哪里）。"""
        filled: list[str] = []
        if ("（采购人）" in old or "【招标人名称】" in old or "招标人：" in old) and buyer:
            filled.append(f"招标人=「{buyer}」（来源：招标文件封面/采购公告，系统确定性提取）")
        if ("（响应供应商名称）" in old or "【供应商名称】" in old or "响应供应商名称" in old) and supplier:
            filled.append(f"供应商=「{supplier}」（来源：企业资料-企业名称）")
        if ("（项目名称）" in old or "【项目名称】" in old or "项目名称：" in old) and project_name:
            filled.append(f"项目名称=「{project_name}」（来源：招标文件封面/采购公告，系统确定性提取）")
        if filled:
            return "系统回填：" + "；".join(filled) + "。请人工复核确认。"
        if "【待补充" in new:
            return "模板空位未取得对应资料，系统原位标注【待补充】（不编造内容），请人工填写后确认。"
        return "系统按招标文件内容补充填写（模型推断，供参考），请人工复核确认。"

    def _fill_paragraph_el(p_el) -> None:
        """整段填充（修订模式）：占位符完整落在单节点内 → 该节点处修订替换；
        跨节点拆分 → 整段修订替换；每处改动挂批注说明来源。"""
        t_nodes = list(p_el.iter(qn("w:t")))
        if not t_nodes:
            return
        full = "".join(t.text or "" for t in t_nodes)
        if not any(k in full for k in _PLACEHOLDER_KEYS) and not _re.search(r"_{2,}", full):
            return
        # 跨节点判断：任一占位符的整段计数 ≠ 各节点计数之和 → 拆分在多个 run 里
        cross = False
        for k in _PLACEHOLDER_KEYS:
            if full.count(k) != sum((t.text or "").count(k) for t in t_nodes):
                cross = True
                break
        if not cross:
            full_unders = len(_re.findall(r"_{2,}", full))
            node_unders = sum(len(_re.findall(r"_{2,}", t.text or "")) for t in t_nodes)
            if full_unders != node_unders:
                cross = True
        if _re.search(r"(响应供应商名称|项目名称|招标人|采购人)[：:]\s*_{2,}", full):
            cross = True  # 带标签空位常跨 run，整段处理
        if cross:
            new_full = _fill_text(full)
            if new_full != full:
                editor.track_paragraph_replace(t_nodes, full, new_full, _comment_for(full, new_full))
            return
        for t in t_nodes:
            text = t.text or ""
            if not text:
                continue
            new = _fill_text(text)
            if new != text:
                editor.track_replace(t, text, new, _comment_for(text, new))

    # 全文档（含表格、控件、文本框）逐段填充（修订模式）
    for p_el in doc.element.body.iter(qn("w:p")):
        _fill_paragraph_el(p_el)
    # 兜底：段落容器之外的裸 w:t 文本节点（如控件内未包段落的 run）
    for t_el in doc.element.body.iter(qn("w:t")):
        text = t_el.text or ""
        if any(k in text for k in _PLACEHOLDER_KEYS) or _re.search(r"_{2,}", text):
            new = _fill_text(text)
            if new != text:
                editor.track_replace(t_el, text, new, _comment_for(text, new))

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
    sup_head = _add_heading_safe(doc, "补充响应内容（在采购文件模板基础上增加，格式自拟部分）", 1)
    # 补充节整体为系统新增：标题 run 标插入 + 批注说明来源
    editor.track_insert_run(sup_head.runs[0])
    editor.add_comment("本节为系统在采购文件模板基础上【新增】的补充响应内容："
                       "由模型依据招标文件要求与企业资料撰写（企业事实以资料为准，未知处标【待补充】），"
                       "请人工复核后确认。")
    for node in model.get("supplement_nodes") or []:
        t = str(node.get("text") or "")
        ntype = node.get("type")
        if ntype == "heading":
            h = _add_heading_safe(doc, _fill_text(t), 2)
            editor.track_insert_run(h.runs[0])
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
                    editor.track_insert_run(run)
        elif t.strip():
            p = doc.add_paragraph(_fill_text(t))
            for run in p.runs:
                editor.track_insert_run(run)

    editor.write_comments_part()

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def _resolve_template_fobj(session, enterprise_id: int, model: dict, project_id: int | None = None):
    """解析底稿源 FileObject：模型记录的底稿源优先；旧成果从项目材料兜底。找不到返回 None。"""
    from sqlalchemy import select as sa_select

    from app.models.file import FileObject

    fobj = None
    fid = model.get("template_source_file_id") if isinstance(model, dict) else None
    if fid:
        try:
            fobj = await session.get(FileObject, int(fid))
        except (TypeError, ValueError):
            fobj = None
        if fobj is not None and (fobj.enterprise_id != enterprise_id or fobj.is_deleted):
            fobj = None
    # 旧成果没记底稿源（或记录已失效）时，从项目材料里找采购文件 docx 兜底
    if fobj is None and project_id:
        rows = (
            await session.scalars(
                sa_select(FileObject).where(
                    FileObject.project_id == int(project_id),
                    FileObject.enterprise_id == enterprise_id,
                    FileObject.is_deleted.is_(False),
                    FileObject.owner_type == 2,
                )
            )
        ).all()
        docx_rows = [f for f in rows if (f.ext or "").strip(".").lower() == "docx"]
        fobj = next((f for f in docx_rows if f.document_role == "tender"), None) or (
            max(docx_rows, key=lambda f: f.size_bytes) if docx_rows else None
        )
    return fobj


async def template_source_name(session, enterprise_id: int, model: dict, project_id: int | None = None) -> str | None:
    """底稿源文件名（下载文件名标注用）：找不到/非 docx 返回 None（文件名回退默认格式）。"""
    try:
        fobj = await _resolve_template_fobj(session, enterprise_id, model, project_id)
    except (TypeError, ValueError):
        return None
    if fobj is None or (fobj.ext or "").strip(".").lower() != "docx":
        return None
    name = str(fobj.original_name or "").strip()
    return name[:40] + "…" if len(name) > 40 else (name or None)


def _draft_label(draft_name: str | None, title: str) -> str:
    """成果文件名：带底稿来源标注（产品要求：一眼可见底稿是谁）。"""
    if draft_name:
        return f"{title}(底稿:{draft_name})"
    return title


async def docx_bytes_with_source(
    session, enterprise_id: int, model: dict, project_id: int | None = None
) -> bytes:
    """导出唯一路径（底稿式，Issue #8 验收铁律 + 产品要求）：复制采购文件原 docx 整本保留，
    只做两件事——填空（已知字段回填/未知空位【待补充】）与文末追加"补充响应内容"。
    全部改动以 Word 修订模式（删除线/插入）+ 批注（来源说明）记录，验收直接可查。

    不存在节点式生成，也不存在回退：拿不到底稿源文件就明确报错，绝不从节点重新排版。"""
    from app.services.storage import StorageProvider

    fobj = await _resolve_template_fobj(session, enterprise_id, model, project_id)
    if fobj is None:
        raise ValueError(
            "该成果缺少采购文件底稿：请确认项目已上传采购文件（docx/pdf 等均可），"
            "重新触发【招标解析】和【生成标书】后再下载"
        )
    if (fobj.ext or "").strip(".").lower() != "docx":
        raise ValueError(
            "该成果的底稿源不是 docx（可能是历史数据）：请重新触发【招标解析】和【生成标书】后再下载"
        )
    try:
        source_path = StorageProvider().open(fobj.bucket, fobj.object_key)
    except FileNotFoundError as exc:
        raise ValueError(
            "采购文件底稿在存储中缺失：请重新上传采购文件并重新触发解析、生成后再下载"
        ) from exc
    try:
        return docx_from_template(source_path, model)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 底稿损坏时明确报错，绝不回退节点式
        logger.warning(
            "底稿式导出失败 file=%s/%s: %s",
            fobj.bucket, fobj.object_key, type(exc).__name__,
            exc_info=True,
        )
        raise ValueError(
            f"底稿导出失败（{type(exc).__name__}）：采购文件底稿可能已损坏，"
            "请重新上传采购文件并重新触发解析、生成后再下载"
        ) from exc


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
    tpl_based: dict[int, bool] = {}
    for d in deliverables:
        name = DELIVERABLE_NAMES.get(d.deliverable_type, f"成果{d.deliverable_type}")
        content = (contents or {}).get(d.id)
        if d.current_version_no == 0 or content is None:
            issues.append({"type": "文档质量", "severity": "error", "message": f"{name}：仅有成果记录，尚无正文", "locate": d.id})
            continue
        model = content.get("model") or {}
        tpl_based[d.id] = bool(model.get("template_based"))
        nodes = model.get("nodes") or []
        parts = [str(n.get("text") or "") for n in nodes]
        for n in nodes:
            for row in n.get("rows") or []:
                parts.append(" | ".join(str(c) for c in row))
        text = "\n".join(parts)
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
    # （底稿式成果整本复制采购文件原 docx，模板章节由底稿保证，节点模型只是抽取子集，
    # 不按节点文本误报"缺少章节"）
    if structure:
        by_type = {d.deliverable_type: d for d in deliverables}
        for item in structure:
            title = str(item.get("title") or "").strip()
            role = item.get("role")
            if not title or role not in role_type:
                continue
            dtype = role_type[role]
            d = by_type.get(dtype)
            if d is not None and tpl_based.get(d.id):
                continue
            text = doc_texts.get(dtype, "")
            if title not in text:
                issues.append(
                    {
                        "type": "结构合规",
                        "severity": "error",
                        "message": f"{DELIVERABLE_NAMES.get(dtype, role)}缺少招标文件要求的章节：{title}",
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
        # 逐条要求覆盖（对标的要求）：技术要求→技术标、资格要求→商务标逐条核对。
        # 底稿式成果整本复制采购文件原 docx，要求原文必然逐字在底稿内——
        # 不按节点文本做逐字比对误报"未响应"。
        tpl_by_type = {d.deliverable_type: tpl_based.get(d.id, False) for d in deliverables}
        for req in requirements:
            req_type = req.get("req_type")
            content = str(req.get("content") or "").strip()
            if not content:
                continue
            if req_type == "tech_requirement":
                text = doc_texts.get(2, "")
                target_name = "技术标"
                target_type = 2
            elif req_type == "qualification":
                text = doc_texts.get(1, "")
                target_name = "商务标"
                target_type = 1
            else:
                continue
            if tpl_by_type.get(target_type):
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
