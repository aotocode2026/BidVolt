"""模拟评标与提升闭环（4.8）：确定性内置 Code Provider + review_item 主模型。"""

from __future__ import annotations

from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deliverable import Deliverable
from app.models.project_material import ProjectMaterial, ProjectSnapshot
from app.models.review import (
    ReviewItem,
    ReviewMaterialLink,
    ReviewProvider,
    ReviewRun,
    ScoreRecord,
)

RULESET_VERSION = "builtin-code-1.0"
DELIVERABLE_NAMES = {1: "商务标", 2: "技术标", 3: "报价单"}


async def ensure_builtin_provider(session: AsyncSession, enterprise_id: int) -> ReviewProvider:
    provider = await session.scalar(
        select(ReviewProvider).where(ReviewProvider.provider_code == "builtin_completeness")
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
        await session.flush()
    return provider


def _claim_id(rule: str, dtype: int) -> str:
    return f"builtin-{rule}-{dtype}"


async def run_evaluation(
    session: AsyncSession,
    *,
    enterprise_id: int,
    project_id: int,
) -> dict:
    """生成 project_snapshot + review_run + score_record + review_items（初始 pending_confirm）。"""
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
    }
    snapshot = ProjectSnapshot(
        enterprise_id=enterprise_id,
        project_id=project_id,
        snapshot_type="review",
        input_refs=input_refs,
        rules_version={"ruleset": RULESET_VERSION},
    )
    session.add(snapshot)
    await session.flush()

    provider = await ensure_builtin_provider(session, enterprise_id)
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

    raw_hash = sha256(json.dumps(items_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
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
    score = ScoreRecord(
        enterprise_id=enterprise_id,
        project_id=project_id,
        review_run_id=run.id,
        total_score=round(total_got / total_full * 100, 2) if total_full else 0.0,
        missing_count=missing_count,
        improvable=round(sum(d["improvable"] for d in items_data), 2),
        detail={"items_count": len(items_data)},
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

    score = ScoreRecord(
        enterprise_id=enterprise_id,
        project_id=project_id,
        review_run_id=run.id,
        total_score=round(sum(float(i.got or 0) for i in new_items) / sum(float(i.full or 0) for i in new_items) * 100, 2),
        missing_count=sum(1 for i in new_items if i.got == 0),
        improvable=round(sum(float(i.improvable or 0) for i in new_items), 2),
        detail={"re_evaluated": len(new_items)},
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
