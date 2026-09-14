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
    extra_keys: list[str] | None = None,
) -> tuple[list[Chunk], dict]:
    """按检索线索给块打分取文；返回 (选中块, 覆盖情况)。

    `extra_keys` 用于补充线索（如规则原文里的词）——要素清单缺失时也能检索，
    避免"清单没生成 → 一个块都取不到 → 全部判证据不足"。
    """
    keys = _normalize_keys(
        [k for e in checklist for k in (e.get("keys") or [])] + list(extra_keys or [])
    )
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


def keys_from_rule_text(text: str, limit: int = 12) -> list[str]:
    """从规则原文抽检索线索（兜底用）：取 2-8 字的中文/英文片段。"""
    out: list[str] = []
    for term in re.split(r"[、，,。；;：:（）()【】\[\]\s/]+", str(text or "")):
        term = term.strip()
        term = re.sub(r"^(优|良|一般|得|加|扣|分|每|项|最高|\d+)+", "", term)
        term = re.sub(r"(分|元|人|项|个)$", "", term)
        if 2 <= len(term) <= 12 and term not in out:
            out.append(term)
        if len(out) >= limit:
            break
    return out


def fallback_chunks_by_category(
    category: str, chunks: list[Chunk], budget: int = RULE_BUDGET_CHARS
) -> list[Chunk]:
    """最后兜底：按评分类别取"该类别对应文件"的前若干块，保证模型至少有相关正文可读。"""
    prefer = {
        "技术": ("技术文件/", "内部管理文件/"),
        "商务": ("商务文件/", "内部管理文件/"),
        "价格": ("价格文件/", "内部管理文件/"),
    }.get(str(category or "").strip(), ("技术文件/", "商务文件/", "内部管理文件/"))
    picked: list[Chunk] = []
    used = 0
    for prefix in prefer:
        for c in chunks:
            if not c.file_name.startswith(prefix):
                continue
            if used + c.chars > budget:
                break
            picked.append(c)
            used += c.chars
    return picked


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
    "4. 另外把该规则的**档位表**按原文抄成 tiers（没有明确档位就给空数组），每档给："
    "label（档位名，如 优/良/一般、A/B/C/D/E、≥30人、<15人、未参加绩效评价、每项加分）、"
    "condition（触发条件原文）、min/max（该档对应的分值区间，单个分值则 min=max；无法确定填 null）；\n"
    "5. 只输出严格 JSON，不要 Markdown 围栏、不要解释文字，格式：\n"
    '{"checklists":[{"rule_index":0,"elements":[{"element":"…","evidence":"…",'
    '"keys":["…"],"criteria":"…"}],"tiers":[{"label":"…","condition":"…",'
    '"min":0,"max":0}]}]}'
)


def _rule_prompt_lines(rules: list[dict], offset: int = 0) -> str:
    lines = []
    for i, r in enumerate(rules):
        lines.append(
            f"[rule_index={offset + i}] 分类：{r.get('category')}｜满分：{r.get('weight')} 分｜"
            f"规则：{r.get('content')}"
        )
    return "\n".join(lines)


