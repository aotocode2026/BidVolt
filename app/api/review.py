"""模拟评标与提升闭环接口（4.8.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_capability, require_permission, UserContext
from app.constants import Permission
from app.db import get_session
from app.models.review import ReviewItem, ReviewProvider, ReviewRun, ScoreRecord
from app.services import review_service
from app.services.audit import write_audit

router = APIRouter(prefix="/projects", tags=["review"])
providers_router = APIRouter(prefix="/review-providers", tags=["review"])


@router.post("/{project_id}/evaluate")
async def evaluate(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.SCORE_CONFIRM)),
) -> dict:
    result = await review_service.run_evaluation(
        session, enterprise_id=user.enterprise_id, project_id=project_id
    )
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project_id,
        action="score.submit",
        object_type="review_run",
        object_id=result["run_id"],
    )
    await session.commit()
    return result


@router.get("/{project_id}/scores")
async def latest_score(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_latest_score")),
) -> dict:
    score = await session.scalar(
        select(ScoreRecord)
        .where(
            ScoreRecord.enterprise_id == user.enterprise_id,
            ScoreRecord.project_id == project_id,
        )
        .order_by(ScoreRecord.id.desc())
        .limit(1)
    )
    if score is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="尚未评标")
    return {
        "score_id": score.id,
        "review_run_id": score.review_run_id,
        "total_score": float(score.total_score) if score.total_score is not None else None,
        "missing_count": score.missing_count,
        "improvable": float(score.improvable) if score.improvable is not None else None,
        "detail": score.detail,
    }


@router.get("/{project_id}/reviews")
async def list_review_runs(
    project_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.SCORE_VIEW)),
) -> dict:
    """评审运行列表（Issue #2 #26/#28：按 run 恢复上下文）。"""
    runs = (
        await session.scalars(
            select(ReviewRun)
            .where(
                ReviewRun.enterprise_id == user.enterprise_id,
                ReviewRun.project_id == project_id,
            )
            .order_by(ReviewRun.id.desc())
            .limit(100)
        )
    ).all()
    return {
        "items": [
            {
                "run_id": r.id,
                "provider_id": r.provider_id,
                "snapshot_id": r.snapshot_id,
                "status": r.status,
                "provider_raw_hash": r.provider_raw_hash,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in runs
        ]
    }


@router.get("/{project_id}/reviews/{run_id}")
async def review_run_detail(
    project_id: int,
    run_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.SCORE_VIEW)),
) -> dict:
    """按 run_id 恢复完整评审上下文：provider + score + 逐条 items + snapshot_id。"""
    run = await session.scalar(
        select(ReviewRun).where(
            ReviewRun.id == run_id,
            ReviewRun.enterprise_id == user.enterprise_id,
            ReviewRun.project_id == project_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评审运行不存在")
    provider = await session.get(ReviewProvider, run.provider_id) if run.provider_id else None
    score = await session.scalar(
        select(ScoreRecord)
        .where(
            ScoreRecord.review_run_id == run.id,
            ScoreRecord.enterprise_id == user.enterprise_id,
            ScoreRecord.project_id == project_id,
        )
        .order_by(ScoreRecord.id.desc())
        .limit(1)
    )
    items = (
        await session.scalars(
            select(ReviewItem).where(
                ReviewItem.review_run_id == run.id,
                ReviewItem.enterprise_id == user.enterprise_id,
                ReviewItem.project_id == project_id,
            )
        )
    ).all()
    return {
        "run_id": run.id,
        "status": run.status,
        "snapshot_id": run.snapshot_id,
        "provider": (
            {
                "provider_id": provider.id,
                "provider_type": provider.provider_type,
                "provider_code": provider.provider_code,
                "provider_version": provider.provider_version,
                "name": provider.name,
            }
            if provider
            else None
        ),
        "score": (
            {
                "score_id": score.id,
                "total_score": float(score.total_score) if score.total_score is not None else None,
                "missing_count": score.missing_count,
                "improvable": float(score.improvable) if score.improvable is not None else None,
                "detail": score.detail,
            }
            if score
            else None
        ),
        "items": [
            {
                "item_id": i.id,
                "category": i.category,
                "problem_description": i.problem_description,
                "got": float(i.got) if i.got is not None else None,
                "full": float(i.full) if i.full is not None else None,
                "improvable": float(i.improvable) if i.improvable is not None else None,
                "risk_level": i.risk_level,
                "suggestion": i.suggestion,
                "suggestion_override": i.suggestion_override,
                "effective_suggestion": i.suggestion_override or i.suggestion,
                "action_type": i.action_type,
                "evidence": i.evidence,
                "status": i.status,
            }
            for i in items
        ],
    }


@router.get("/{project_id}/scores/{score_id}/items")
async def review_items(
    project_id: int,
    score_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_review_items")),
) -> list[dict]:
    score = await session.scalar(
        select(ScoreRecord).where(
            ScoreRecord.id == score_id,
            ScoreRecord.enterprise_id == user.enterprise_id,
            ScoreRecord.project_id == project_id,
        )
    )
    if score is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评分记录不存在")
    rows = await session.scalars(
        select(ReviewItem).where(
            ReviewItem.enterprise_id == user.enterprise_id,
            ReviewItem.project_id == project_id,
            ReviewItem.score_id == score_id,
        )
    )
    return [
        {
            "item_id": i.id,
            "category": i.category,
            "problem_description": i.problem_description,
            "got": float(i.got) if i.got is not None else None,
            "full": float(i.full) if i.full is not None else None,
            "improvable": float(i.improvable) if i.improvable is not None else None,
            "risk_level": i.risk_level,
            "suggestion": i.suggestion,
            "suggestion_override": i.suggestion_override,
            "effective_suggestion": i.suggestion_override or i.suggestion,
            "action_type": i.action_type,
            "evidence": i.evidence,
            "status": i.status,
        }
        for i in rows
    ]


@router.put("/{project_id}/scores/{score_id}/items/{item_id}/suggestion")
async def save_suggestion_override(
    project_id: int,
    score_id: int,
    item_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("confirm_review_items")),
) -> dict:
    """保存用户修改后的评审建议（Issue #2 #29，保留原始 suggestion）。"""
    item = await session.scalar(
        select(ReviewItem).where(
            ReviewItem.id == item_id,
            ReviewItem.enterprise_id == user.enterprise_id,
            ReviewItem.project_id == project_id,
            ReviewItem.score_id == score_id,
        )
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评审项不存在")
    suggestion = (body.get("suggestion") or "").strip()
    if not suggestion:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="建议为空")
    item.suggestion_override = suggestion
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project_id,
        action="review_item.suggestion_override",
        object_type="review_item",
        object_id=item.id,
        payload={"suggestion": suggestion},
    )
    await session.commit()
    return {
        "item_id": item.id,
        "suggestion_override": item.suggestion_override,
        "effective_suggestion": item.suggestion_override,
    }


