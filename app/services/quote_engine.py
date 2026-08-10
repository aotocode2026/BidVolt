"""QuoteEngine：确定性报价算法（4.7.4，纯函数，不依赖 LLM）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

ENGINE_VERSION = "1.0.0"
MIN_SAMPLES = 5


@dataclass
class QuoteParams:
    material_ref: str
    cost: float
    min_profit_rate: float
    unit: str = "元"
    currency: str = "CNY"
    tax_included: bool = True
    adjustments: dict = field(default_factory=dict)  # {"time": 0.02, "region": 0.01, "spec": -0.03}
    method: str = "median"  # median / avg_1y / avg_region / avg_spec
    cap: float | None = None
    score_formula: dict | None = None  # {"type": "linear", "k": -0.5, "b": 100}


def eligible_samples(samples: list[dict], params: QuoteParams) -> list[float]:
    """口径统一：单位/币种/含税不一致的样本不得参与计算（D-F）。"""
    prices: list[float] = []
    for s in samples:
        if s.get("unit") not in (None, params.unit):
            continue
        if s.get("currency") not in (None, params.currency):
            continue
        if s.get("tax_included") not in (None, params.tax_included):
            continue
        if s.get("win_price") is None:
            continue
        prices.append(float(s["win_price"]))
    return prices


def remove_outliers(prices: list[float]) -> list[float]:
    if len(prices) < 4:
        return prices
    mid = median(prices)
    # 简单稳健：剔除偏离中位数超过 50% 的样本
    return [p for p in prices if abs(p - mid) / mid <= 0.5]


def _score(price: float, formula: dict | None) -> float:
    if formula and formula.get("type") == "linear":
        return formula["k"] * price + formula["b"]
    return max(0.0, 100.0 - price / 10.0)  # 兜底：价格越低分越高


def base_price(prices: list[float], params: QuoteParams) -> float:
    if not prices:
        raise ValueError("无可计算样本")
    if params.method == "avg_1y":
        return sum(prices) / len(prices)
    return median(prices)


def adjust(base: float, params: QuoteParams) -> float:
    total = 1.0 + sum(params.adjustments.values())
    return round(base * total, 2)


def calculate(params: QuoteParams, samples: list[dict]) -> dict:
    prices = remove_outliers(eligible_samples(samples, params))
    if len(prices) < MIN_SAMPLES:
        raise ValueError(
            f"数据不足（有效样本 {len(prices)} < {MIN_SAMPLES}），无法可靠测算；可走 AI 参考建议"
        )
    base = base_price(prices, params)
    suggested = adjust(base, params)
    min_price = round(params.cost * (1 + params.min_profit_rate), 2)
    if params.cap is not None and params.cap < min_price:
        raise ValueError("无可行解：投标上限低于最低毛利价")
    if suggested < min_price:
        suggested = min_price
    if params.cap is not None and suggested > params.cap:
        suggested = round(params.cap, 2)
    return {
        "sample_count": len(prices),
        "median": round(median(prices), 2),
        "avg": round(sum(prices) / len(prices), 2),
        "base": round(base, 2),
        "adjustments": params.adjustments,
        "suggested": suggested,
        "min_price": min_price,
        "cap": params.cap,
        "engine_version": ENGINE_VERSION,
    }


def strategy_win(result: dict, params: QuoteParams) -> dict:
    price = result["suggested"]
    if price < result["min_price"] or (params.cap is not None and price > params.cap):
        raise ValueError("无可行解：成本/毛利约束冲突")
    score = _score(price, params.score_formula)
    return {
        "strategy": "win",
        "suggested_price": price,
        "score": round(score, 2),
        "gross_margin": round((price - params.cost) / price, 4),
        "risk_level": "low",
    }


def strategy_balance(result: dict, params: QuoteParams, alpha: float = 0.5) -> dict:
    price = result["suggested"]
    score = _score(price, params.score_formula)
    margin = (price - params.cost) / price if price else 0
    return {
        "strategy": "balance",
        "suggested_price": price,
        "score": round(score, 2),
        "gross_margin": round(margin, 4),
        "risk_level": "medium",
        "alpha": alpha,
    }


def strategy_profit(result: dict, params: QuoteParams) -> dict:
    price = params.cap if params.cap is not None else result["suggested"]
    score = _score(price, params.score_formula)
    margin = (price - params.cost) / price if price else 0
    return {
        "strategy": "profit",
        "suggested_price": round(price, 2),
        "score": round(score, 2),
        "gross_margin": round(margin, 4),
        "risk_level": "high",
    }
