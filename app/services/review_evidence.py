"""评分输入构造：块索引 + 待核验要素清单 + 定向取文（issue #70）。

背景（项目 217 实测）：原实现把每份交付件截到 12,000 字、全局合计 30,000 字按 artifact 顺序
拼接，结果 156,695 字的技术专项响应文件只被读到 **7.7%**，价格四件更是一个字都没读到——
11 条"证据不足"里多数是"没读到"而不是"没写"。

本模块把"读标书"按「规则 → 待核验要素清单 → 分块索引 → 定向精读」重排：

1. `chunk_docx` / `chunk_xlsx`：按标题层级把交付件切成块（无标题长文按窗口切），
   保留"标题路径"便于定位与引用；
2. `build_checklists`：一次调用把全部评分规则拆成"可核验要素"清单
   （要素 / 需要的证据 / 检索线索 / 判定口径）；
3. `select_chunks`：用检索线索给块打分，取预算内的块；**零命中时**才启用兜底
   （把"目录 + 每块开头"交给 LLM 选块）；
4. `build_editorial_hints`：从《编制逻辑与评分响应记录》抽"评分点→响应位置"索引行与
   "未闭环/风险提示"段落——**仅作索引与提示，不得作为得分依据**。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from lxml import etree

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 单块目标长度：超过即按窗口继续切（保持标题路径，后缀"（续）"）
CHUNK_WINDOW_CHARS = 8000
# 每条规则的正文预算与目录预算（用户拍板：正文 4 万字 + 目录 5 千字）
RULE_BUDGET_CHARS = 40000
MAP_BUDGET_CHARS = 5000

_HEADING_STYLE_RE = re.compile(r"Heading(\d)")
_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\s*[、.．]?\s*(\S.{0,40})$")
_CHAPTER_RE = re.compile(r"^\s*[（(]?\s*[一二三四五六七八九十百]+\s*[）)、.]")


@dataclass
class Chunk:
    """交付件里的一个可定位块。"""

    index: int
    artifact_id: int
    file_name: str
    heading_path: str
    text: str
    part: int = 1  # 同一标题下的第几段窗口（长章节会被切开）
    keys: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def label(self) -> str:
        suffix = f"（续{self.part}）" if self.part > 1 else ""
        return f"{self.file_name} › {self.heading_path}{suffix}"


# --------------------------------------------------------------------------- #
# 分块
# --------------------------------------------------------------------------- #


def _paragraph_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{_W}t"))


def _heading_level(p) -> int | None:
    """标题级别（1 起）：outlineLvl / HeadingN 样式 / 编号型短标题三级判定。"""
    ppr = p.find(f"{_W}pPr")
    if ppr is not None:
        ol = ppr.find(f"{_W}outlineLvl")
        if ol is not None:
            try:
                return int(ol.get(f"{_W}val")) + 1
            except (TypeError, ValueError):
                pass
        ps = ppr.find(f"{_W}pStyle")
        if ps is not None:
            m = _HEADING_STYLE_RE.search(ps.get(f"{_W}val") or "")
            if m:
                return int(m.group(1))
    text = _paragraph_text(p).strip()
    if 0 < len(text) <= 40 and _NUMBERED_HEADING_RE.match(text):
        return 3
    return None


def _table_text(tbl) -> str:
    rows = []
    for tr in tbl.findall(f"{_W}tr"):
        cells = ["".join(t.text or "" for t in tc.iter(f"{_W}t")).strip() for tc in tr.findall(f"{_W}tc")]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _split_windows(text: str, window: int) -> list[str]:
    if len(text) <= window:
        return [text]
    out = []
    start = 0
    while start < len(text):
        out.append(text[start : start + window])
        start += window
    return out


def chunk_docx(data: bytes, artifact_id: int, file_name: str, counter: list[int]) -> list[Chunk]:
    """按标题层级把 docx 切成块；表格归入当前块；超长章节按窗口续切。"""
    from docx import Document

    doc = Document(io.BytesIO(data))
    body = doc.element.body
    chunks: list[Chunk] = []
    stack: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(x for x in buffer if x.strip())
        buffer.clear()
        if not text.strip():
            return
        path = " / ".join(stack) or "（正文）"
        for part, window in enumerate(_split_windows(text, CHUNK_WINDOW_CHARS), start=1):
            counter[0] += 1
            chunks.append(
                Chunk(
                    index=counter[0],
                    artifact_id=artifact_id,
                    file_name=file_name,
                    heading_path=path,
                    text=window,
                    part=part,
                )
            )

    for child in body.iterchildren():
        if child.tag == f"{_W}p":
            level = _heading_level(child)
            text = _paragraph_text(child).strip()
            if level is not None and text:
                flush()
                stack = stack[: max(level - 1, 0)]
                stack.append(text)
                continue
            if text:
                buffer.append(text)
        elif child.tag == f"{_W}tbl":
            text = _table_text(child)
            if text:
                buffer.append(text)
    flush()
    return chunks


def chunk_xlsx(data: bytes, artifact_id: int, file_name: str, counter: list[int]) -> list[Chunk]:
    """xlsx 体量小，按 sheet 切块。"""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    chunks: list[Chunk] = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            vals = ["" if v is None else str(v) for v in row]
            if any(v.strip() for v in vals):
                rows.append(" | ".join(vals))
        text = "\n".join(rows).strip()
        if text:
            counter[0] += 1
            chunks.append(
                Chunk(
                    index=counter[0],
                    artifact_id=artifact_id,
                    file_name=file_name,
                    heading_path=f"工作表 {ws.title}",
                    text=text,
                )
            )
    wb.close()
    return chunks


def build_chunks(artifacts: list[dict]) -> list[Chunk]:
    """把 `_artifact_texts` 形态的产物列表切成块（docx 按标题、xlsx 按 sheet）。"""
    counter = [0]
    chunks: list[Chunk] = []
    for a in artifacts:
        data = a.get("content") or b""
        kind = a.get("kind")
        try:
            if kind == "xlsx":
                chunks.extend(chunk_xlsx(data, int(a["artifact_id"]), a["name"], counter))
            else:
                chunks.extend(chunk_docx(data, int(a["artifact_id"]), a["name"], counter))
        except Exception:  # noqa: BLE001 单份不可读不影响其它文件（评审层会如实记录）
            continue
    return chunks


# --------------------------------------------------------------------------- #
# 检索与取文
# --------------------------------------------------------------------------- #


def _normalize_keys(keys: list[str]) -> list[str]:
    out = []
    for k in keys or []:
        s = str(k or "").strip()
        if len(s) >= 2 and s not in out:
            out.append(s)
    return out[:40]


def chunk_map(chunks: list[Chunk], budget: int = MAP_BUDGET_CHARS) -> str:
    """交付件总目录：标题路径 + 每块开头，用于给 LLM 全局视野（≤ budget 字）。"""
    lines: list[str] = []
    used = 0
    for c in chunks:
        head = c.text.strip().replace("\n", " ")[:120]
        line = f"[块{c.index}] {c.label}（{c.chars}字）：{head}"
        if used + len(line) > budget:
            lines.append(f"…（其余 {len(chunks) - len(lines)} 块略，可按需检索）")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def select_chunks(
    checklist: list[dict],
    chunks: list[Chunk],
    budget: int = RULE_BUDGET_CHARS,
) -> tuple[list[Chunk], dict]:
    """按要素检索线索给块打分取文；返回 (选中块, 覆盖情况)。"""
    keys = _normalize_keys([k for e in checklist for k in (e.get("keys") or [])])
    scored: list[tuple[int, int, Chunk]] = []
    for c in chunks:
        score = 0
        for k in keys:
            score += 3 * c.heading_path.count(k) + c.text.count(k)
        if score:
            scored.append((score, -c.index, c))
    scored.sort(reverse=True)
    selected: list[Chunk] = []
    used = 0
    for _score, _neg, c in scored:
        if used + c.chars <= budget:
            selected.append(c)
            used += c.chars
        elif used < budget and not selected:
            # 单块就超预算：截断保留开头，避免整条规则拿不到内容
            selected.append(
                Chunk(
                    index=c.index,
                    artifact_id=c.artifact_id,
                    file_name=c.file_name,
                    heading_path=c.heading_path + "（截断）",
                    text=c.text[:budget],
                    part=c.part,
                )
            )
            used = budget
    selected.sort(key=lambda c: c.index)
    scope = {
        "chunks_total": len(chunks),
        "chunks_matched": len(scored),
        "chunks_provided": len(selected),
        "chars_total": sum(c.chars for c in chunks),
        "chars_provided": used,
        "fallback": False,
        "scope_limited": len(scored) < len(chunks) and used >= budget,
    }
    return selected, scope


def render_chunks(selected: list[Chunk]) -> str:
    return "\n\n".join(f"【块{c.index}｜{c.label}】\n{c.text}" for c in selected)


def chunks_to_prompt(selected: list[Chunk]) -> str:
    return render_chunks(selected)


# --------------------------------------------------------------------------- #
# 待核验要素清单（LLM）
# --------------------------------------------------------------------------- #

_CHECKLIST_SYSTEM = (
    "你是资深招投标评审专家。下面是招标文件里的若干条评分规则，请把**每一条**拆成"
    "\u201c必须核验的要素清单\u201d，用于指导后续读投标文件取证。\n"
    "要求：\n"
    "1. 要素必须能在投标文件里查到具体事实或证据（谁／什么／多少／哪份证明），"
    "不要写成\u201c方案是否优秀\u201d这类无法核验的话；\n"
    "2. 每个要素给四样东西：element（要素，≤30 字）、evidence（需要什么证据或材料）、"
    "keys（用于在标书里定位的检索线索词 2-6 个，用投标文件里可能出现的措辞）、"
    "criteria（该要素达标口径，按规则档位写）；\n"
    "3. 每条规则 2-8 个要素，覆盖该规则全部得分要点；扣分型条款写明\u201c不触发即不得分也不扣分\u201d；\n"
    "4. 只输出严格 JSON，不要 Markdown 围栏、不要解释文字，格式：\n"
    '{"checklists":[{"rule_index":0,"elements":[{"element":"…","evidence":"…",'
    '"keys":["…"],"criteria":"…"}]}]}'
)


def _rule_prompt_lines(rules: list[dict]) -> str:
    lines = []
    for i, r in enumerate(rules):
        lines.append(
            f"[rule_index={i}] 分类：{r.get('category')}｜满分：{r.get('weight')} 分｜"
            f"规则：{r.get('content')}"
        )
    return "\n".join(lines)


async def build_checklists(rules: list[dict]) -> dict[int, list[dict]]:
    """一次 LLM 调用生成全部规则的要素清单；解析失败返回空（调用方走原逻辑兜底）。"""
    from app.services.llm import LLMClient, try_extract_json

    if not rules:
        return {}
    reply = await LLMClient().chat(_CHECKLIST_SYSTEM, _rule_prompt_lines(rules))
    parsed = try_extract_json(reply)
    out: dict[int, list[dict]] = {}
    if not isinstance(parsed, dict):
        return out
    for item in parsed.get("checklists") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("rule_index"))
        except (TypeError, ValueError):
            continue
        elements = []
        for e in item.get("elements") or []:
            if not isinstance(e, dict) or not str(e.get("element") or "").strip():
                continue
            elements.append(
                {
                    "element": str(e.get("element"))[:60],
                    "evidence": str(e.get("evidence") or "")[:120],
                    "keys": [str(k)[:40] for k in (e.get("keys") or []) if str(k).strip()][:6],
                    "criteria": str(e.get("criteria") or "")[:120],
                }
            )
        if elements:
            out[idx] = elements[:8]
    return out


async def pick_chunks_by_llm(checklist: list[dict], chunks: list[Chunk], limit: int = 12) -> list[int]:
    """兜底：把目录交给 LLM 选块（只在关键词零命中时调用）。返回块号列表。"""
    from app.services.llm import LLMClient, try_extract_json

    if not chunks:
        return []
    system = (
        "你是投标文件检索助手。给定评分规则的核验要素与投标文件分块目录，"
        "选出最可能包含相关证据的块号。只输出严格 JSON：{\"chunks\":[块号,…]}，最多 "
        f"{limit} 个；找不到就返回空数组。"
    )
    user = (
        f"核验要素：{[e.get('element') for e in checklist]}\n"
        f"检索线索：{_normalize_keys([k for e in checklist for k in (e.get('keys') or [])])}\n\n"
        f"投标文件分块目录：\n{chunk_map(chunks, budget=12000)}"
    )
    reply = await LLMClient().chat(system, user)
    parsed = try_extract_json(reply)
    if not isinstance(parsed, dict):
        return []
    valid = {c.index for c in chunks}
    return [i for i in (parsed.get("chunks") or []) if isinstance(i, int) and i in valid][:limit]


def select_by_index(chunks: list[Chunk], indexes: list[int], budget: int = RULE_BUDGET_CHARS) -> list[Chunk]:
    wanted = {c.index: c for c in chunks if c.index in set(indexes)}
    selected: list[Chunk] = []
    used = 0
    for idx in sorted(wanted):
        c = wanted[idx]
        if used + c.chars <= budget:
            selected.append(c)
            used += c.chars
    return selected


# --------------------------------------------------------------------------- #
# 《编制逻辑与评分响应记录》：索引 + 风险提示（不得作为得分依据）
# --------------------------------------------------------------------------- #

_EDITORIAL_NAME_HINT = "编制逻辑与评分响应记录"
_RISK_HEADINGS = ("未闭环", "风险提示", "回修记录", "验收回修")


def is_editorial(name: str) -> bool:
    return _EDITORIAL_NAME_HINT in str(name or "")


def build_editorial_hints(data: bytes) -> dict:
    """抽取编制方自述的索引行与风险提示（仅作定位与提示）。"""
    from docx import Document

    doc = Document(io.BytesIO(data))
    matrix: list[list[str]] = []
    for tb in doc.tables:
        rows = [[c.text.strip() for c in row.cells] for row in tb.rows]
        if rows and any("评分点" in c for c in rows[0]):
            matrix = rows
            break
    risk: list[str] = []
    capture = False
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if not t:
            continue
        if any(h in t for h in _RISK_HEADINGS) and len(t) <= 30:
            capture = True
            risk.append(t)
            continue
        if capture:
            if _CHAPTER_RE.match(t) and len(t) <= 30 and not t.startswith(("1.", "2.", "3.", "4.", "5.")):
                capture = False
                continue
            risk.append(t)
    return {"matrix": matrix, "risk": "\n".join(risk)[:4000]}


def editorial_block(hints: dict, rule_text: str, keys: list[str]) -> str:
    """把与当前规则相关的索引行 + 全局风险提示拼成提示块。"""
    if not hints:
        return ""
    rows = []
    for row in hints.get("matrix") or []:
        joined = " ".join(row)
        if any(k and k in joined for k in keys) or any(
            w in joined for w in str(rule_text)[:20].split("、")
        ):
            rows.append(" | ".join(row))
    parts = []
    if rows:
        parts.append("编制方自述的装订索引（评分点｜分值｜证明材料｜响应位置｜预计得分）：\n" + "\n".join(rows[:6]))
    if hints.get("risk"):
        parts.append("编制方自述的风险提示／回修记录：\n" + hints["risk"])
    return "\n".join(parts)