def _coerce_num(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_checklists(reply: str) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """解析 LLM 回执 → (要素清单, 档位表)。"""
    from app.services.llm import LLMClient, try_extract_json

    parsed = try_extract_json(reply)
    elements_out: dict[int, list[dict]] = {}
    tiers_out: dict[int, list[dict]] = {}
    if not isinstance(parsed, dict):
        return elements_out, tiers_out
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
            elements_out[idx] = elements[:8]
        tiers = []
        for t in item.get("tiers") or []:
            if not isinstance(t, dict):
                continue
            label = str(t.get("label") or "").strip()
            lo, hi = _coerce_num(t.get("min")), _coerce_num(t.get("max"))
            if lo is None and hi is not None:
                lo = hi
            if hi is None and lo is not None:
                hi = lo
            if not label and lo is None:
                continue
            tiers.append(
                {
                    "label": label[:30],
                    "condition": str(t.get("condition") or "")[:60],
                    "min": lo,
                    "max": hi,
                }
            )
        if tiers:
            tiers_out[idx] = tiers[:8]
    return elements_out, tiers_out


def _synthetic_checklist(rule: dict) -> list[dict]:
    """清单生成失败时的兜底：用规则原文与关键词造一条"整体核验"要素，保证不为空。"""
    keys = keys_from_rule_text(rule.get("content") or "")
    return [
        {
            "element": str(rule.get("content") or "")[:60],
            "evidence": "按规则原文逐项核验投标文件中的对应事实与证明材料",
            "keys": keys,
            "criteria": f"按满分 {rule.get('weight')} 分与规则原文档位判定",
            "synthetic": True,
        }
    ]


async def build_plan(
    rules: list[dict],
    batch_size: int = 5,
    retry_missing: int = 99,
) -> dict:
    """生成规则的「待核验要素清单 + 档位表」（issue #70 / #71）。

    分批调用（默认每批 5 条）以提高 JSON 解析成功率；批内漏掉的规则**逐条再试两轮**；
    仍拿不到就用规则原文造一条兜底要素（`synthetic=True`）——保证**覆盖率 100%**，
    不再出现"清单为空 → 检索零命中 → 大面积误判证据不足"的回归。
    返回 {"elements": {i: [...]}, "tiers": {i: [...]}, "coverage": {...}}。
    """
    from app.services.llm import LLMClient

    elements: dict[int, list[dict]] = {}
    tiers: dict[int, list[dict]] = {}
    if not rules:
        return {"elements": elements, "tiers": tiers, "coverage": {"rules": 0}}
    for start in range(0, len(rules), batch_size):
        batch = rules[start : start + batch_size]
        try:
            reply = await LLMClient().chat(
                _CHECKLIST_SYSTEM, _rule_prompt_lines(batch, offset=start)
            )
        except Exception:  # noqa: BLE001 单批失败不阻塞，交由逐条补齐
            continue
        els, tls = _parse_checklists(reply)
        elements.update(els)
        tiers.update(tls)

    # 逐条重试（两轮），仍失败则用规则原文兜底
    for _round in range(2):
        missing = [i for i in range(len(rules)) if i not in elements][
            : max(retry_missing, 0)
        ]
        if not missing:
            break
        for i in missing:
            try:
                reply = await LLMClient().chat(
                    _CHECKLIST_SYSTEM, _rule_prompt_lines([rules[i]], offset=i)
                )
            except Exception:  # noqa: BLE001
                continue
            els, tls = _parse_checklists(reply)
            elements.update(els)
            tiers.update(tls)

    synthetic = 0
    for i, rule in enumerate(rules):
        if i not in elements:
            elements[i] = _synthetic_checklist(rule)
            synthetic += 1
    coverage = {
        "rules": len(rules),
        "elements": sum(len(v) for v in elements.values()),
        "tiers": sum(len(v) for v in tiers.values()),
        "synthetic": synthetic,
        "rules_without_tiers": [i for i in range(len(rules)) if not tiers.get(i)],
    }
    return {"elements": elements, "tiers": tiers, "coverage": coverage}


async def build_checklists(rules: list[dict], batch_size: int = 5) -> dict[int, list[dict]]:
    """兼容旧调用：只返回要素清单。"""
    plan = await build_plan(rules, batch_size=batch_size)
    return plan["elements"]


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
# 档位一致性自检（issue #71）
# --------------------------------------------------------------------------- #


def _match_tier(label: str, tiers: list[dict]) -> dict | None:
    if not label:
        return None
    key = label.strip()
    for t in tiers:
        if str(t.get("label") or "").strip() == key:
            return t
    for t in tiers:
        tl = str(t.get("label") or "").strip()
        if tl and (tl in key or key in tl):
            return t
    return None


_THRESHOLD_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:人|项|份|个|年|次|%|万元|分)")
_THRESHOLD_TIER_RE = re.compile(r"[≥≤<>＜＞]|不少于|不足|超过|达到|\d+\s*(?:人|项|份|个|年|次)")


