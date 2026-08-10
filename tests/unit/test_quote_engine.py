from __future__ import annotations

import pytest

from app.services.quote_engine import (
    MIN_SAMPLES,
    QuoteParams,
    calculate,
    eligible_samples,
    remove_outliers,
    strategy_profit,
    strategy_win,
)


def _samples(n: int = 6) -> list[dict]:
    return [
        {
            "material_ref": "CABLE",
            "win_price": 100.0 + i,
            "unit": "元",
            "currency": "CNY",
            "tax_included": True,
        }
        for i in range(n)
    ]


def test_eligible_samples_filters_mismatched_caliber():
    samples = _samples(3)
    samples.append({"win_price": 10, "currency": "USD", "tax_included": True, "unit": "元"})
    samples.append({"win_price": 20, "currency": "CNY", "tax_included": False, "unit": "元"})
    params = QuoteParams(material_ref="CABLE", cost=50, min_profit_rate=0.05)
    prices = eligible_samples(samples, params)
    assert len(prices) == 3
    assert all(p > 90 for p in prices)


def test_remove_outliers():
    assert remove_outliers([100, 101, 102]) == [100, 101, 102]
    assert 1000 not in remove_outliers([100, 101, 102, 1000])


def test_calculate_insufficient_samples():
    params = QuoteParams(material_ref="CABLE", cost=50, min_profit_rate=0.05)
    with pytest.raises(ValueError, match="数据不足"):
        calculate(params, _samples(MIN_SAMPLES - 1))


def test_calculate_respects_min_price_and_cap():
    params = QuoteParams(
        material_ref="CABLE",
        cost=100,
        min_profit_rate=0.2,
        cap=130,
        adjustments={"region": 0.1},
    )
    result = calculate(params, _samples(6))
    assert result["suggested"] >= result["min_price"]
    assert result["suggested"] <= params.cap
    assert result["engine_version"]


def test_calculate_rejects_infeasible_cap():
    params = QuoteParams(
        material_ref="CABLE",
        cost=100,
        min_profit_rate=0.2,
        cap=105,
    )
    with pytest.raises(ValueError, match="上限低于最低毛利价"):
        calculate(params, _samples(6))


def test_strategy_win_constraints():
    params = QuoteParams(material_ref="CABLE", cost=90, min_profit_rate=0.1, cap=130)
    result = calculate(params, _samples(6))
    win = strategy_win(result, params)
    assert win["suggested_price"] >= result["min_price"]
    assert win["gross_margin"] >= 0.1


def test_strategy_profit_uses_cap():
    params = QuoteParams(material_ref="CABLE", cost=90, min_profit_rate=0.1, cap=130)
    result = calculate(params, _samples(6))
    profit = strategy_profit(result, params)
    assert profit["suggested_price"] == 130
