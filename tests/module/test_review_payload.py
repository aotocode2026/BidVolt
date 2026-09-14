"""评分接口展示层契约回归（issue #72）：四态 / 语义化动作 / 汇总 / 综合分与价格口径。"""

from __future__ import annotations

from types import SimpleNamespace

from app.services import review_payload as rp


def _item(
    iid: int,
    *,
    verdict: str,
    full=None,
    got=None,
    improvable=None,
    action: str = "manual_review",
    category: str = "技术",
    evidence: dict | None = None,
):
    return SimpleNamespace(
        id=iid,
        requirement_id=1,
        criterion_id=f"c{iid}",
        ruleset_version="v1",
        category=category,
        problem_description="规则",
        got=got,
        full=full,
        improvable=improvable,
        risk_level=0,
        suggestion=None,
        suggestion_override=None,
        action_type=action,
        evidence=evidence or {},
        verdict=verdict,
        deduction_reason=None,
        rule_source={},
        response_source={},
        missing_materials=[],
        status=1,
    )


def test_score_state_covers_four_states():
    assert rp.score_state("satisfied", 30, 30) == rp.STATE_FULL
    assert rp.score_state("partial", 30, 26) == rp.STATE_IMPROVABLE
    assert rp.score_state("unsatisfied", 3, 0) == rp.STATE_IMPROVABLE
    assert rp.score_state("insufficient_evidence", 30, None) == rp.STATE_UNRATED
    assert rp.score_state("not_applicable", None, None) == rp.STATE_NOT_SCORED
    # 扣分型条款：satisfied 且 full=0 → 不参与计分（不是"已通过得分"）
    assert rp.score_state("satisfied", 0, 0) == rp.STATE_NOT_SCORED


def test_effective_action_type_maps_legacy_values():
    # 历史库里的 manual_review 兜底值 → 按语义映射
    assert rp.effective_action_type("manual_review", "satisfied", 30, 30) == rp.ACTION_NONE
    assert rp.effective_action_type("manual_review", "not_applicable", None, None) == rp.ACTION_REFERENCE
    assert rp.effective_action_type("manual_review", "satisfied", 0, 0) == rp.ACTION_REFERENCE
    assert rp.effective_action_type("upload_material", "insufficient_evidence", 30, None) == rp.ACTION_UPLOAD
    assert rp.effective_action_type("manual_review", "insufficient_evidence", 30, None) == rp.ACTION_MANUAL
    assert rp.effective_action_type("edit_deliverable", "unsatisfied", 3, 0) == rp.ACTION_EDIT
    assert rp.effective_action_type("manual_review", "partial", 30, 20) == rp.ACTION_EDIT


def test_derive_action_type_for_new_records():
    assert rp.derive_action_type("not_applicable", None, False) == rp.ACTION_REFERENCE
    assert rp.derive_action_type("satisfied", 30, False) == rp.ACTION_NONE
    assert rp.derive_action_type("satisfied", 0, False) == rp.ACTION_REFERENCE
    assert rp.derive_action_type("insufficient_evidence", 30, True) == rp.ACTION_UPLOAD
    assert rp.derive_action_type("insufficient_evidence", 30, False) == rp.ACTION_MANUAL
    assert rp.derive_action_type("unsatisfied", 3, False) == rp.ACTION_EDIT


def test_item_payload_has_state_percent_and_chunk_summary():
    item = _item(
        1,
        verdict="partial",
        full=30,
        got=26,
        improvable=4,
        action="edit_deliverable",
        evidence={
            "chunks_provided": [
                {"index": 1, "label": "技术文件 › 四、工作规划描述", "chars": 3000},
                {"index": 2, "label": "技术文件 › 六、实施组织与进度计划", "chars": 2500},
            ],
            "tiers": [{"label": "一般", "min": 25, "max": 26}],
        },
    )
    payload = rp.item_payload(item)
    assert payload["score_state"] == rp.STATE_IMPROVABLE
    assert payload["is_scored"] is True
    assert payload["improvable_percent"] == 13.33
    assert payload["action_type"] == rp.ACTION_EDIT
    assert payload["action_type_raw"] == "edit_deliverable"
    # chunks 默认收敛为摘要
    assert "chunks_provided" not in payload["evidence"]
    assert payload["evidence"]["chunks_summary"] == {
        "count": 2,
        "chars": 5500,
        "head": ["技术文件 › 四、工作规划描述", "技术文件 › 六、实施组织与进度计划"],
    }
    assert payload["evidence"]["tiers"]
    # include_chunks 时给全量
    full = rp.item_payload(item, include_chunks=True)
    assert len(full["evidence"]["chunks_provided"]) == 2


def test_summarize_items_three_lists_and_improvable_total():
    items = [
        _item(1, verdict="satisfied", full=30, got=30, improvable=0, action="none"),
        _item(2, verdict="partial", full=30, got=26, improvable=4, action="edit_deliverable"),
        _item(3, verdict="unsatisfied", full=3, got=0, improvable=3, action="edit_deliverable"),
        _item(4, verdict="insufficient_evidence", full=5, action="upload_material"),
        _item(5, verdict="not_applicable", action="reference", category="价格"),
        _item(6, verdict="satisfied", full=0, got=0, improvable=0, action="reference"),
    ]
    s = rp.summarize_items(items)
    assert s["items_by_state"][rp.STATE_FULL] == [1]
    assert sorted(s["items_by_state"][rp.STATE_IMPROVABLE]) == [2, 3]
    assert s["items_by_state"][rp.STATE_UNRATED] == [4]
    assert sorted(s["items_by_state"][rp.STATE_NOT_SCORED]) == [5, 6]
    assert s["improvable_total"] == 7.0
    assert s["improvable_count"] == 2
    assert s["unrated_count"] == 1
    assert "另有 1 条待评" in s["unrated_note"]
    assert s["items_by_action"][rp.ACTION_UPLOAD] == [4]
    assert s["items_by_action"][rp.ACTION_EDIT] == [2, 3]


def test_score_breakdown_and_price_scoring():
    items = [
        _item(1, verdict="satisfied", full=100, got=90, improvable=10, category="技术"),
        _item(2, verdict="partial", full=70, got=54, improvable=16, category="商务"),
        _item(3, verdict="not_applicable", action="reference", category="价格"),
    ]
    detail = {
        "weight_config": {"价格": 30, "商务": 10, "技术": 60},
        "reference_rules": [
            {
                "category": "价格",
                "content": "报价规则2（区间平均价浮动法，参数C=0.02、n1=1、n2=0.5）：最高限价198万元",
                "rule_id": 6640,
            }
        ],
    }
    breakdown = rp.score_breakdown(detail, items)
    assert breakdown["total_score_displayable"] is False
    assert "暂不对外展示" in breakdown["total_score_note"]
    assert breakdown["categories"]["技术"] == {
        "got": 90.0,
        "full": 100.0,
        "scored_count": 1,
        "unrated": 0,
        "not_scored": 0,
        "weight_pct": 60,
        "score_pct": 90.0,
        "included_in_total": True,
    }
    assert breakdown["scored_count"] == 2
    assert breakdown["scored_full"] == 170.0

    price = rp.price_scoring(detail, items)
    assert price["scored"] is False
    assert price["weight_pct"] == 30
    assert price["formula"] == "区间平均价浮动法"
    assert price["params"].get("C") == "0.02"
    assert price["cap"] == "198 万元"
    assert "开标后" in price["note"]
