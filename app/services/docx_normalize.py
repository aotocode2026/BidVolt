"""交付 docx 格式归一化：页码页脚 + 大纲级别噪声（issue #65 / #66）。

背景（2026-09 复盘，项目 217 / 202 生产库实测）
------------------------------------------------
交付件页码缺失有两套成因：

1. **整文件直写通道**（`build_docx_from_recipe.py` / `assemble.py` 用 `Document()`
   从零新建）：包里没有任何 header/footer 部件，自然没有 PAGE 域。
2. **底稿裁剪通道**：包里 header/footer 部件与 rels 关系都在，但 `document.xml` 里
   `footerReference` 被「清全部 sectPr 内 headerReference/footerReference」的修复链
   删空（202 实测 4 份文件 10 个部件全在、引用数 0）→ 渲染无页码。

模板口径（第五次 / 第七次采购文件完全一致）：页脚**居中、单个
`PAGE \\* MERGEFORMAT` 域、纯数字、9pt**（模板 footer 样式 `sz=18`，
中文宋体 / 西文 Times New Roman）。

大纲级别噪声：图注 / 题注被写入 `w:outlineLvl`（217 实测 306 条 `val="8"`），
混进 Word 导航窗格与 PDF 书签，把真正的条目淹没。

本模块全部走「部件级 / 纯属性」改写，不动正文文字、图片、表格：

- `ensure_page_footer`：缺页脚就按模板口径注入；有部件但引用空心化就把引用接回去。
- `strip_outline_noise`：删掉 `outlineLvl ≥ 6` 与题注型段落上的任何大纲级别。
- `audit_docx`：给打包门禁用的只读体检。
"""

from __future__ import annotations

import io
import re
import zipfile

from lxml import etree

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WQ = f"{{{_W}}}"
_RQ = f"{{{_R}}}"

_DOC = "word/document.xml"
_RELS = "word/_rels/document.xml.rels"
_SETTINGS = "word/settings.xml"
_CT = "[Content_Types].xml"

_FOOTER_PART_RE = re.compile(r"^word/footer\d*\.xml$")
_FOOTER_REL_TYPE = f"{_R}/footer"
_CT_FOOTER = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"

# 题注型段落：图注 / 表注 / 附件说明一律不进大纲
_CAPTION_RE = re.compile(r"^\s*(图|表|附件|附图|附表|照片|扫描件)\s*[:：]")

# 正式文件最多 6 级标题（outlineLvl 0–5 = 1–6 级）
_MAX_OUTLINE_LEVEL = 5

# 模板页脚 run 字体：中文宋体、西文 Times New Roman、9pt
_RPR = (
    '<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
    'w:eastAsia="宋体"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>'
)


def _footer_xml() -> bytes:
    """模板口径的页码页脚：居中 + 单个 PAGE 域（三段式 fldChar）。"""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<w:ftr xmlns:w="{_W}" xmlns:r="{_R}">'
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        f'<w:r>{_RPR}<w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> PAGE   \\* MERGEFORMAT </w:instrText></w:r>'
        f'<w:r>{_RPR}<w:fldChar w:fldCharType="separate"/></w:r>'
        f'<w:r>{_RPR}<w:t>1</w:t></w:r>'
        f'<w:r>{_RPR}<w:fldChar w:fldCharType="end"/></w:r>'
        "</w:p></w:ftr>"
    )
    return xml.encode("utf-8")


# --------------------------------------------------------------------------- #
# zip 读写（保持原条目顺序，新部件追加在末尾）
# --------------------------------------------------------------------------- #


