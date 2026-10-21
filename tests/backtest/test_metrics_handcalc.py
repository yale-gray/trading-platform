"""Hand-calculated metrics references (SPEC-005 section 6.6; SPEC-002 section 7).

Every expected value is worked out in a comment from the formula, not copied from the code.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from tradebot.backtest.metrics import compute_metrics
from tradebot.backtest.trades import ClosedTrade


def _equity(values: list[float], start: str = "2024-01-01") -> pd.Series:
    """Equity points on consecutive calendar days from ``start`` (UTC midnight)."""
    first = pd.Timestamp(start, tz="UTC")
    idx = pd.DatetimeIndex([first + pd.Timedelta(days=i) for i in range(len(values))])
    return pd.Series(values, index=idx, dtype="float64")


def _zeros_like(equity: pd.Series) -> pd.Series:
    return pd.Series(0.0, index=equity.index, dtype="float64")


def test_drawdown_and_total_return_match_spec_example() -> None:
    # E = [100, 120, 90, 110].
    # Running peak: 100, 120, 120, 120. Ratios E/peak: 1, 1, 90/120 = 0.75, 110/120 = 0.91667.
    # max_drawdown = min(ratio - 1) = 0.75 - 1 = -0.25 (exactly -25.0%).
    # total_return = 110/100 - 1 = 0.10.
    eq = _equity([100.0, 120.0, 90.0, 110.0])
    m = compute_metrics(eq, [], _zeros_like(eq), periods_per_year=252)
    assert m.max_drawdown == pytest.approx(-0.25, abs=1e-12)
    assert m.total_return == pytest.approx(0.10, abs=1e-12)


def test_cagr_uses_365_25_day_years_and_ignores_p() -> None:
    # 2020-01-01 to 2024-01-01 spans 366 + 365 + 365 + 365 = 1461 days.
    # years = 1461 / 365.25 = 4.0 exactly.
    # CAGR = (400/100)^(1/4) - 1 = sqrt(2) - 1 = 0.41421356237.
    # With 365 days instead, years would be 4.0027 and CAGR would be about 0.41386 (not asserted).
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2020-01-01", tz="UTC"), pd.Timestamp("2024-01-01", tz="UTC")]
    )
    eq = pd.Series([100.0, 400.0], index=idx, dtype="float64")
    exp = pd.Series([0.0, 0.0], index=idx, dtype="float64")
    for p in (252, 365):
        m = compute_metrics(eq, [], exp, periods_per_year=p)
        assert m.cagr == pytest.approx(math.sqrt(2) - 1, rel=1e-9)


def test_sharpe_and_sortino_hand_calc_with_all_n_downside() -> None:
    # E = [100, 110, 99, 108.9]  ->  r = [+0.1, -0.1, +0.1], N = 3, rf = 0 so excess = r.
    # mean(r) = (0.1 - 0.1 + 0.1) / 3 = 1/30.
    # Sample variance (ddof=1): sum(r^2) - N*mean^2 = 0.03 - 3/900 = 2/75; divided by N-1 = 2
    #   gives 1/75, so std = 1/sqrt(75).
    # Sharpe = mean / std * sqrt(P) = (1/30) * sqrt(75) * sqrt(P) = sqrt(75 P) / 30.
    #   P = 252: sqrt(18900) / 30 = 4.5825757.
    # Downside deviation over ALL N periods: only r = -0.1 is negative, so
    #   DD = sqrt((1/N) * (-0.1)^2) = sqrt(0.01 / 3) = 1/sqrt(300).
    # Sortino = mean / DD * sqrt(P) = (1/30) * sqrt(300) * sqrt(P) = sqrt(300 P) / 30.
    #   P = 252: sqrt(75600) / 30 = 9.1651514.
    eq = _equity([100.0, 110.0, 99.0, 108.9])
    exp = _zeros_like(eq)
    m252 = compute_metrics(eq, [], exp, periods_per_year=252)
    assert m252.sharpe == pytest.approx(math.sqrt(75 * 252) / 30, rel=1e-9)
    assert m252.sortino == pytest.approx(math.sqrt(300 * 252) / 30, rel=1e-9)
    assert m252.sharpe == pytest.approx(4.5825757, rel=1e-6)
    assert m252.sortino == pytest.approx(9.1651514, rel=1e-6)

    # P = 365 (crypto-style calendar): the same series scales by sqrt(365) instead.
    #   Sharpe = sqrt(75 * 365) / 30, Sortino = sqrt(300 * 365) / 30.
    m365 = compute_metrics(eq, [], exp, periods_per_year=365)
    assert m365.sharpe == pytest.approx(math.sqrt(75 * 365) / 30, rel=1e-9)
    assert m365.sortino == pytest.approx(math.sqrt(300 * 365) / 30, rel=1e-9)
    assert m365.periods_per_year == 365


def test_win_rate_and_trade_count_hand_calc() -> None:
    # Three closed trades with net P&L +10, -5, +7: two winners out of three.
    # win_rate = 2 / 3; trade_count = 3.
    def trade(net: str) -> ClosedTrade:
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

    eq = _equity([100.0, 101.0, 100.5, 102.0])
    trades = [trade("10.00"), trade("-5.00"), trade("7.00")]
    m = compute_metrics(eq, trades, _zeros_like(eq), periods_per_year=252)
    assert m.win_rate == pytest.approx(2 / 3, rel=1e-12)
    assert m.trade_count == 3


def test_exposure_average_and_time_in_market_hand_calc() -> None:
    # Exposure per bar = [0.0, 0.2, 0.4, 0.6].
    # exposure_avg = (0 + 0.2 + 0.4 + 0.6) / 4 = 0.3.
    # time_in_market = bars with exposure > 0 / bars = 3 / 4 = 0.75.
    eq = _equity([100.0, 100.0, 100.0, 100.0])
    exp = pd.Series([0.0, 0.2, 0.4, 0.6], index=eq.index, dtype="float64")
    m = compute_metrics(eq, [], exp, periods_per_year=252)
    assert m.exposure_avg == pytest.approx(0.3, rel=1e-12)
    assert m.time_in_market == pytest.approx(0.75, rel=1e-12)
    # Flat equity: no returns, so the return and drawdown are exactly zero.
    assert m.total_return == 0.0
    assert m.max_drawdown == 0.0


def test_nonzero_risk_free_rate_compounds_per_period() -> None:
    # SPEC-002 section 7: rf_p = (1 + rf)^(1/P) - 1 (compounding), not rf / P.
    # E = [100, 110, 99, 108.9] -> r = [0.1, -0.1, 0.1]; rf = 4%, P = 252.
    # rf_p = 1.04^(1/252) - 1 = 0.00015564... (linear rf/P would be 0.00015873).
    # excess = r - rf_p; mean(excess) = 1/30 - rf_p; std is unchanged (shift) = 1/sqrt(75).
    # Sortino DD = sqrt(sum(min(excess, 0)^2) / 3) = sqrt((0.1 + rf_p)^2 / 3).
    rf_p = 1.04 ** (1 / 252) - 1
    eq = _equity([100.0, 110.0, 99.0, 108.9])
    m = compute_metrics(eq, [], _zeros_like(eq), periods_per_year=252, rf_annual=0.04)
    mean = 1 / 30 - rf_p
    assert m.sharpe == pytest.approx(mean / (1 / math.sqrt(75)) * math.sqrt(252), rel=1e-9)
    dd = math.sqrt((0.1 + rf_p) ** 2 / 3)
    assert m.sortino == pytest.approx(mean / dd * math.sqrt(252), rel=1e-9)
    assert abs(rf_p - 0.04 / 252) > 2e-6  # the compounding form really differs from linear
