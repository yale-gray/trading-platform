"""Hand-calculated indicator reference cases (SPEC-005 section 6.1-6.2, T6).

Expected values are worked out by hand in comments, not copied from the code's output.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradebot.strategy.indicators import rsi, sma

pytestmark = pytest.mark.unit

NAN = float("nan")


def test_sma3_matches_hand_calculated_values() -> None:
    # 6.1 closes [10, 12, 14, 16, 12], n = 3
    #   t=2: (10+12+14)/3 = 36/3 = 12
    #   t=3: (12+14+16)/3 = 42/3 = 14
    #   t=4: (14+16+12)/3 = 42/3 = 14
    #   t=0,1: fewer than 3 bars, so NaN (not partial means)
    x = pd.Series([10.0, 12.0, 14.0, 16.0, 12.0])
    out = sma(x, 3)
    expected = np.array([NAN, NAN, 12.0, 14.0, 14.0])
    np.testing.assert_allclose(out.to_numpy(), expected, atol=1e-9, equal_nan=True)


def test_sma3_leading_values_are_nan_not_partial_means() -> None:
    # 6.1: the first n-1 = 2 values must be NaN, the first valid bar is index 2
    out = sma(pd.Series([10.0, 12.0, 14.0, 16.0, 12.0]), 3)
    assert np.isnan(out.iloc[0])
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(12.0, abs=1e-9)


def test_rsi2_matches_wilder_hand_calculated_values() -> None:
    # 6.2 closes [100, 102, 101, 99, 98], n = 2, Wilder alpha = 1/2.
    # Changes d: +2, -1, -2, -1  ->  U (gain): 2, 0, 0, 0   D (loss): 0, 1, 2, 1
    # Seed at index 2 = simple mean of the first two changes:
    #   AU = (2+0)/2 = 1,  AD = (0+1)/2 = 0.5,  RS = 2
    #   RSI = 100 - 100/(1+2) = 100 - 33.3333 = 66.6667
    # Index 3 (d = -2): AU = (1*1 + 0)/2 = 0.5,  AD = (1*0.5 + 2)/2 = 1.25,  RS = 0.4
    #   RSI = 100 - 100/1.4 = 28.5714
    # Index 4 (d = -1): AU = (1*0.5 + 0)/2 = 0.25,  AD = (1*1.25 + 1)/2 = 1.125,  RS = 2/9
    #   RSI = 100 - 100/(11/9) = 100 - 81.8182 = 18.1818
    x = pd.Series([100.0, 102.0, 101.0, 99.0, 98.0])
    out = rsi(x, 2)
    expected = np.array([NAN, NAN, 66.6667, 28.5714, 18.1818])
    np.testing.assert_allclose(out.to_numpy(), expected, atol=1e-4, equal_nan=True)


def test_rsi2_leading_values_are_nan_before_first_valid_bar() -> None:
    # 6.2: RSI(n) first valid bar is n = 2; indices 0 and 1 are NaN
    out = rsi(pd.Series([100.0, 102.0, 101.0, 99.0, 98.0]), 2)
    assert np.isnan(out.iloc[0])
    assert np.isnan(out.iloc[1])
    assert not np.isnan(out.iloc[2])


def test_rsi_flat_fixture_gives_50_without_division_by_zero(edge_cache: Path) -> None:
    # ZZFLAT: every close is 100.0, so every change is 0 -> AU = AD = 0.
    # SPEC-002 convention: AD = 0 and AU = 0 gives RSI 50 (not NaN, not inf).
    bars = pd.read_parquet(edge_cache / "yfinance/ZZFLAT/gen-0001/bars.parquet")
    out = rsi(bars["close"], 2).to_numpy()
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], 50.0, atol=1e-9)
    assert np.isfinite(out[2:]).all()


def test_rsi_up_fixture_gives_100_without_division_by_zero(edge_cache: Path) -> None:
    # ZZUP: close strictly increases, so every change is > 0 -> AD = 0 and AU > 0.
    # SPEC-002 convention: AD = 0 with AU > 0 gives RSI 100 (not NaN, not inf).
    bars = pd.read_parquet(edge_cache / "yfinance/ZZUP/gen-0001/bars.parquet")
    assert (bars["close"].diff().dropna() > 0).all()
    out = rsi(bars["close"], 2).to_numpy()
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], 100.0, atol=1e-9)
    assert np.isfinite(out[2:]).all()