def _evidence_blob(elements: list | None) -> str:
    if not elements:
        return ""
    parts = []
    for e in elements:
        if isinstance(e, dict):
            parts.append(str(e.get("quote") or ""))
            parts.append(str(e.get("note") or ""))
    return " ".join(parts).replace(" ", "").replace("\n", "")


def tier_by_evidence(tiers: list[dict], elements: list | None) -> dict | None:
    """证据驱动定档：若要素引用里只指向**唯一**一个档位（按阈值词/档位名），以它为准。"""
    blob = _evidence_blob(elements)
    if not blob:
        return None
    cands: list[dict] = []
    for t in tiers:
        if _tier_supported_by_evidence(t, blob):
            cands.append(t)
    return cands[0] if len(cands) == 1 else None


_NEG_THRESHOLD_RE = re.compile(r"(不足|少于|低于|未达|未达到|未满|<|＜)\s*(\d+)")
_POS_THRESHOLD_RE = re.compile(r"(不少于|达到|超过|≥|不低于|大于)\s*(\d+)")
_TIER_DIRECTION_RE = re.compile(r"([≥≤<>＜＞]|不少于|不足|超过|达到|低于)\s*(\d+)")


def _tier_supported_by_evidence(tier: dict, blob: str) -> bool:
    """证据是否支持该档位：支持"条件文本命中"与**阈值极性**匹配。

    实测坑（run 256）：规则档位是 ≥30人/≥15人/<15人，证据写"不足 15 人"——
    单纯按"15人"做子串匹配会同时命中 ≥15人 与 <15人 两档而判为歧义，
    必须识别"不足/少于/未达"的否定极性，把它归到 <15人 档。
    """
    text = f"{tier.get('label') or ''} {tier.get('condition') or ''}".replace(" ", "")
    if not text:
        return False
    dirs = _TIER_DIRECTION_RE.findall(text)
    if dirs:
        # 有方向词（≥/≤/< /不少于/不足…）时**只认极性**，否则"15人"这类 token 会同时命中两档
        for sign, num in dirs:
            if sign in ("≥", "不少于", "超过", "达到", "不低于"):
                if re.search(rf"(≥|不少于|达到|超过|不低于)\s*{num}", blob):
                    return True
            elif sign in ("<", "＜", "不足", "低于"):
                if re.search(rf"(不足|少于|低于|未达|未达到|未满|<|＜)\s*{num}", blob):
                    return True
            elif sign == "≤":
                if re.search(rf"(≤|不足|少于|不超过)\s*{num}", blob):
                    return True
        return False
    # 无方向词（如"未参加绩效评价得4分"）：按档位名 / 条件文本 / 带单位的数值 token 命中
    if tier.get("label") and len(str(tier["label"])) >= 2 and str(tier["label"]).replace(" ", "") in blob:
        return True
    if text in blob:
        return True
    toks = [x.replace(" ", "") for x in _THRESHOLD_TOKEN_RE.findall(text)]
    return bool(toks) and any(tok in blob for tok in toks)


def resolve_tier_conflict(tiers: list[dict], elements: list | None, current) -> dict:
    """重问仍未收敛时的**保守裁决**（issue #71 补丁）：

    按证据支持的档位取值（极性匹配；多个候选取最低档），且**绝不抬高当前得分**。
    """
    blob = _evidence_blob(elements)
    if not blob:
        return {}
    cands = [t for t in tiers if _tier_supported_by_evidence(t, blob) and t.get("min") is not None]
    if not cands:
        return {}
    tier = min(cands, key=lambda t: float(t["min"]))
    target = float(tier["min"])
    cur = _num_local(current)
    if cur is not None and target > cur:
        return {}
    return {
        "applied": True,
        "to": target,
        "reason": f"档位与证据矛盾且重问未收敛：按证据支持的档位「{tier.get('label')}」保守定分",
    }


