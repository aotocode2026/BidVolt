"""评分接口的展示层契约（issue #72）。

背景：前端要回答三件事——①每个条目是否已到最好；②哪些还能提分、提多少；③哪些要补材料。
改造前这些都要前端用 `verdict + got/full` 自己推断，已知三个坑：
`satisfied` 且 `full=0`（扣分型条款）、`not_applicable`（价格参考项）、`full=null`（未评）。
另外 `action_type` 的 `manual_review` 是兜底值（满分项与参考项都落它头上）；
综合分 `total_score` 的分母只含"能评条目"、价格 30% 权重不参与，**不宜直接展示**
（产品决定：先只展示分项）；`chunks_provided` 全量返回会让明细接口随规则细化膨胀。

本模块只做展示层映射与汇总，不改变评分结果本身：
- `score_state`：四态（已满分 / 可提升 / 未评 / 不参与计分）；
- `effective_action_type`：语义化动作（none / reference / upload_material /
  edit_deliverable / manual_review），并把历史值就地映射；
- `item_payload`：条目统一序列化（含 `improvable_percent`、`is_scored`、chunks 收敛）；
- `summarize_items` / `score_breakdown` / `price_scoring`：汇总层三张清单与口径说明。
"""

from __future__ import annotations

import re

# 条目四态
STATE_FULL = "full"
STATE_IMPROVABLE = "improvable"
STATE_UNRATED = "unrated"
STATE_NOT_SCORED = "not_scored"
SCORE_STATES = (STATE_FULL, STATE_IMPROVABLE, STATE_UNRATED, STATE_NOT_SCORED)

# 语义化动作
ACTION_NONE = "none"
ACTION_REFERENCE = "reference"
ACTION_UPLOAD = "upload_material"
ACTION_EDIT = "edit_deliverable"
ACTION_MANUAL = "manual_review"

STATE_LABELS = {
    STATE_FULL: "已达最好（满分/无扣分）",
    STATE_IMPROVABLE: "还能提分",
    STATE_UNRATED: "未评（缺证据，补材料后可判）",
    STATE_NOT_SCORED: "不参与计分（参考项/无分值条款）",
}
ACTION_LABELS = {
    ACTION_NONE: "无需动作",
    ACTION_REFERENCE: "参考项（不参与计分）",
    ACTION_UPLOAD: "补充材料",
    ACTION_EDIT: "修改正文",
    ACTION_MANUAL: "需人工判断",
}

_TOTAL_SCORE_NOTE = (
    "综合分暂不对外展示：分母只含已评条目、价格分待开标后按公式计算，"
    "按现口径相加会失真；请以分项（类别）得分与提分清单为准。"
)
# 供接口直接引用的公开文案
TOTAL_SCORE_NOTE_PUBLIC = _TOTAL_SCORE_NOTE