@router.put("/{project_id}/scores/{score_id}/items/{item_id}/confirm")
async def confirm_one(
    project_id: int,
    score_id: int,
    item_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("confirm_review_items")),
) -> dict:
    results = await review_service.confirm_items(
        session,
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        score_id=score_id,
        item_ids=[item_id],
        action=body["action"],
        expected_version=body.get("expected_version"),
    )
    await session.commit()
    return results[0]


@router.post("/{project_id}/scores/{score_id}/items/confirm")
async def confirm_batch(
    project_id: int,
    score_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("confirm_review_items")),
) -> dict:
    results = await review_service.confirm_items(
        session,
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        score_id=score_id,
        item_ids=body["item_ids"],
        action=body.get("action", "confirm"),
        expected_version=body.get("expected_version"),
    )
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project_id,
        action="score.confirm",
        payload={"results": results},
    )
    await session.commit()
    return {"results": results}


@router.post("/{project_id}/re-evaluate")
async def re_evaluate(
    project_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.SCORE_CONFIRM)),
) -> dict:
    try:
        result = await review_service.re_evaluate(
            session,
            enterprise_id=user.enterprise_id,
            project_id=project_id,
            item_ids=body["item_ids"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project_id,
        action="score.re_evaluate",
        object_type="review_run",
        object_id=result["run_id"],
    )
    await session.commit()
    return result


@providers_router.get("")
async def list_providers(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.SCORE_VIEW)),
) -> list[dict]:
    rows = await session.scalars(select(ReviewProvider))
    return [
        {
            "provider_id": p.id,
            "provider_code": p.provider_code,
            "provider_type": p.provider_type,
            "provider_version": p.provider_version,
            "name": p.name,
            "enabled": p.enabled,
        }
        for p in rows
    ]


@providers_router.put("/{provider_id}/config")
async def update_provider_config(
    provider_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.REVIEW_PROVIDER_CONFIG)),
) -> dict:
    provider = await session.scalar(
        select(ReviewProvider).where(ReviewProvider.id == provider_id)
    )
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider 不存在")
    if "enabled" in body:
        provider.enabled = bool(body["enabled"])
    if "config" in body:
        provider.config = body["config"]
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        action="review_provider.config",
        object_type="review_provider",
        object_id=provider.id,
    )
    await session.commit()
    return {"provider_id": provider.id, "enabled": provider.enabled}
