"""模拟评标与提升闭环（4.8）：确定性内置 Code Provider + review_item 主模型。"""

from __future__ import annotations

import json
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deliverable import Deliverable
from app.models.project_material import ProjectMaterial, ProjectSnapshot
from app.models.requirement import Requirement
from app.models.review import (
    ReviewItem,
    ReviewMaterialLink,
    ReviewProvider,
    ReviewRun,
    ScoreRecord,
)
from app.services import deliverable_service

RULESET_VERSION = "builtin-code-1.0"
DELIVERABLE_NAMES = {1: "商务标", 2: "技术标", 3: "报价单"}


async def ensure_builtin_provider(session: AsyncSession, enterprise_id: int) -> ReviewProvider:
    provider = await session.scalar(
        select(ReviewProvider).where(
            ReviewProvider.provider_code == "builtin_completeness",
            ReviewProvider.enterprise_id == enterprise_id,
        )
    )
    if provider is None:
        provider = ReviewProvider(
            enterprise_id=enterprise_id,
            provider_type="code",
            provider_code="builtin_completeness",
            provider_version="1.0.0",
            name="成果完整性检查（内置）",
            category="integrity",
            severity=2,
            enabled=True,
        )
        session.add(provider)
        try:
            # 保存点隔离：并发请求同时创建同企业 Provider 时，唯一约束冲突只回滚本次 INSERT
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            # 冲突说明另一并发请求已建好：回滚保存点后重查（租户内 code 唯一，结果必属于本企业）
            provider = await session.scalar(
                select(ReviewProvider).where(
                    ReviewProvider.provider_code == "builtin_completeness",
                    ReviewProvider.enterprise_id == enterprise_id,
                )
            )
            if provider is None:
                raise
    return provider


def _claim_id(rule: str, dtype: int) -> str:
    return f"builtin-{rule}-{dtype}"


