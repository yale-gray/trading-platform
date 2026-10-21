"""Unit tests for devconsole/lib/charts.py: plotly figure builders (SPEC-006 AC 3)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from devconsole.lib.charts import (
    OSCILLATOR_INDICATORS,
    PRICE_INDICATORS,
    candlestick_with_volume,
    compute_overlays,
    indicator_subplot,
)
from devconsole.lib.sources import FULL_END, FULL_START, DataSource, load_bars
from tradebot.strategy.indicators import INDICATORS

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def spy_bars() -> pd.DataFrame:
    return load_bars(DataSource.SYNTHETIC, "SPY", FULL_START, FULL_END).bars


@pytest.fixture(scope="module")
def btc_bars() -> pd.DataFrame:
    return load_bars(DataSource.SYNTHETIC, "BTC/USD", FULL_START, FULL_END).bars


# --------------------------------------------------------------------------- candlestick


def test_candlestick_with_volume_and_overlays_trace_and_row_counts(spy_bars: pd.DataFrame) -> None:
    overlays = compute_overlays(spy_bars, [("sma", {"n": 50}), ("sma", {"n": 200})])

    fig = candlestick_with_volume(spy_bars, "SPY", overlays)

    # One candlestick, two overlay lines, one volume bar trace.
    assert [t.type for t in fig.data] == ["candlestick", "scatter", "scatter", "bar"]
    assert [t.name for t in fig.data] == ["SPY", "SMA 50", "SMA 200", "Volume"]
    candles, sma50, sma200, volume = fig.data
    assert len(candles.open) == len(spy_bars) == 1761
    assert len(sma50.y) == len(spy_bars)
    assert len(sma200.y) == len(spy_bars)
    assert len(volume.y) == len(spy_bars)


def test_candlestick_without_overlays_has_two_traces(spy_bars: pd.DataFrame) -> None:
    fig = candlestick_with_volume(spy_bars, "SPY", {})
    assert [t.type for t in fig.data] == ["candlestick", "bar"]


def test_overlay_y_values_equal_indicator_output(spy_bars: pd.DataFrame) -> None:
    overlays = compute_overlays(spy_bars, [("sma", {"n": 50}), ("sma", {"n": 200})])

    fig = candlestick_with_volume(spy_bars, "SPY", overlays)

    expected_50 = INDICATORS["sma"](spy_bars["close"], n=50).to_numpy()
    expected_200 = INDICATORS["sma"](spy_bars["close"], n=200).to_numpy()
    np.testing.assert_array_equal(np.asarray(fig.data[1].y, dtype=float), expected_50)
    np.testing.assert_array_equal(np.asarray(fig.data[2].y, dtype=float), expected_200)


def test_equity_chart_hides_weekends_with_rangebreaks(spy_bars: pd.DataFrame) -> None:
    fig = candlestick_with_volume(spy_bars, "SPY", {})

    for axis in (fig.layout.xaxis, fig.layout.xaxis2):
        assert len(axis.rangebreaks) == 1
        assert list(axis.rangebreaks[0].bounds) == ["sat", "mon"]


def test_crypto_chart_has_no_rangebreaks(btc_bars: pd.DataFrame) -> None:
    fig = candlestick_with_volume(btc_bars, "BTC/USD", {})

    assert len(fig.layout.xaxis.rangebreaks) == 0
    assert len(fig.layout.xaxis2.rangebreaks) == 0


def test_candlestick_colours_volume_by_direction(spy_bars: pd.DataFrame) -> None:
    fig = candlestick_with_volume(spy_bars, "SPY", {})
    colours = list(fig.data[-1].marker.color)
    up = (spy_bars["close"] >= spy_bars["open"]).to_numpy()
    assert colours == ["#2ca02c" if u else "#d62728" for u in up]


# --------------------------------------------------------------------------- oscillator


def test_oscillator_figure_has_one_trace_and_reference_lines(spy_bars: pd.DataFrame) -> None:
    rsi = compute_overlays(spy_bars, [("rsi", {"n": 2})])["RSI 2"]

    fig = indicator_subplot({"RSI 2": rsi}, "RSI", hlines=(10, 70))

    assert [t.type for t in fig.data] == ["scatter"]
    assert fig.data[0].name == "RSI 2"
    assert len(fig.layout.shapes) == 2  # one dotted line per reference level
    assert fig.layout.title.text == "RSI"
    assert fig.layout.height == 280


def test_oscillator_figure_without_reference_lines(spy_bars: pd.DataFrame) -> None:
    atr = compute_overlays(spy_bars, [("atr", {"n": 14})])["ATR 14"]
    fig = indicator_subplot({"ATR 14": atr}, "ATR")
    assert len(fig.layout.shapes) == 0


def test_oscillator_y_values_equal_rsi_output(spy_bars: pd.DataFrame) -> None:
    rsi = compute_overlays(spy_bars, [("rsi", {"n": 2})])["RSI 2"]
    fig = indicator_subplot({"RSI 2": rsi}, "RSI", hlines=(10, 70))
    np.testing.assert_array_equal(
        np.asarray(fig.data[0].y, dtype=float), INDICATORS["rsi"](spy_bars["close"], n=2).to_numpy()
    )


# --------------------------------------------------------------------------- compute_overlays


def test_compute_overlays_labels_and_splits_dataframe_outputs(spy_bars: pd.DataFrame) -> None:
    picks = [
        ("sma", {"n": 200}),
        ("rsi", {"n": 2}),
        ("atr", {"n": 14}),
        ("macd", {"fast": 12, "slow": 26, "signal": 9}),
        ("bbands", {"n": 20, "k": 2.0}),
    ]

    overlays = compute_overlays(spy_bars, picks)

    assert "SMA 200" in overlays
    assert "RSI 2" in overlays
    assert "ATR 14" in overlays
    assert {"MACD 12 26 9 macd", "MACD 12 26 9 signal", "MACD 12 26 9 hist"} <= set(overlays)
    assert {"BBANDS 20 2 upper", "BBANDS 20 2 mid", "BBANDS 20 2 lower"} <= set(overlays)
    for series in overlays.values():
        assert len(series) == len(spy_bars)
        assert series.index.equals(spy_bars.index)


def test_compute_overlays_atr_uses_high_low_close(spy_bars: pd.DataFrame) -> None:
    atr = compute_overlays(spy_bars, [("atr", {"n": 14})])["ATR 14"]
    expected = INDICATORS["atr"](spy_bars["high"], spy_bars["low"], spy_bars["close"], n=14)
    np.testing.assert_array_equal(atr.to_numpy(), expected.to_numpy())


def test_compute_overlays_unknown_indicator_raises_value_error(spy_bars: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="unknown indicator 'nope'"):
        compute_overlays(spy_bars, [("nope", {"n": 5})])


def test_compute_overlays_duplicate_label_raises_value_error(spy_bars: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="duplicate overlay label"):
        compute_overlays(spy_bars, [("sma", {"n": 5}), ("sma", {"n": 5})])


def test_compute_overlays_empty_picks_gives_empty_dict(spy_bars: pd.DataFrame) -> None:
    assert compute_overlays(spy_bars, []) == {}


def test_kind_sets_cover_every_indicator_once() -> None:
    assert PRICE_INDICATORS.isdisjoint(OSCILLATOR_INDICATORS)
    assert PRICE_INDICATORS | OSCILLATOR_INDICATORS == set(INDICATORS)