def _load(content: bytes) -> tuple[list[str], dict[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        order = [info.filename for info in zf.infolist()]
        parts = {name: zf.read(name) for name in order}
    return order, parts


def _save(order: list[str], parts: dict[str, bytes]) -> bytes:
    remaining = dict(parts)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zo:
        for name in order:
            blob = remaining.pop(name, None)
            if blob is not None:
                zo.writestr(name, blob)
        for name, blob in remaining.items():
            zo.writestr(name, blob)
    return buf.getvalue()


def is_docx(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return _DOC in zf.namelist()
    except Exception:  # noqa: BLE001 非 zip / 损坏文件
        return False


# --------------------------------------------------------------------------- #
# rels / content-types 小工具
# --------------------------------------------------------------------------- #


def _rel_map(rels_text: str) -> dict[str, str]:
    """rId -> Target（原始相对路径，如 footer3.xml / media/image1.png）。"""
    if not rels_text:
        return {}
    m: dict[str, str] = {}
    for rid, target in re.findall(
        r'<Relationship\b[^>]*\bId="([^"]+)"[^>]*\bTarget="([^"]+)"', rels_text
    ):
        m[rid] = target
    # Id 与 Target 顺序可能相反，补一次反向匹配
    for target, rid in re.findall(
        r'<Relationship\b[^>]*\bTarget="([^"]+)"[^>]*\bId="([^"]+)"', rels_text
    ):
        m.setdefault(rid, target)
    return m


def _resolve(target: str | None) -> str:
    """rels 里的相对 Target → zip 内部件路径（word/ 前缀）。"""
    t = str(target or "")
    if not t:
        return ""
    if t.startswith("/"):
        return t.lstrip("/")
    return f"word/{t}"


def _next_rid(rel_map: dict[str, str]) -> str:
    used: set[int] = set()
    for key in rel_map:
        m = re.fullmatch(r"rId(\d+)", key)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"rId{n}"


def _add_relationship(parts: dict[str, bytes], rid: str, part: str) -> None:
    target = part.rsplit("/", 1)[-1]
    rels = parts.get(_RELS, b"").decode("utf-8")
    if not rels:
        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
            '<Relationships xmlns='
            '"http://schemas.openxmlformats.org/package/2006/relationships">'
            "</Relationships>"
        )
    rel = f'<Relationship Id="{rid}" Type="{_FOOTER_REL_TYPE}" Target="{target}"/>'
    for closing in ("</Relationships>", "</r:Relationships>"):
        if closing in rels:
            rels = rels.replace(closing, rel + closing, 1)
            break
    else:  # 兜底：结构异常时不做破坏性拼接
        return
    parts[_RELS] = rels.encode("utf-8")


def _ensure_content_type(parts: dict[str, bytes], part: str) -> None:
    ct = parts.get(_CT, b"").decode("utf-8")
    if not ct:
        return
    name = "/" + part
    if f'PartName="{name}"' in ct:
        return
    override = f'<Override PartName="{name}" ContentType="{_CT_FOOTER}"/>'
    parts[_CT] = ct.replace("</Types>", override + "</Types>", 1).encode("utf-8")


def _even_and_odd(parts: dict[str, bytes]) -> bool:
    return b"evenAndOddHeaders" in parts.get(_SETTINGS, b"")


# --------------------------------------------------------------------------- #
# 大纲级别噪声
# --------------------------------------------------------------------------- #


def _paragraph_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{_WQ}t"))


def _is_outline_noise(p, ol) -> bool:
    raw = ol.get(f"{_WQ}val")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = _MAX_OUTLINE_LEVEL + 1
    if val > _MAX_OUTLINE_LEVEL:
        return True
    # 题注型段落：无论级别高低都不该进大纲
    return bool(_CAPTION_RE.match(_paragraph_text(p)))


def strip_outline_noise(content: bytes) -> tuple[bytes, int]:
    """删除大纲级别噪声（`outlineLvl ≥ 6` + 题注型段落上的任何级别）。

    返回 (新内容, 清除条数)；无改动时原样返回。纯属性删除，正文字/图/表不动。
    """
    try:
        order, parts = _load(content)
    except Exception:  # noqa: BLE001
        return content, 0
    if _DOC not in parts:
        return content, 0
    root = etree.fromstring(parts[_DOC])
    removed = 0
    for p in root.iter(f"{_WQ}p"):
        ppr = p.find(f"{_WQ}pPr")
        if ppr is None:
            continue
        ol = ppr.find(f"{_WQ}outlineLvl")
        if ol is None:
            continue
        if _is_outline_noise(p, ol):
            ppr.remove(ol)
            removed += 1
    if not removed:
        return content, 0
    parts[_DOC] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    return _save(order, parts), removed


def _count_outline_noise(root) -> int:
    n = 0
    for p in root.iter(f"{_WQ}p"):
        ppr = p.find(f"{_WQ}pPr")
        if ppr is None:
            continue
        ol = ppr.find(f"{_WQ}outlineLvl")
        if ol is not None and _is_outline_noise(p, ol):
            n += 1
    return n


# --------------------------------------------------------------------------- #
# 页码页脚
# --------------------------------------------------------------------------- #


def _footer_refs(sectpr) -> list:
    return [c for c in sectpr if c.tag == f"{_WQ}footerReference"]


def _ref_index(sectpr) -> int:
    """footerReference 必须排在 headerReference 之后、其它元素之前。"""
    idx = 0
    for i, child in enumerate(sectpr):
        if child.tag in (f"{_WQ}headerReference", f"{_WQ}footerReference"):
            idx = i + 1
    return idx


def _make_ref(ref_type: str, rid: str):
    el = etree.Element(f"{_WQ}footerReference")
    el.set(f"{_WQ}type", ref_type)
    el.set(f"{_RQ}id", rid)
    return el


def ensure_page_footer(content: bytes) -> tuple[bytes, dict]:
    """保证每个 sectPr 都有指向「含 PAGE 域的页脚」的引用（模板口径）。

    - 已有合规引用 → 幂等返回原内容；
    - 有含 PAGE 的页脚部件但引用被删空（202 场景）→ 把引用接回去；
    - 没有任何页脚部件（整文件直写场景）→ 按模板口径注入新部件并挂到每个节；
    - `w:pgNumType w:start="0"`（模板封面节设置）→ 去掉，使首页从 1 起。
    """
    stats: dict = {
        "changed": False,
        "created_part": None,
        "refs_added": 0,
        "refs_relinked": 0,
        "pg_num_start0_removed": 0,
        "sections": 0,
    }
    try:
        order, parts = _load(content)
    except Exception:  # noqa: BLE001
        return content, stats
    if _DOC not in parts:
        return content, stats

    root = etree.fromstring(parts[_DOC])
    sectprs = list(root.iter(f"{_WQ}sectPr"))
    stats["sections"] = len(sectprs)
    if not sectprs:
        return content, stats

    rel_map = _rel_map(parts.get(_RELS, b"").decode("utf-8"))
    good = {
        name
        for name in parts
        if _FOOTER_PART_RE.match(name) and b"PAGE" in parts[name]
    }

    def good_ref_of(sectpr) -> str | None:
        for ref in _footer_refs(sectpr):
            if _resolve(rel_map.get(ref.get(f"{_RQ}id", ""))) in good:
                return ref.get(f"{_RQ}id")
        return None

    # 1) 选一个复用的「好页脚」：优先已被引用的，其次任意含 PAGE 的
    target_part: str | None = None
    for sp in sectprs:
        rid = good_ref_of(sp)
        if rid:
            target_part = _resolve(rel_map.get(rid))
            break
    if target_part is None and good:
        target_part = sorted(good)[0]

    # 2) 没有可用页脚 → 按模板口径新建部件
    if target_part is None:
        n = 1
        while f"word/footer{n}.xml" in parts:
            n += 1
        target_part = f"word/footer{n}.xml"
        parts[target_part] = _footer_xml()
        order.append(target_part)
        stats["created_part"] = target_part
        _ensure_content_type(parts, target_part)

    # 3) 找到（或补一条）指向该部件的 rels 关系
    rid = next(
        (k for k, v in rel_map.items() if _resolve(v) == target_part),
        None,
    )
    if rid is None:
        rid = _next_rid(rel_map)
        _add_relationship(parts, rid, target_part)
        rel_map[rid] = target_part.rsplit("/", 1)[-1]

    # 4) 每个节都挂上 default（+ 需要时 first / even），首页也要显示页码
    even = _even_and_odd(parts)
    for sp in sectprs:
        present = {(r.get(f"{_WQ}type") or "default"): r for r in _footer_refs(sp)}
        want = ["default"]
        if "first" in present or sp.find(f"{_WQ}titlePg") is not None:
            want.append("first")
        if even:
            want.append("even")
        for ref_type in want:
            cur = present.get(ref_type)
            if cur is None:
                sp.insert(_ref_index(sp), _make_ref(ref_type, rid))
                stats["refs_added"] += 1
            elif _resolve(rel_map.get(cur.get(f"{_RQ}id", ""))) not in good:
                cur.set(f"{_RQ}id", rid)
                stats["refs_relinked"] += 1
        # 模板封面节的 start=0 会让首页显示「0」，交付件一律从 1 起
        pgnum = sp.find(f"{_WQ}pgNumType")
        if pgnum is not None and pgnum.get(f"{_WQ}start") == "0":
            sp.remove(pgnum)
            stats["pg_num_start0_removed"] += 1

    stats["changed"] = bool(
        stats["created_part"]
        or stats["refs_added"]
        or stats["refs_relinked"]
        or stats["pg_num_start0_removed"]
    )
    if not stats["changed"]:
        return content, stats
    parts[_DOC] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    return _save(order, parts), stats


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def normalize_docx(content: bytes) -> tuple[bytes, dict]:
    """交付 docx 归一化：先清大纲噪声，再保证页码页脚。返回 (内容, 回执)。"""
    if not is_docx(content):
        return content, {"is_docx": False}
    out, noise = strip_outline_noise(content)
    out, foot = ensure_page_footer(out)
    return out, {"is_docx": True, "outline_noise_removed": noise, **foot}


def audit_docx(content: bytes) -> dict:
    """只读体检：打包门禁用（页码覆盖率 + 大纲噪声）。"""
    info: dict = {
        "is_docx": False,
        "sections": 0,
        "sections_without_page_footer": 0,
        "footer_parts": 0,
        "page_field_parts": 0,
        "footer_refs": 0,
        "has_page_footer": False,
        "outline_noise": 0,
    }
    if not is_docx(content):
        return info
    info["is_docx"] = True
    try:
        _, parts = _load(content)
        root = etree.fromstring(parts[_DOC])
    except Exception:  # noqa: BLE001 解析失败按无页码处理（门禁会拦）
        return info
    sectprs = list(root.iter(f"{_WQ}sectPr"))
    info["sections"] = len(sectprs)
    footer_parts = [n for n in parts if _FOOTER_PART_RE.match(n)]
    info["footer_parts"] = len(footer_parts)
    good = {n for n in footer_parts if b"PAGE" in parts[n]}
    info["page_field_parts"] = len(good)
    rel_map = _rel_map(parts.get(_RELS, b"").decode("utf-8"))
    refs = 0
    missing = 0
    for sp in sectprs:
        ok = False
        for ref in _footer_refs(sp):
            refs += 1
            if _resolve(rel_map.get(ref.get(f"{_RQ}id", ""))) in good:
                ok = True
        if not ok:
            missing += 1
    info["footer_refs"] = refs
    info["sections_without_page_footer"] = missing
    info["has_page_footer"] = bool(sectprs) and missing == 0
    info["outline_noise"] = _count_outline_noise(root)
    return info
