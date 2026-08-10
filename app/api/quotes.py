"""报价接口（4.7）：历史只读查询、确定性测算、三类策略、AI 参考建议、应用门禁。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission, UserContext
from app.constants import Permission
from app.db import get_session
from app.models.deliverable import Deliverable
from app.models.quote import QuoteCalc
from app.services import deliverable_service
from app.services.audit import write_audit
from app.services.deliverable_service import VersionConflict
from app.services.history_provider import MockHistoryPriceProvider, snapshot_samples
from app.services.quote_engine import (
    QuoteParams,
    calculate,
    strategy_balance,
    strategy_profit,
    strategy_win,
)

router = APIRouter(prefix="/quotes", tags=["quotes"])
provider = MockHistoryPriceProvider()


def _quote_params(body: dict) -> QuoteParams:
    return QuoteParams(
        material_ref=body["material_ref"],
        cost=float(body["cost"]),
        min_profit_rate=float(body.get("min_profit_rate", 0.05)),
        adjustments=body.get("adjustments") or {},
        method=body.get("method", "median"),
        cap=float(body["cap"]) if body.get("cap") is not None else None,
        score_formula=body.get("score_formula"),
    )


@router.get("/history")
async def history_query(
    material_ref: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> dict:
    samples = await provider.query_history({"material_ref": material_ref})
    snapshot_ids = await snapshot_samples(session, user.enterprise_id, samples)
    await session.commit()
    return {
        "sample_count": len(samples),
        "samples": samples,
        "snapshot_ids": snapshot_ids,
        "readonly": True,
    }


@router.get("/history/{material_ref}/samples")
async def material_samples(
    material_ref: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> list[dict]:
    return await provider.get_material_samples(material_ref)


@router.get("/history/source-metadata")
async def source_metadata(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> list[dict]:
    return await provider.get_source_metadata()


@router.post("/calculate")
async def calculate_quote(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> dict:
    params = _quote_params(body)
    samples = await provider.get_material_samples(params.material_ref)
    try:
        result = calculate(params, samples)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    snapshot_ids = await snapshot_samples(session, user.enterprise_id, samples)
    calc = QuoteCalc(
        enterprise_id=user.enterprise_id,
        project_id=body.get("project_id", 0),
        params=body,
        result=result,
        snapshot_refs=snapshot_ids,
        status=1,
    )
    session.add(calc)
    await session.commit()
    return {"calc_id": calc.id, "result": result}


@router.post("/strategies")
async def quote_strategies(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> dict:
    calc = await session.scalar(
        select(QuoteCalc).where(
            QuoteCalc.id == body["calc_id"],
            QuoteCalc.enterprise_id == user.enterprise_id,
        )
    )
    if calc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="测算不存在")
    params = _quote_params(calc.params)
    strategy = body["strategy"]
    if strategy == "win":
        out = strategy_win(calc.result, params)
    elif strategy == "balance":
        out = strategy_balance(calc.result, params)
    elif strategy == "profit":
        out = strategy_profit(calc.result, params)
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="未知策略")
    calc.strategy_results = {**(calc.strategy_results or {}), strategy: out}
    await session.commit()
    return out


@router.post("/ai-suggest")
async def ai_suggest(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> dict:
    calc = await session.scalar(
        select(QuoteCalc).where(
            QuoteCalc.id == body["calc_id"],
            QuoteCalc.enterprise_id == user.enterprise_id,
        )
    )
    if calc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="测算不存在")
    basis = body.get("basis")
    if not basis:
        return {"unavailable": True, "message": "无可追溯依据，不输出报价数字（D-F）"}
    suggested = calc.result["suggested"]
    low = round(suggested * 0.95, 2)
    high = round(suggested * 1.05, 2)
    suggest = {
        "price_range": [low, high],
        "recommended": suggested,
        "reasons": [f"基于历史样本中位数与调整系数；依据：{basis}"],
        "assumptions": list(calc.result.get("adjustments", {}).keys()),
        "confidence": "low",
        "risk_level": "medium",
        "is_ai_suggest": True,
    }
    calc.ai_suggest = suggest
    await session.commit()
    return suggest


@router.post("/apply", status_code=status.HTTP_201_CREATED)
async def apply_quote(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_APPLY)),
) -> dict:
    calc = await session.scalar(
        select(QuoteCalc).where(
            QuoteCalc.id == body["calc_id"],
            QuoteCalc.enterprise_id == user.enterprise_id,
        )
    )
    if calc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="测算不存在")
    if calc.status != 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该测算已应用或已放弃")
    deliverable = await session.get(Deliverable, body["deliverable_id"])
    if deliverable is None or deliverable.enterprise_id != user.enterprise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报价单成果不存在")
    suggested = (calc.strategy_results or {}).get("win", {}).get("suggested_price", calc.result["suggested"])
    sheet_model = {
        "type": "sheet",
        "sheets": [
            {
                "name": "报价单",
                "rows": [
                    ["项目", "建议价"],
                    [calc.result.get("material_ref"), suggested],
                    ["说明", body.get("note") or "确定性算法测算价（QuoteEngine）"],
                ],
            }
        ],
    }
    try:
        version = await deliverable_service.save_version(
            session,
            deliverable,
            sheet_model,
            version_type=5,  # 报价应用
            created_by=user.user_id,
            expected_version_no=body.get("expected_version_no"),
            idempotency_key=body.get("idempotency_key"),
        )
    except VersionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    calc.status = 2
    calc.applied_version_no = version.version_no
    calc.applied_at = datetime.now(timezone.utc)
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=deliverable.project_id,
        action="quote_apply",
        object_type="deliverable_version",
        object_id=version.id,
        payload={"calc_id": calc.id, "price": suggested, "version_no": version.version_no},
    )
    await session.commit()
    return {"new_version_no": version.version_no, "calc_status": calc.status}