def _num_local(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_threshold_tier(tier: dict) -> bool:
    text = f"{tier.get('label') or ''} {tier.get('condition') or ''}"
    return bool(_THRESHOLD_TIER_RE.search(text))


def apply_tier_consistency(parsed: dict, tiers: list[dict]) -> dict:
    """按档位表校正得分（issue #71：实测出现"证据到了、档位算错"且方向是多给分）。

    返回 `{applied, from, to, reason, need_reask}`：
    - **证据优先**：要素引用只指向唯一档位时以该档为准（实测：证据写"未参加绩效评价"，
      模型却给了 A 档 5 分）；
    - 档位是固定分值而 got 不等 → 校正到该档分值；区间档位越界 → 夹到区间内；
    - **门槛型档位**（含 ≥/≤/不少于/N人 等）但要素核验存在 miss → 判为档位可疑，
      `need_reask=True`（实测：高职称人数档位 ≥30 人给 3 分，但要素引用写明"不足 15 人"）；
    - 未给档位或档位对不上，且 got 不落在任何档位区间 → `need_reask=True`。
    """
    got = parsed.get("got")
    if got is None or not tiers:
        return {}
    try:
        got = float(got)
    except (TypeError, ValueError):
        return {}
    elements = parsed.get("element_results") or []
    selected = _match_tier(str(parsed.get("selected_tier") or ""), tiers)
    evidence_tier = tier_by_evidence(tiers, elements)
    tier = evidence_tier or selected
    misses = [
        e for e in elements if isinstance(e, dict) and e.get("result") == "miss"
    ]
    if tier is not None and _is_threshold_tier(tier) and misses:
        return {
            "need_reask": True,
            "reason": (
                f"所选档位「{tier.get('label')}」属数量/门槛型，但要素核验有 {len(misses)} 项未命中"
                f"（如「{misses[0].get('element')}」{('- ' + str(misses[0].get('quote'))[:40]) if misses[0].get('quote') else ''}）"
                "——请按档位表重新定档，不要越过未命中的门槛给高分。"
            ),
        }
    if tier is None:
        hits = [
            t
            for t in tiers
            if t.get("min") is not None
            and t.get("max") is not None
            and float(t["min"]) <= got <= float(t["max"])
        ]
        if len(hits) == 1:
            return {}
        return {
            "need_reask": True,
            "reason": f"得分 {got} 与档位表不一致（所选档位：{parsed.get('selected_tier') or '未给出'}）",
        }
    lo, hi = tier.get("min"), tier.get("max")
    if lo is None or hi is None:
        return {}
    lo, hi = float(lo), float(hi)
    # 扣分型条款（"扣5分/扣10分/扣20分"）的"档位"是**扣分值**，不是得分档；
    # 这类规则满分本就是 0，不参与得分校正（run 257 实测：曾把 0 写成 -10/-20）。
    if hi <= 0:
        return {}
    # **自检只做保守方向：绝不抬高得分**（run 256 实测：模型选了"获奖"档但实际未获奖，
    # 若按档位把 0 分改成 2 分就是虚增）。档位分值高于当前得分时，要求重问而不是直接改分。
    if got < lo - 1e-9:
        return {
            "need_reask": True,
            "reason": (
                f"所选档位「{tier.get('label')}」分值为 {lo}-{hi}，高于当前得分 {got}；"
                "档位自检不抬高得分，请核对档位与证据后重新定分。"
            ),
        }
    if lo == hi:
        if abs(got - lo) < 1e-6:
            return {}
        return {
            "applied": True,
            "from": got,
            "to": lo,
            "reason": f"所选档位「{tier.get('label')}」固定为 {lo} 分",
        }
    if lo <= got <= hi:
        return {}
    to = min(max(got, lo), hi)
    return {
        "applied": True,
        "from": got,
        "to": to,
        "reason": f"所选档位「{tier.get('label')}」分值区间为 {lo}-{hi}",
    }


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