def _num(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def score_state(verdict: str | None, full, got) -> str:
    """条目四态判定（先判不参与计分，再判未评，最后按 got/full 分满分与可提升）。"""
    v = str(verdict or "").strip()
    f = _num(full)
    g = _num(got)
    if v == "not_applicable":
        return STATE_NOT_SCORED
    if v == "insufficient_evidence" or (g is None and v in ("", "unknown")):
        return STATE_UNRATED
    if not f:  # full 为 None 或 0：无分值条款（如扣分型条款）不参与计分
        return STATE_NOT_SCORED
    if g is None:
        return STATE_UNRATED
    return STATE_FULL if g >= f - 1e-9 else STATE_IMPROVABLE


def effective_action_type(raw: str | None, verdict: str | None, full, got) -> str:
    """语义化动作；历史值（manual_review 兜底）在此就地映射，前端无需兼容旧语义。"""
    r = str(raw or "").strip()
    state = score_state(verdict, full, got)
    if state == STATE_NOT_SCORED:
        return ACTION_REFERENCE
    if state == STATE_FULL:
        return ACTION_NONE
    if state == STATE_UNRATED:
        return ACTION_UPLOAD if r == ACTION_UPLOAD else ACTION_MANUAL
    # 还能提分：只区分"补材料"与"改正文"（历史 manual_review 兜底值一律按改正文处理）
    return ACTION_UPLOAD if r == ACTION_UPLOAD else ACTION_EDIT


def derive_action_type(verdict: str | None, full, has_missing_materials: bool) -> str:
    """评分落库时写入的语义化动作（与读时映射 `effective_action_type` 保持一致）。"""
    v = str(verdict or "").strip()
    if v == "not_applicable":
        return ACTION_REFERENCE
    if v == "satisfied":
        # full=0 的"扣分型条款"未触发扣分：不产生任何动作
        return ACTION_NONE if full else ACTION_REFERENCE
    if has_missing_materials:
        return ACTION_UPLOAD
    if v in ("unsatisfied", "partial"):
        return ACTION_EDIT
    return ACTION_MANUAL


def _evidence_payload(evidence: dict | None, include_chunks: bool) -> dict:
    """evidence 序列化：默认把 chunks_provided 收敛为摘要（issue #72）。"""
    ev = dict(evidence or {})
    chunks = ev.pop("chunks_provided", None) or []
    ev["chunks_summary"] = {
        "count": len(chunks),
        "chars": sum(int(c.get("chars") or 0) for c in chunks if isinstance(c, dict)),
        "head": [c.get("label") for c in chunks[:3] if isinstance(c, dict)],
    }
    if include_chunks:
        ev["chunks_provided"] = chunks
    return ev


def item_payload(item, include: frozenset[str] | set[str] | None = None, include_chunks: bool = False) -> dict:
    """条目统一序列化（/scores/{id}/items 与 /reviews/{run_id} 共用，避免字段漂移）。

    默认收敛（issue #72）：`evidence.chunks_provided` → `chunks_summary`；
    `response_source.files`（每条的产物清单，与 /scores 的 `scored_artifacts` 重复，实测占 19% 体积）
    仅在 `include=sources` 时返回，默认只给 `quote`。
    """
    inc = set(include or ())
    if include_chunks:
        inc.add("chunks")
    state = score_state(item.verdict, item.full, item.got)
    full = _num(item.full)
    got = _num(item.got)
    improvable = _num(item.improvable)
    response_source = item.response_source or {}
    if "sources" not in inc and isinstance(response_source, dict):
        response_source = {"quote": response_source.get("quote")}
    return {
        "item_id": item.id,
        "requirement_id": item.requirement_id,
        "criterion_id": item.criterion_id,
        "ruleset_version": item.ruleset_version,
        "category": item.category,
        "problem_description": item.problem_description,
        "got": got,
        "full": full,
        "improvable": improvable,
        "improvable_percent": (
            round(improvable / full * 100, 2) if improvable is not None and full else None
        ),
        "is_scored": bool(full),
        "score_state": state,
        "score_state_label": STATE_LABELS[state],
        "risk_level": item.risk_level,
        "suggestion": item.suggestion,
        "suggestion_override": item.suggestion_override,
        "effective_suggestion": item.suggestion_override or item.suggestion,
        "action_type": effective_action_type(item.action_type, item.verdict, item.full, item.got),
        "action_type_raw": item.action_type,
        "action_type_label": ACTION_LABELS[
            effective_action_type(item.action_type, item.verdict, item.full, item.got)
        ],
        "evidence": _evidence_payload(item.evidence, "chunks" in inc),
        "verdict": item.verdict,
        "deduction_reason": item.deduction_reason,
        "rule_source": item.rule_source,
        "response_source": response_source,
        "missing_materials": item.missing_materials,
        "status": item.status,
    }


def summarize_items(items) -> dict:
    """三张清单 + 提分合计（前端一次拿全，不必自己按状态/动作分组）。"""
    by_state: dict[str, list[int]] = {s: [] for s in SCORE_STATES}
    by_action: dict[str, list[int]] = {}
    improvable_total = 0.0
    for i in items:
        state = score_state(i.verdict, i.full, i.got)
        by_state[state].append(int(i.id))
        action = effective_action_type(i.action_type, i.verdict, i.full, i.got)
        by_action.setdefault(action, []).append(int(i.id))
        if state == STATE_IMPROVABLE and _num(i.improvable) is not None:
            improvable_total += _num(i.improvable) or 0.0
    return {
        "items_by_state": by_state,
        "items_by_action": by_action,
        "state_labels": STATE_LABELS,
        "action_labels": ACTION_LABELS,
        "improvable_total": round(improvable_total, 2),
        "improvable_count": len(by_state[STATE_IMPROVABLE]),
        "unrated_count": len(by_state[STATE_UNRATED]),
        "not_scored_count": len(by_state[STATE_NOT_SCORED]),
        "full_count": len(by_state[STATE_FULL]),
        "unrated_note": (
            f"另有 {len(by_state[STATE_UNRATED])} 条待评：补材料后才能判断可提升空间"
            if by_state[STATE_UNRATED]
            else None
        ),
    }


def score_breakdown(detail: dict | None, items) -> dict:
    """按类别汇总 + 综合分口径说明（产品决定：综合分暂不对外展示）。"""
    weights = (detail or {}).get("weight_config") or {}
    cats: dict[str, dict] = {}
    for i in items:
        c = cats.setdefault(
            str(i.category or "未分类"),
            {"got": 0.0, "full": 0.0, "scored_count": 0, "unrated": 0, "not_scored": 0},
        )
        state = score_state(i.verdict, i.full, i.got)
        if state in (STATE_FULL, STATE_IMPROVABLE) and i.full and i.got is not None:
            c["got"] += _num(i.got) or 0.0
            c["full"] += _num(i.full) or 0.0
            c["scored_count"] += 1
        elif state == STATE_UNRATED:
            c["unrated"] += 1
        else:
            c["not_scored"] += 1
    for name, c in cats.items():
        c["got"] = round(c["got"], 2)
        c["full"] = round(c["full"], 2)
        c["weight_pct"] = weights.get(name)
        c["score_pct"] = round(c["got"] / c["full"] * 100, 2) if c["full"] else None
        c["included_in_total"] = bool(c["scored_count"])
    return {
        "categories": cats,
        "scored_full": round(sum(c["full"] for c in cats.values()), 2),
        "scored_count": sum(c["scored_count"] for c in cats.values()),
        "total_score_displayable": False,
        "total_score_note": _TOTAL_SCORE_NOTE,
        "weight_config": weights,
    }


def price_scoring(detail: dict | None, items) -> dict:
    """价格分说明块：数据取自评分计划里的参考项，供前端显示"待开标按公式计算"。"""
    refs = (detail or {}).get("reference_rules") or []
    price_rules = [r for r in refs if str(r.get("category")) == "价格"]
    weights = (detail or {}).get("weight_config") or {}
    content = " ".join(str(r.get("content") or "") for r in price_rules)
    formula = next(
        (n for n in ("区间平均价浮动法", "最低价法", "平均价法", "复合标底法") if n in content),
        None,
    )
    params = dict(
        re.findall(r"([A-Za-z]\s?\w{0,3})\s*=\s*(-?\d+(?:\.\d+)?%?)", content)
    )
    cap = None
    m = re.search(r"最高限价\s*([\d.]+)\s*万元", content)
    if m:
        cap = f"{m.group(1)} 万元"
    has_price_item = any(str(getattr(i, "category", "")) == "价格" for i in items)
    return {
        "scored": False,
        "has_price_items": has_price_item,
        "weight_pct": weights.get("价格"),
        "formula": formula,
        "params": params,
        "cap": cap,
        "rule_count": len(price_rules),
        "note": (
            "价格分按公式并以全部投标人报价为基数，需开标后才能计算，本卡不展示价格得分"
            "（显示 0 分会造成误读）。"
        ),
    }