async def run_evaluation(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    provider_id: int | None = None,
) -> dict:
    """生成 project_snapshot + review_run + score_record + review_items（初始 pending_confirm）。

    provider_id（Issue #6 P0）：未传则使用企业内置 Provider；传入则校验
    （必须属于本企业、已启用、当前仅支持 builtin_completeness 引擎），
    非法/禁用/跨租户/不支持的引擎一律失败关闭（ValueError，由 API 映射 404/422）。
    评审冻结 Provider 版本与配置身份：provider_code/version 计入快照与 raw_hash。
    """
    if provider_id is None:
        provider = await ensure_builtin_provider(session, enterprise_id)
    else:
        provider = await session.scalar(
            select(ReviewProvider).where(
                ReviewProvider.id == provider_id,
                ReviewProvider.enterprise_id == enterprise_id,
            )
        )
        if provider is None:
            raise ValueError("provider_not_found")
        if not provider.enabled:
            raise ValueError("provider_disabled")
        if provider.provider_code != "builtin_completeness":
            raise ValueError("provider_unsupported")
    deliverables = (
        await session.scalars(
            select(Deliverable).where(
                Deliverable.enterprise_id == enterprise_id,
                Deliverable.project_id == project_id,
            )
        )
    ).all()
    existing_types = {d.deliverable_type for d in deliverables}
    input_refs = {
        "deliverable_versions": {d.id: d.current_version_no for d in deliverables},
        "ruleset": RULESET_VERSION,
        "provider_code": provider.provider_code,
        "provider_version": provider.provider_version,
    }
    snapshot = ProjectSnapshot(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_type="review",
        input_refs=input_refs,
        rules_version={"ruleset": RULESET_VERSION, "provider_code": provider.provider_code},
    )
    session.add(snapshot)
    await session.flush()

    items_data: list[dict] = []
    for dtype, name in DELIVERABLE_NAMES.items():
        if dtype in existing_types:
            items_data.append(
                {
                    "category": "完整性",
                    "problem_description": f"{name}已生成",
                    "got": 10.0,
                    "full": 10.0,
                    "improvable": 0.0,
                    "risk_level": 0,
                    "suggestion": None,
                    "action_type": "manual_review",
                    "missing_material_types": None,
                    "evidence": {
                        "claim_id": _claim_id("integrity", dtype),
                        "source_version_id": input_refs["deliverable_versions"].get(dtype),
                        "content_hash": None,
                        "source_range": None,
                        "exact_quote": None,
                    },
                }
            )
        else:
            items_data.append(
                {
                    "category": "完整性",
                    "problem_description": f"缺少{name}",
                    "got": 0.0,
                    "full": 10.0,
                    "improvable": 10.0,
                    "risk_level": 2,
                    "suggestion": f"请上传{name}",
                    "action_type": "upload_material",
                    "missing_material_types": name,
                    "evidence": {
                        "claim_id": _claim_id("integrity", dtype),
                        "source_version_id": None,
                        "content_hash": None,
                        "source_range": None,
                        "exact_quote": None,
                    },
                }
            )

    # 评分细则权重化评审引擎（路线图项）：解析出的评分细则（structured.score_rule.weight/criterion）
    # 逐项打分：细则在技术标/商务标正文中体现 → 得满分；未体现 → 0 分并给出建议。
    score_rule_reqs = (
        await session.scalars(
            select(Requirement).where(
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
                Requirement.req_type == "score_rule",
            )
        )
    ).all()
    score_rule_stats = {"count": 0, "weight_total": 0.0, "weight_got": 0.0, "missed": 0}
    if score_rule_reqs:
        doc_texts: dict[int, str] = {}
        for d in deliverables:
            if d.current_version_no == 0:
                continue
            try:
                _, m = await deliverable_service.get_version_content(session, d.id, d.current_version_no)
            except Exception:  # noqa: BLE001
                m = {}
            doc_texts[d.deliverable_type] = "\n".join(
                n.get("text", "") for n in (m or {}).get("nodes", [])
            )
        for r in score_rule_reqs:
            structured = r.structured or {}
            rule = structured.get("score_rule") or {}
            try:
                weight = float(rule.get("weight") or 10)
            except (TypeError, ValueError):
                weight = 10.0
            criterion = str(rule.get("criterion") or r.content)
            covered = any(
                t and (r.content[:10] in t or criterion[:10] in t)
                for t in doc_texts.values()
            )
            items_data.append(
                {
                    "category": "评分细则",
                    "problem_description": r.content,
                    "got": round(weight, 2) if covered else 0.0,
                    "full": round(weight, 2),
                    "improvable": 0.0 if covered else round(weight, 2),
                    "risk_level": 0 if covered else 1,
                    "suggestion": None if covered else f"评分细则未在成果中体现：{criterion[:40]}",
                    "action_type": "manual_review",
                    "missing_material_types": None,
                    "evidence": {
                        "claim_id": f"score-rule-{r.id}",
                        "criterion": criterion,
                        "weight": weight,
                        "source_requirement_id": r.id,
                    },
                }
            )
            score_rule_stats["count"] += 1
            score_rule_stats["weight_total"] += round(weight, 2)
            if covered:
                score_rule_stats["weight_got"] += round(weight, 2)
            else:
                score_rule_stats["missed"] += 1

    raw_hash = sha256(
        json.dumps(
            {
                "provider_code": provider.provider_code,
                "provider_version": provider.provider_version,
                "items": items_data,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    run = ReviewRun(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_id=snapshot.id,
        provider_id=provider.id,
        provider_raw_hash=raw_hash,
        status=2,
    )
    session.add(run)
    await session.flush()

    total_got = sum(d["got"] for d in items_data)
    total_full = sum(d["full"] for d in items_data)
    missing_count = sum(1 for d in items_data if d["got"] == 0)
    # 评分基准（Issue #8 反馈"未获取评审细则却显示 10 分满分"）：
    # 有招标评分细则 → 总分按细则权重计算（weight_got/weight_total×100）；
    # 无细则 → 内置完整性规则得分，并显式标注"未获取评分细则，不代表招标评标得分"。
    has_rules = bool(score_rule_reqs)
    if has_rules:
        scale = "score_rules"
        rule_got = score_rule_stats["weight_got"]
        rule_full = score_rule_stats["weight_total"] or 1.0
        overall = round(rule_got / rule_full * 100, 2)
        full_marks = round(rule_full, 2)
        got_marks = round(rule_got, 2)
    else:
        scale = "builtin"
        overall = round(total_got / total_full * 100, 2) if total_full else 0.0
        full_marks = round(total_full, 2)
        got_marks = round(total_got, 2)
    score = ScoreRecord(
        enterprise_id=enterprise_id,
        project_id=project_id,
        review_run_id=run.id,
        total_score=overall,
        missing_count=missing_count,
        improvable=round(sum(d["improvable"] for d in items_data), 2),
        deliverable_versions=input_refs["deliverable_versions"],
        detail={
            "items_count": len(items_data),
            "score_rules": score_rule_stats,
            "scale": scale,
            "full_marks": full_marks,
            "got_marks": got_marks,
        },
    )
    session.add(score)
    await session.flush()

    items: list[ReviewItem] = []
    for data in items_data:
        item = ReviewItem(
            enterprise_id=enterprise_id,
            project_id=project_id,
            review_run_id=run.id,
            score_id=score.id,
            ruleset_version=RULESET_VERSION,
            category=data["category"],
            problem_description=data["problem_description"],
            got=data["got"],
            full=data["full"],
            improvable=data["improvable"],
            risk_level=data["risk_level"],
            suggestion=data["suggestion"],
            action_type=data["action_type"],
            evidence=data["evidence"],
            missing_material_types=data["missing_material_types"],
            status=1,
            expected_version=str(snapshot.id),
        )
        session.add(item)
        items.append(item)
    await session.flush()
    return {
        "run_id": run.id,
        "score_id": score.id,
        "snapshot_id": snapshot.id,
        "total_score": float(score.total_score),
        "missing_count": missing_count,
        "item_ids": [i.id for i in items],
        "score_rules": score_rule_stats,
        "scale": scale,
        "full_marks": full_marks,
        "got_marks": got_marks,
    }


async def confirm_items(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    score_id: int,
    item_ids: list[int],
    action: str,
    expected_version: str | None = None,
) -> list[dict]:
    if action not in ("confirm", "reject"):
        raise ValueError("action 必须为 confirm 或 reject")
    results: list[dict] = []
    for item_id in item_ids:
        item = await session.scalar(
            select(ReviewItem).where(
                ReviewItem.id == item_id,
                ReviewItem.enterprise_id == enterprise_id,
                ReviewItem.project_id == project_id,
                ReviewItem.score_id == score_id,
            )
        )
        if item is None:
            results.append({"item_id": item_id, "status": "skipped", "reason": "条目不存在"})
            continue
        if item.status != 1:
            results.append({"item_id": item_id, "status": "skipped", "reason": f"当前状态不可确认（{item.status}）"})
            continue
        if expected_version is not None and str(item.expected_version) != str(expected_version):
            results.append({"item_id": item_id, "status": "conflict", "reason": "快照版本不一致"})
            continue
        item.status = 2 if action == "confirm" else 3
        results.append({"item_id": item_id, "status": "succeeded"})
    return results


async def re_evaluate(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
    item_ids: list[int],
) -> dict:
    """仅重审受影响项：确认且可补材料的项，若项目已有材料则提升 got=full。"""
    originals = (
        await session.scalars(
            select(ReviewItem).where(
                ReviewItem.id.in_(item_ids),
                ReviewItem.enterprise_id == enterprise_id,
                ReviewItem.project_id == project_id,
            )
        )
    ).all()
    if not originals:
        raise ValueError("没有可重审的条目")

    provider = await ensure_builtin_provider(session, enterprise_id)
    snapshot = ProjectSnapshot(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_type="review",
        input_refs={"item_ids": item_ids, "ruleset": RULESET_VERSION},
        rules_version={"ruleset": RULESET_VERSION},
    )
    session.add(snapshot)
    await session.flush()

    material = await session.scalar(
        select(ProjectMaterial).where(
            ProjectMaterial.enterprise_id == enterprise_id,
            ProjectMaterial.project_id == project_id,
        ).limit(1)
    )

    run = ReviewRun(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_id=snapshot.id,
        provider_id=provider.id,
        status=2,
    )
    session.add(run)
    await session.flush()

    new_items: list[ReviewItem] = []
    for original in originals:
        improved = (
            original.status == 2
            and original.action_type == "upload_material"
            and material is not None
        )
        got = float(original.full) if improved else float(original.got or 0)
        improvable = 0.0 if improved else float(original.improvable or 0)
        item = ReviewItem(
            enterprise_id=enterprise_id,
            project_id=project_id,
            review_run_id=run.id,
            requirement_id=original.requirement_id,
            criterion_id=original.criterion_id,
            ruleset_version=original.ruleset_version,
            category=original.category,
            problem_description=original.problem_description,
            got=got,
            full=original.full,
            improvable=improvable,
            risk_level=original.risk_level,
            suggestion=original.suggestion,
            action_type=original.action_type,
            evidence=original.evidence,
            missing_material_types=original.missing_material_types,
            status=4,  # re_reviewed
            expected_version=str(snapshot.id),
        )
        session.add(item)
        await session.flush()
        if improved:
            session.add(
                ReviewMaterialLink(
                    enterprise_id=enterprise_id,
                    project_id=project_id,
                    review_item_id=item.id,
                    material_id=material.id,
                    material_type="project",
                    match_basis="自动匹配（V1 确定性）",
                    confidence=0.9,
                )
            )
        new_items.append(item)

    # 总分 = 原评审已确认项（未被本次重审替换的部分） + 重审新项
    original_score_ids = {o.score_id for o in originals if o.score_id is not None}
    carried: list[ReviewItem] = []
    replaced_ids = {o.id for o in originals}
    for score_id in original_score_ids:
        score_row = await session.scalar(
            select(ScoreRecord).where(
                ScoreRecord.id == score_id,
                ScoreRecord.enterprise_id == enterprise_id,
                ScoreRecord.project_id == project_id,
            )
        )
        if score_row is None:
            continue
        run_rows = (
            await session.scalars(
                select(ReviewItem).where(
                    ReviewItem.review_run_id == score_row.review_run_id,
                    ReviewItem.enterprise_id == enterprise_id,
                    ReviewItem.project_id == project_id,
                )
            )
        ).all()
        for row in run_rows:
            if row.id in replaced_ids:
                continue  # 本次重审已生成新项
            carried.append(row)  # 未被替换的原项全部保留（含已给分未确认项）
    all_items = carried + new_items
    total_got = sum(float(i.got or 0) for i in all_items)
    total_full = sum(float(i.full or 0) for i in all_items)
    score = ScoreRecord(
        enterprise_id=enterprise_id,
        project_id=project_id,
        review_run_id=run.id,
        total_score=round(total_got / total_full * 100, 2) if total_full else 0.0,
        missing_count=sum(1 for i in all_items if i.got == 0),
        improvable=round(sum(float(i.improvable or 0) for i in all_items), 2),
        detail={"re_evaluated": len(new_items), "carried": len(carried)},
    )
    session.add(score)
    await session.flush()
    return {
        "run_id": run.id,
        "score_id": score.id,
        "total_score": float(score.total_score),
        "new_item_ids": [i.id for i in new_items],
        "improved_count": sum(1 for i in new_items if i.improvable == 0 and i.got == i.full),
    }
