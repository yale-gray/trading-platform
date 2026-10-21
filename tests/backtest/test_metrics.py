"""Metrics formulas, NaN guards and annualisation choice (SPEC-002 section 7, AC 7-8)."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from tradebot.backtest.metrics import compute_metrics, periods_per_year
from tradebot.backtest.trades import ClosedTrade


def _equity(values: list[float], start: str = "2024-01-01") -> pd.Series:
    first = pd.Timestamp(start, tz="UTC")
    idx = pd.DatetimeIndex([first + pd.Timedelta(days=i) for i in range(len(values))])
    return pd.Series(values, index=idx, dtype="float64")


def _flat_exposure(equity: pd.Series) -> pd.Series:
    return pd.Series(0.0, index=equity.index, dtype="float64")


def _trade(net: str) -> ClosedTrade:
    return ClosedTrade(
        sleeve_id="core_momentum",
        symbol="SPY",
        entry_asof=date(2024, 1, 2),
        exit_asof=date(2024, 1, 9),
        qty=Decimal("1"),
        entry_price=Decimal("100"),
        exit_price=Decimal("100"),
        fees=Decimal("0"),
        net_pnl=Decimal(net),
        exit_reason="signal",
    )


# AC 7 / annualisation: P = 252 for an equity-only run, 365 whenever crypto is present.
@pytest.mark.parametrize(
    ("items", "expected"),
    [
        (["SPY", "QQQ"], 252),
        (["equity"], 252),
        (["BTC/USD"], 365),
        (["ETH/USD"], 365),
        (["crypto"], 365),
        (["SPY", "BTC/USD"], 365),
    ],
)
def test_periods_per_year_is_252_for_equity_and_365_with_any_crypto(
    items: list[str], expected: int
) -> None:
    assert periods_per_year(items) == expected


def test_win_rate_counts_only_strictly_positive_net_pnl() -> None:
    # Net P&L +10 wins; 0 does not (win means > 0); so 1 win out of 2 trades = 0.5.
    eq = _equity([100.0, 101.0, 102.0])
    m = compute_metrics(eq, [_trade("10.00"), _trade("0.00")], _flat_exposure(eq), 252)
    assert m.win_rate == pytest.approx(0.5)
    assert m.trade_count == 2


def test_no_closed_trades_gives_nan_win_rate_not_zero() -> None:
    eq = _equity([100.0, 101.0, 102.0])
    m = compute_metrics(eq, [], _flat_exposure(eq), 252)
    assert math.isnan(m.win_rate)
    assert m.trade_count == 0


def test_zero_variance_returns_give_nan_sharpe_and_sortino_never_inf() -> None:
    # Flat equity: every return is 0, so the standard deviation is 0 -> Sharpe is NaN.
    # Steady growth 100 -> 110 -> 121: returns are both 0.1, so std is 0 -> Sharpe NaN.
    # Sortino: no negative returns, so the downside deviation is 0 -> NaN.
    flat = _equity([100.0, 100.0, 100.0, 100.0])
    m_flat = compute_metrics(flat, [], _flat_exposure(flat), 252)
    assert math.isnan(m_flat.sharpe)
    assert math.isnan(m_flat.sortino)
    assert m_flat.total_return == 0.0

    grow = _equity([100.0, 110.0, 121.0])
    m_grow = compute_metrics(grow, [], _flat_exposure(grow), 252)
    assert math.isnan(m_grow.sharpe)
    assert math.isnan(m_grow.sortino)
    assert m_grow.total_return == pytest.approx(0.21, rel=1e-12)  # 121/100 - 1


def test_single_point_curve_has_undefined_sharpe_sortino_and_cagr() -> None:
    # With one equity point there are no returns, so Sharpe, Sortino and CAGR are NaN.
    eq = _equity([100.0])
    m = compute_metrics(eq, [], _flat_exposure(eq), 252)
    assert math.isnan(m.sharpe)
    assert math.isnan(m.sortino)
    assert math.isnan(m.cagr)


def test_metrics_are_deterministic_for_identical_inputs() -> None:
    # Same equity, trades and exposure give an identical model (no hidden state or clock).
    eq = _equity([100.0, 105.0, 97.0, 110.0])
    trades = [_trade("5.00"), _trade("-3.00")]
    exp = pd.Series([0.1, 0.2, 0.2, 0.3], index=eq.index, dtype="float64")
    a = compute_metrics(eq, trades, exp, 252)
    b = compute_metrics(eq, trades, exp, 252)
    assert a.model_dump() == b.model_dump()
    assert a.periods_per_year == 252
