#!/usr/bin/env python3
"""bidvolt 整文件直写 docx 的渲染修复链。

修复三类「python-docx 能打开、LibreOffice 拒载」的隐患（189 实测根因）：

1. 从采购文件 docx 复制章节时带入的段落内嵌 sectPr（分节符），其
   headerReference/footerReference 指向新文档里不存在的 header/footer 部件
   → LibreOffice 报 "Error: source file could not be loaded"（render_qa_docx 同病）。
   **修正（issue #65）**：只删「目标部件不存在」的悬空引用；目标部件存在的引用
   一律保留，并在节塌缩前上提到 body sectPr——原实现「清全部 sectPr 内
   headerReference/footerReference」会把模板现成的页码页脚一起删掉
   （项目 202 实测：包里 10 个页脚部件与 rels 都在，引用被删空 → 交付件无页码）。
2. 内嵌 sectPr 本身=分节符强制换页 + 分页符 + 空段 → 渲染出空白页。
3. 表尾/文末空段遗留。

用法：python3 fix_docx_render.py a.docx b.docx ...
跑完用本地 LibreOffice 验证：
  libreoffice --headless -env:UserInstallation=file:///tmp/lo_prof_1 --convert-to pdf --outdir /tmp/lo_test a.docx
再 PUT /api/v1/projects/{pid}/assembly/artifacts/{aid} 覆盖上传 + render-qa 复验。

回执字段：para_sectpr=删内嵌分节符数 / page_br=删显式分页符数 / pbb=删段前分页数 /
empty=删空段数 / hf_ref_dropped=删悬空页眉页脚引用数 / hf_ref_kept=保留的有效引用数 /
hf_ref_restored=上提到 body sectPr 的引用数。
"""
import re
import sys
import zipfile

from docx import Document
from docx.oxml.ns import qn

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_HF_TAGS = (f"{{{_W_NS}}}headerReference", f"{{{_W_NS}}}footerReference")


def _package_parts(path: str) -> tuple[set[str], dict[str, bytes]]:
    """zip 成员清单 + 部件字节（只读 header/footer 这类小部件）。"""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        blobs: dict[str, bytes] = {}
        for name in names:
            if re.fullmatch(r"word/(header|footer)\d*\.xml", name):
                blobs[name] = z.read(name)
    return names, blobs


def _valid_refs(path: str) -> dict[str, dict]:
    """rId → {type, part, has_page}：只收「目标部件确实存在于包里」的关系。

    悬空 rId 正是 LibreOffice 拒载的根因；反过来，部件在的关系必须保留，
    否则会把模板现成的页码页脚一起删掉。
    """
    names, blobs = _package_parts(path)
    try:
        with zipfile.ZipFile(path) as z:
            rels = z.read("word/_rels/document.xml.rels").decode("utf-8", "replace")
    except KeyError:
        return {}
    out: dict[str, dict] = {}
    for seg in re.findall(r"<Relationship\b[^>]*/?>", rels):
        rid = re.search(r'\bId="([^"]+)"', seg)
        target = re.search(r'\bTarget="([^"]+)"', seg)
        rtype = re.search(r'\bType="([^"]+)"', seg)
        if not (rid and target and rtype):
            continue
        if not rtype.group(1).endswith(("header", "footer")):
            continue
        part = target.group(1).lstrip("/")
        if not target.group(1).startswith("/"):
            part = f"word/{target.group(1)}"
        if part not in names:
            continue  # 悬空引用：调用方负责删除
        out[rid.group(1)] = {
            "part": part,
            "type": rtype.group(1).rsplit("/", 1)[-1],
            "has_page": b"PAGE" in (blobs.get(part) or b""),
        }
    return out


def _ref_insert_index(sect) -> int:
    """header/footerReference 必须排在其它 sectPr 子元素之前（OOXML 顺序）。"""
    idx = 0
    for i, child in enumerate(sect):
        if child.tag in _HF_TAGS:
            idx = i + 1
    return idx


def strip_all(path: str) -> dict:
    valid = _valid_refs(path)
    doc = Document(path)
    body = doc.element.body
    n = {
        "para_sectpr": 0,
        "page_br": 0,
        "pbb": 0,
        "empty": 0,
        "hf_ref_dropped": 0,
        "hf_ref_kept": 0,
        "hf_ref_restored": 0,
    }

    # 1) 整篇（含内嵌）sectPr 的页眉页脚引用：悬空删、有效收好待上提
    kept: dict[str, tuple[str, str, bool]] = {}  # ref_type -> (tag, rid, has_page)
    for sect in doc.element.iter(qn("w:sectPr")):
        for child in list(sect):
            if child.tag not in _HF_TAGS:
                continue
            rid = child.get(qn("r:id"))
            info = valid.get(rid or "")
            if info is None:
                sect.remove(child)
                n["hf_ref_dropped"] += 1
                continue
            ref_type = child.get(qn("w:type")) or "default"
            n["hf_ref_kept"] += 1
            prev = kept.get(ref_type)
            # 同类型多份时优先保留带 PAGE 域的那份（页码页脚优先）
            if prev is None or (info["has_page"] and not prev[2]):
                kept[ref_type] = (child.tag, rid, info["has_page"])

    # 2) 段落级清理（内嵌分节符 / 段前分页 / 显式分页符 / 无文本无图空段）
    for p in list(doc.paragraphs):
        pPr = p._p.find(qn("w:pPr"))
        if pPr is not None:
            for sp in pPr.findall(qn("w:sectPr")):
                pPr.remove(sp)
                n["para_sectpr"] += 1
            pbb = pPr.find(qn("w:pageBreakBefore"))
            if pbb is not None:
                pPr.remove(pbb)
                n["pbb"] += 1
        for br in p._p.findall(".//" + qn("w:br")):
            if br.get(qn("w:type")) == "page":
                br.getparent().remove(br)
                n["page_br"] += 1
        texts = "".join(t.text or "" for t in p._p.iter(qn("w:t")))
        has_drawing = p._p.find(".//" + qn("w:drawing")) is not None
        if texts.strip() == "" and not has_drawing:
            body.remove(p._p)
            n["empty"] += 1

    # 3) 节塌缩前把有效引用上提到 body sectPr（否则删内嵌 sectPr = 毁页码）
    body_sect = body.find(qn("w:sectPr"))
    if body_sect is not None:
        existing = {
            (c.get(qn("w:type")) or "default", c.get(qn("r:id")))
            for c in body_sect
            if c.tag in _HF_TAGS
        }
        for ref_type, (tag, rid, _has_page) in kept.items():
            if (ref_type, rid) in existing:
                continue
            el = body_sect.makeelement(tag, {})
            el.set(qn("w:type"), ref_type)
            el.set(qn("r:id"), rid)
            body_sect.insert(_ref_insert_index(body_sect), el)
            existing.add((ref_type, rid))
            n["hf_ref_restored"] += 1

    doc.save(path)
    return n


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python3 fix_docx_render.py a.docx b.docx ...")
        sys.exit(1)
    for f in sys.argv[1:]:
        print(f, strip_all(f))
