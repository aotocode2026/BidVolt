"""报价接口（4.7）：历史只读查询、确定性测算、三类策略、AI 参考建议、应用门禁。"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, require_capability, require_permission
from app.constants import Permission
from app.db import get_session
from app.models.deliverable import Deliverable
from app.models.quote import HistoryPriceSnapshot, QuoteCalc
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


# 报价数值契约（Issue #6 P0）：金额/费率一律字符串输出，避免 float 精度与前端精度问题
_MONEY_KEYS = {
    "suggested", "min_price", "median", "avg", "base", "cap",
    "suggested_price", "median_price", "win_price", "unit_price", "price", "cost",
}


def _money_dict(d: dict) -> dict:
    out: dict = {}
    for k, v in d.items():
        if k in _MONEY_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = str(v)
        elif k == "price_range" and isinstance(v, list):
            out[k] = [str(x) for x in v]
        elif k == "samples" and isinstance(v, list):
            out[k] = [_money_dict(s) if isinstance(s, dict) else s for s in v]
        else:
            out[k] = v
    return out


@router.get("/history")
async def history_query(
    material_ref: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_history_price")),
) -> dict:
    samples = await provider.query_history({"material_ref": material_ref})
    snapshot_ids = await snapshot_samples(session, user.enterprise_id, samples)
    await session.commit()
    return {
        "sample_count": len(samples),
        "samples": [_money_dict(s) for s in samples],
        "snapshot_ids": snapshot_ids,
        "readonly": True,
    }


@router.get("/history/{material_ref}/samples")
async def material_samples(
    material_ref: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> list[dict]:
    return [_money_dict(s) for s in await provider.get_material_samples(material_ref)]


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
    user: UserContext = Depends(require_capability("calculate_quote")),
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
    return {"calc_id": calc.id, "result": _money_dict(result)}


@router.post("/recalc")
async def recalc_quote(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("calculate_quote")),
) -> dict:
    """按冻结样本 + 原始参数 + 算法版本复算建议价（A-8 复算可验证）。"""
    calc = await session.scalar(
        select(QuoteCalc).where(
            QuoteCalc.id == body["calc_id"],
            QuoteCalc.enterprise_id == user.enterprise_id,
        )
    )
    if calc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="测算记录不存在")
    params = _quote_params(calc.params)
    snapshot_ids = calc.snapshot_refs or []
    rows = (
        await session.scalars(
            select(HistoryPriceSnapshot).where(
                HistoryPriceSnapshot.id.in_(snapshot_ids),
                HistoryPriceSnapshot.enterprise_id == user.enterprise_id,
            )
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无冻结样本，无法复算")
    samples = [
        {
            "material_ref": params.material_ref,
            "material_name": r.material_name,
            "material_code": r.material_code,
            "spec": r.spec,
            "region": r.region,
            "win_price": float(r.win_price),
            "win_date": r.win_date,
            "unit": "元",
            "currency": "CNY",
            "tax_included": True,
        }
        for r in rows
    ]
    result = calculate(params, samples)
    return {
        "calc_id": calc.id,
        "recalc": _money_dict(result),
        "matches_original": result["suggested"] == calc.result.get("suggested"),
        "engine_version": result["engine_version"],
    }


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
    return _money_dict(out)


@router.post("/ai-suggest")
async def ai_suggest(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> dict:
    """AI 报价建议（Issue #6 P0 整改）：
    - 无依据/引擎无测算结果时不出任何数字（D-F：无追溯依据不输出数字）；
    - 仅返回参考区间（源自确定性引擎结果 ±5%），不再返回 recommended 单点数字；
    - 区间只作决策参考，正式报价仅走 calculate/recalc/strategies/apply 确定性链路。
    """
    calc = await session.scalar(
        select(QuoteCalc).where(
            QuoteCalc.id == body["calc_id"],
            QuoteCalc.enterprise_id == user.enterprise_id,
        )
    )
    if calc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="测算不存在")
    basis = body.get("basis")
    suggested = (calc.result or {}).get("suggested")
    if not basis or suggested is None:
        return {"unavailable": True, "message": "无可追溯依据或引擎无测算结果，不输出报价数字（D-F）"}
    low = round(float(suggested) * 0.95, 2)
    high = round(float(suggested) * 1.05, 2)
    suggest = {
        "price_range": [low, high],
        "reasons": [f"参考区间基于确定性引擎测算价的 ±5%；依据：{basis}"],
        "assumptions": list((calc.result or {}).get("adjustments", {}).keys()),
        "confidence": "low",
        "risk_level": "medium",
        "is_ai_suggest": True,
    }
    calc.ai_suggest = suggest
    await session.commit()
    return _money_dict(suggest)


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
    if deliverable.deliverable_type != 3:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="目标成果不是报价单")
    if deliverable.project_id != calc.project_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="测算与报价单成果不属于同一项目")
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


@router.get("")
async def list_calculations(
    project_id: int | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> dict:
    """测算列表（Issue #2 #30：恢复项目最近/历史报价测算）。"""
    query = (
        select(QuoteCalc)
        .where(QuoteCalc.enterprise_id == user.enterprise_id)
        .order_by(QuoteCalc.id.desc())
        .limit(100)
    )
    if project_id:
        query = query.where(QuoteCalc.project_id == project_id)
    rows = (await session.scalars(query)).all()
    return {
        "items": [
            {
                "calc_id": c.id,
                "project_id": c.project_id,
                "params": c.params,
                "result": c.result,
                "status": c.status,
                "applied_version_no": c.applied_version_no,
                "has_strategy": bool(c.strategy_results),
                "has_ai_suggest": bool(c.ai_suggest),
                "sample_count": len(c.snapshot_refs or []),
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ]
    }


@router.get("/{calc_id}")
async def calculation_detail(
    calc_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> dict:
    """测算详情（Issue #2 #33：依据/结果/策略/AI 建议/冻结样本）。"""
    calc = await session.scalar(
        select(QuoteCalc).where(
            QuoteCalc.id == calc_id,
            QuoteCalc.enterprise_id == user.enterprise_id,
        )
    )
    if calc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="测算不存在")
    samples: list[dict] = []
    snapshot_ids = calc.snapshot_refs or []
    if snapshot_ids:
        rows = (
            await session.scalars(
                select(HistoryPriceSnapshot).where(
                    HistoryPriceSnapshot.id.in_(snapshot_ids),
                    HistoryPriceSnapshot.enterprise_id == user.enterprise_id,
                )
            )
        ).all()
        samples = [
            {
                "sample_id": r.id,
                "material_name": r.material_name,
                "material_code": r.material_code,
                "spec": r.spec,
                "region": r.region,
                "win_price": float(r.win_price),
                "win_date": r.win_date.isoformat(),
                "source_hash": r.source_hash,
            }
            for r in rows
        ]
    return {
        "calc_id": calc.id,
        "project_id": calc.project_id,
        "params": calc.params,
        "result": calc.result,
        "strategy_results": calc.strategy_results or {},
        "ai_suggest": calc.ai_suggest,
        "status": calc.status,
        "applied_version_no": calc.applied_version_no,
        "applied_at": calc.applied_at.isoformat() if calc.applied_at else None,
        "created_at": calc.created_at.isoformat() if calc.created_at else None,
        "samples": samples,
    }


@router.get("/history/samples/{sample_id}")
async def sample_detail(
    sample_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> dict:
    """单条历史报价样本（Issue #2 #35）。"""
    row = await session.scalar(
        select(HistoryPriceSnapshot).where(
            HistoryPriceSnapshot.id == sample_id,
            HistoryPriceSnapshot.enterprise_id == user.enterprise_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="样本不存在")
    return _money_dict({
        "sample_id": row.id,
        "provider_id": row.provider_id,
        "material_name": row.material_name,
        "material_code": row.material_code,
        "spec": row.spec,
        "region": row.region,
        "win_price": float(row.win_price),
        "win_date": row.win_date.isoformat(),
        "source_hash": row.source_hash,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
    })


@router.get("/history/{material_ref}/trend")
async def material_trend(
    material_ref: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.QUOTE_CALCULATE)),
) -> dict:
    """物料趋势与可比样本统计（Issue #2 #36，确定性计算）。"""
    samples = await provider.get_material_samples(material_ref)
    prices = sorted(s["win_price"] for s in samples)
    n = len(prices)
    regions: dict[str, list[float]] = {}
    for s in samples:
        regions.setdefault(s["region"], []).append(s["win_price"])
    latest = max(samples, key=lambda s: s["win_date"]) if samples else None
    return _money_dict({
        "material_ref": material_ref,
        "sample_count": n,
        "min_price": prices[0] if n else None,
        "max_price": prices[-1] if n else None,
        "avg_price": round(sum(prices) / n, 2) if n else None,
        "median_price": round(median(prices), 2) if n else None,
        "latest_price": latest["win_price"] if latest else None,
        "latest_date": latest["win_date"].isoformat() if latest else None,
        "region_breakdown": {
            k: {"count": len(v), "avg": round(sum(v) / len(v), 2)} for k, v in regions.items()
        },
        "readonly": True,
    })
