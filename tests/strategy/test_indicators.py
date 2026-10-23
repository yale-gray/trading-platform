"""Tests for tradebot.strategy.indicators (SPEC-002 section 4, T7a, AC 2).

Expected values are hand-computed (in comments) from the reference vectors V1-V8 in
docs/specs/002-strategy-backtest.md, not copied from the implementation's output.
Offline and deterministic: small literal series plus a seeded random walk.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradebot.strategy.indicators import (
    INDICATORS,
    atr,
    bbands,
    devatr,
    ema,
    macd,
    noise_lower,
    noise_upper,
    ret,
    rsi,
    sma,
    vol,
)

TOL = 1e-9
NAN = math.nan


def _s(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


def _assert_series(actual: pd.Series, expected: list[float]) -> None:
    exp = np.asarray(expected, dtype="float64")
    act = actual.to_numpy(dtype="float64")
    assert len(act) == len(exp)
    np.testing.assert_array_equal(np.isnan(act), np.isnan(exp), err_msg="NaN positions differ")
    np.testing.assert_allclose(act, exp, atol=TOL, rtol=0, equal_nan=True)


def _assert_frame(actual: pd.DataFrame, expected: dict[str, list[float]]) -> None:
    assert list(actual.columns) == list(expected)
    for col, exp in expected.items():
        _assert_series(actual[col], exp)


# ---------------------------------------------------------------- V1-V8 reference vectors

X_V = _s([10, 12, 11, 13, 14, 12, 15])


def test_sma_matches_v1_with_nan_warmup():
    # n=3, first valid idx 2.
    # idx2 = (10+12+11)/3 = 11; idx3 = (12+11+13)/3 = 12; idx4 = (11+13+14)/3 = 38/3;
    # idx5 = (13+14+12)/3 = 13; idx6 = (14+12+15)/3 = 41/3.
    _assert_series(sma(X_V, 3), [NAN, NAN, 11, 12, 38 / 3, 13, 41 / 3])


def test_ema_matches_v2_with_sma_seed():
    # n=3, alpha = 2/4 = 0.5, seed at idx2 = mean(10,12,11) = 11.
    # idx3 = .5*13 + .5*11 = 12; idx4 = .5*14 + .5*12 = 13;
    # idx5 = .5*12 + .5*13 = 12.5; idx6 = .5*15 + .5*12.5 = 13.75.
    _assert_series(ema(X_V, 3), [NAN, NAN, 11, 12, 13, 12.5, 13.75])


def test_ret_matches_v3_lookback_two():
    # x=[100,110,121,121], n=2, skip=0, first valid idx 2.
    # idx2 = 121/100 - 1 = 0.21; idx3 = 121/110 - 1 = 0.10.
    _assert_series(ret(_s([100, 110, 121, 121]), 2), [NAN, NAN, 0.21, 0.10])


def test_ret_matches_v3b_with_skip_one():
    # n=1, skip=1, first valid idx n+skip = 2.
    # idx2 = x1/x0 - 1 = 110/100 - 1 = 0.10; idx3 = x2/x1 - 1 = 121/110 - 1 = 0.10.
    _assert_series(ret(_s([100, 110, 121, 121]), 1, skip=1), [NAN, NAN, 0.10, 0.10])


def test_rsi_matches_v4_wilder():
    # n=2, x=[10,11,10,12,11,13]. d=[nan,1,-1,2,-1,2]; U=[nan,1,0,2,0,2]; D=[nan,0,1,0,1,0].
    # idx2: AU=(1+0)/2=0.5, AD=(0+1)/2=0.5, RS=1, RSI=50.
    # idx3: AU=(0.5+2)/2=1.25, AD=(0.5+0)/2=0.25, RS=5, RSI=100-100/6=250/3.
    # idx4: AU=(1.25+0)/2=0.625, AD=(0.25+1)/2=0.625, RSI=50.
    # idx5: AU=(0.625+2)/2=1.3125, AD=(0.625+0)/2=0.3125, RS=4.2, RSI=100-100/5.2=1050/13.
    _assert_series(rsi(_s([10, 11, 10, 12, 11, 13]), 2), [NAN, NAN, 50, 250 / 3, 50, 1050 / 13])


@pytest.mark.parametrize(
    ("x", "edge"),
    [
        pytest.param([1, 2, 3, 4], 100.0, id="all_up_AD_zero_AU_positive"),
        pytest.param([5, 5, 5, 5], 50.0, id="flat_AD_and_AU_zero"),
        pytest.param([4, 3, 2, 1], 0.0, id="all_down"),
    ],
)
def test_rsi_v4b_edge_cases(x: list[float], edge: float):
    # n=2: idx 0,1 NaN; idx 2 and 3 take the edge value.
    _assert_series(rsi(_s(x), 2), [NAN, NAN, edge, edge])


def test_macd_matches_v5():
    # fast=2 (alpha 2/3), slow=3 (alpha 1/2), signal=2 (alpha 2/3). x = X_V.
    # ema_fast: seed idx1 = 11, idx2 = 11, idx3 = 37/3, idx4 = 121/9, idx5 = 337/27, idx6 = 1147/81.
    # ema_slow: seed idx2 = 11, then 12, 13, 12.5, 13.75.
    # macd = ema_fast - ema_slow = [nan, nan, 0, 1/3, 4/9, -1/54, 133/324].
    # signal: seed idx3 = mean(0, 1/3) = 1/6; idx4 = 19/54; idx5 = 17/162; idx6 = 25/81.
    # hist = macd - signal = [nan, nan, nan, 1/6, 5/54, -10/81, 11/108].
    # First valid macd = slow-1 = 2; first valid signal/hist = slow+signal-2 = 3.
    out = macd(X_V, fast=2, slow=3, signal=2)
    _assert_frame(
        out,
        {
            "macd": [NAN, NAN, 0, 1 / 3, 4 / 9, -1 / 54, 133 / 324],
            "signal": [NAN, NAN, NAN, 1 / 6, 19 / 54, 17 / 162, 25 / 81],
            "hist": [NAN, NAN, NAN, 1 / 6, 5 / 54, -10 / 81, 11 / 108],
        },
    )


def test_bbands_matches_v6_population_std():
    # n=8, k=2, x=[2,4,4,4,5,5,7,9]. Only idx 7 is valid (first valid n-1 = 7).
    # mean = 40/8 = 5; squared deviations sum = 9+1+1+1+0+0+4+16 = 32;
    # population var = 32/8 = 4, sd = 2. upper = 9, lower = 1.
    # pctb = (9-1)/(9-1) = 1.0; bandwidth = (9-1)/5 = 1.6.
    x = _s([2, 4, 4, 4, 5, 5, 7, 9])
    out = bbands(x, n=8, k=2)
    nan7 = [NAN] * 7
    _assert_frame(
        out,
        {
            "mid": nan7 + [5],
            "upper": nan7 + [9],
            "lower": nan7 + [1],
            "pctb": nan7 + [1.0],
            "bandwidth": nan7 + [1.6],
        },
    )


def test_bbands_matches_v6b_flat_series():
    # n=3, k=2, x=[5,5,5,5]. First valid idx 2. sd = 0 so upper = lower = mid = 5.
    # pctb is NaN where upper == lower; bandwidth = 0/5 = 0.
    out = bbands(_s([5, 5, 5, 5]), 3, 2)
    _assert_frame(
        out,
        {
            "mid": [NAN, NAN, 5, 5],
            "upper": [NAN, NAN, 5, 5],
            "lower": [NAN, NAN, 5, 5],
            "pctb": [NAN, NAN, NAN, NAN],
            "bandwidth": [NAN, NAN, 0, 0],
        },
    )


def test_atr_matches_v7_wilder():
    # n=2. TR_0 undefined. TR_1 = max(3, 3, 0) = 3; TR_2 = max(3, 2, 1) = 3;
    # TR_3 = max(3, 0, 3) = 3; TR_4 = max(3, 4, 1) = 4.
    # ATR_2 = mean(TR_1, TR_2) = 3; ATR_3 = (1*3 + 3)/2 = 3; ATR_4 = (1*3 + 4)/2 = 3.5.
    h = _s([10, 12, 13, 12, 14])
    lo = _s([8, 9, 10, 9, 11])
    c = _s([9, 11, 12, 10, 13])
    _assert_series(atr(h, lo, c, 2), [NAN, NAN, 3, 3, 3.5])


def test_vol_matches_v8_sample_std_of_returns():
    # n=2, x=[100,110,99,99]. r = [nan, 0.1, -0.1, 0.0].
    # idx2 over (0.1, -0.1): mean 0, sum sq 0.02, ddof=1 -> var 0.02, sd sqrt(0.02).
    # idx3 over (-0.1, 0.0): mean -0.05, sum sq 0.005, var 0.005, sd sqrt(0.005).
    _assert_series(vol(_s([100, 110, 99, 99]), 2), [NAN, NAN, math.sqrt(0.02), math.sqrt(0.005)])


# ---------------------------------------------------------------- error cases


def test_ema_allows_leading_nan_and_seeds_from_first_valid():
    # n=2, first valid f=2 (x=10), seed idx3 = mean(10,12) = 11.
    # alpha = 2/3; idx4 = 2/3*11 + 1/3*11 = 11.
    _assert_series(ema(_s([NAN, NAN, 10, 12, 11]), 2), [NAN, NAN, NAN, 11, 11])


def test_ema_raises_on_nan_after_first_valid_value():
    with pytest.raises(ValueError):
        ema(_s([NAN, 10, NAN, 12]), 2)


def test_rsi_raises_on_nan_in_input():
    with pytest.raises(ValueError):
        rsi(_s([10, 11, 12, NAN, 13, 14]), 2)


def test_atr_raises_on_length_mismatch():
    h = _s([10, 12, 13, 12, 14])
    lo = _s([8, 9, 10, 9])
    c = _s([9, 11, 12, 10, 13])
    with pytest.raises(ValueError):
        atr(h, lo, c, 2)


def test_atr_raises_on_index_mismatch():
    h = _s([10, 12, 13, 12, 14])
    lo = pd.Series([8, 9, 10, 9, 11], index=range(10, 15), dtype="float64")
    c = _s([9, 11, 12, 10, 13])
    with pytest.raises(ValueError):
        atr(h, lo, c, 2)


def test_atr_raises_on_nan_in_input():
    h = _s([10, 12, NAN, 12, 14])
    lo = _s([8, 9, 10, 9, 11])
    c = _s([9, 11, 12, 10, 13])
    with pytest.raises(ValueError):
        atr(h, lo, c, 2)


def test_macd_raises_when_fast_not_below_slow():
    with pytest.raises(ValueError):
        macd(X_V, fast=3, slow=3, signal=2)
    with pytest.raises(ValueError):
        macd(X_V, fast=4, slow=3, signal=2)


def test_ret_raises_on_negative_skip():
    with pytest.raises(ValueError):
        ret(X_V, 2, skip=-1)


_N0_CALLS = {
    "sma": lambda: sma(X_V, 0),
    "ema": lambda: ema(X_V, 0),
    "ret": lambda: ret(X_V, 0),
    "rsi": lambda: rsi(X_V, 0),
    "macd_fast": lambda: macd(X_V, fast=0, slow=3, signal=2),
    "macd_signal": lambda: macd(X_V, fast=2, slow=3, signal=0),
    "bbands": lambda: bbands(X_V, 0),
    "atr": lambda: atr(X_V, X_V, X_V, 0),
    "devatr": lambda: devatr(X_V, X_V, X_V, 0),
    "vol": lambda: vol(X_V, 0),
}


@pytest.mark.parametrize("name", sorted(_N0_CALLS))
def test_zero_length_window_raises(name: str):
    with pytest.raises(ValueError):
        _N0_CALLS[name]()


# ---------------------------------------------------------------- registry, index, truncation


def test_indicators_registry_has_exact_keys():
    assert set(INDICATORS) == {
        "sma", "ema", "ret", "rsi", "macd", "bbands", "atr", "devatr", "vol",
        "noise_upper", "noise_lower",
    }  # fmt: skip


def _random_walk_ohlc() -> tuple[pd.Series, pd.Series, pd.Series]:
    rng = np.random.default_rng(0)
    n = 300
    close_vals = 100.0 * np.exp(np.cumsum(0.01 * rng.standard_normal(n)))
    high_vals = close_vals * (1.0 + np.abs(0.005 * rng.standard_normal(n)))
    low_vals = close_vals * (1.0 - np.abs(0.005 * rng.standard_normal(n)))
    idx = pd.bdate_range("2020-01-01", periods=n)
    return (
        pd.Series(close_vals, index=idx, dtype="float64"),
        pd.Series(high_vals, index=idx, dtype="float64"),
        pd.Series(low_vals, index=idx, dtype="float64"),
    )


# Parameters used for every indicator in the truncation and index tests.
_CALLS = {
    "sma": lambda c, h, lo: sma(c, 5),
    "ema": lambda c, h, lo: ema(c, 5),
    "ret": lambda c, h, lo: ret(c, 3, skip=1),
    "rsi": lambda c, h, lo: rsi(c, 14),
    "macd": lambda c, h, lo: macd(c, fast=12, slow=26, signal=9),
    "bbands": lambda c, h, lo: bbands(c, 20, 2.0),
    "atr": lambda c, h, lo: atr(h, lo, c, 14),
    "devatr": lambda c, h, lo: devatr(h, lo, c, 5),
    "vol": lambda c, h, lo: vol(c, 20),
    "noise_upper": lambda c, h, lo: noise_upper(c, c, 5),
    "noise_lower": lambda c, h, lo: noise_lower(c, c, 5),
}

# Covers warm-up edges of every call above (sma/ema/ret 4, rsi/atr 14, bbands 19,
# macd line 25, macd signal/hist 33, vol 20) plus interior and late positions.
_SAMPLE_T = (4, 5, 14, 19, 20, 25, 33, 60, 150, 299)


def _as_frame(out: pd.Series | pd.DataFrame) -> pd.DataFrame:
    if isinstance(out, pd.DataFrame):
        return out
    return out.to_frame(name="value")


def test_calls_cover_every_registry_name():
    assert set(_CALLS) == set(INDICATORS)


@pytest.mark.parametrize("name", sorted(INDICATORS))
def test_output_keeps_input_index_length_and_float64(name: str):
    close, high, low = _random_walk_ohlc()
    out = _CALLS[name](close, high, low)
    frame = _as_frame(out)
    assert frame.index.equals(close.index)
    assert len(frame) == len(close)
    assert all(dtype == np.float64 for dtype in frame.dtypes)


@pytest.mark.parametrize("name", sorted(INDICATORS))
def test_truncation_invariance(name: str):
    close, high, low = _random_walk_ohlc()
    full = _as_frame(_CALLS[name](close, high, low))
    # Non-vacuous: the last bar is valid for every output column.
    assert np.isfinite(full.iloc[-1].to_numpy(dtype="float64")).all()
    for t in _SAMPLE_T:
        trunc = _as_frame(_CALLS[name](close.iloc[: t + 1], high.iloc[: t + 1], low.iloc[: t + 1]))
        for col in full.columns:
            np.testing.assert_allclose(
                trunc[col].iloc[-1],
                full[col].iloc[t],
                atol=TOL,
                rtol=0,
                equal_nan=True,
                err_msg=f"{name}.{col} at t={t} differs when computed on x[:t+1]",
            )


def test_devatr_is_distance_from_average_in_atr_units():
    """Hand calculation: closes 10,10,10,10,7 -> SMA(3) at t=4 is 9; ATR(2) via Wilder."""
    close = pd.Series([10.0, 10.0, 10.0, 10.0, 7.0])
    high = pd.Series([10.5, 10.5, 10.5, 10.5, 10.5])
    low = pd.Series([9.5, 9.5, 9.5, 9.5, 6.5])
    out = devatr(high, low, close, 3, atr_n=2)
    a = atr(high, low, close, 2)
    # TR: nan, 1, 1, 1, max(4, 0.5, 3.5) = 4 ; Wilder(2): seed t=2 -> 1, t=3 -> 1, t=4 -> 2.5
    assert a.iloc[4] == pytest.approx(2.5)
    assert out.iloc[4] == pytest.approx((7.0 - (10 + 10 + 7) / 3) / 2.5)
    assert out.iloc[:2].isna().all()  # first valid bar is max(n - 1, atr_n) = 2
    assert out.iloc[2] == pytest.approx(0.0)


def test_devatr_is_nan_when_atr_is_zero():
    flat = pd.Series([5.0] * 6)
    assert devatr(flat, flat, flat, 3, atr_n=2).isna().all()
