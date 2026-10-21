"""Market data page: candlesticks, volume and indicators for one symbol. Read-only."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import streamlit as st

from devconsole.lib import charts
from devconsole.lib.sources import (
    FULL_END,
    FULL_START,
    DataSource,
    data_extent,
    resolve_root,
    universe,
)
from devconsole.ui import explainer, load_bars_cached, source_banner, source_sidebar, term

_LOOKBACK = timedelta(days=730)
_OSCILLATORS = {"None": None, "RSI": "rsi", "ATR": "atr", "Volatility (VOL)": "vol"}
_PRICE_KINDS = ("SMA", "EMA", "Bollinger bands")


def _parse_periods(text: str) -> list[int]:
    """Comma-separated periods such as "50, 200". Raises ValueError when invalid."""
    periods = [int(part) for part in text.split(",") if part.strip()]
    if not periods or any(p < 2 for p in periods):
        raise ValueError(text)
    return periods


@st.cache_data(show_spinner=False, ttl=60)
def _extent_cached(source: DataSource, root: Path, symbol: str) -> tuple[date, date] | None:
    """First and last bar dates for ``symbol``, cached briefly per source, root and symbol."""
    if source is DataSource.SYNTHETIC:
        return data_extent(source, symbol)
    return data_extent(source, symbol, cache_dir=root)


def render() -> None:
    st.title("Market data")
    explainer(
        "A candlestick shows one day of trading: where the price opened, how high and low it "
        "went, and where it closed. The bars underneath are how many shares or coins traded."
    )
    source, cache_dir = source_sidebar()
    source_banner(source, cache_dir)
    root = resolve_root(source, cache_dir=cache_dir)

    st.subheader("Choose what to see")
    left, right = st.columns(2)
    with left:
        symbol = st.selectbox(
            "Symbol", universe(), key="md_symbol", help="Symbols the app knows about."
        )

    extent = _extent_cached(source, root, symbol)
    if extent is None:
        empty = load_bars_cached(source, root, symbol, FULL_START, FULL_END)
        st.info(empty.message or f"No bars for {symbol} in this source yet.")
        return

    first, last = extent
    with right:
        picked = st.date_input(
            "Date range",
            value=(max(first, last - _LOOKBACK), last),
            min_value=first,
            max_value=last,
            key=f"md_range_{source.value}_{symbol}",
            help="Defaults to the last two years of available data.",
        )
    if not isinstance(picked, (tuple, list)) or len(picked) != 2:
        st.info("Pick an end date to finish choosing the range.")
        return
    start, end = picked
    if start > end:
        st.warning("The start date is after the end date.")
        return

    st.subheader("Indicators")
    kinds = st.multiselect(
        "Price overlays",
        list(_PRICE_KINDS),
        default=["SMA"],
        key="md_overlays",
        help="Lines drawn on top of the candles.",
    )
    sma_text = st.text_input(
        "SMA periods (days, comma-separated)", value="50, 200", key="md_sma_periods"
    )
    ema_period = st.number_input(
        "EMA period (days)", min_value=2, max_value=500, value=20, key="md_ema_period"
    )
    bb_period = st.number_input(
        "Bollinger period (days)", min_value=2, max_value=500, value=20, key="md_bb_period"
    )
    osc_label = st.selectbox(
        "Oscillator panel",
        list(_OSCILLATORS),
        index=1,
        key="md_osc",
        help="Drawn in its own panel below the price chart.",
    )
    osc_period = st.number_input(
        "Oscillator period (days)", min_value=2, max_value=500, value=2, key="md_osc_period"
    )
    if "SMA" in kinds:
        term("sma")
    if "EMA" in kinds:
        term("ema")
    if "Bollinger bands" in kinds:
        term("bbands")
    osc_key = _OSCILLATORS[osc_label]
    if osc_key is not None:
        term(osc_key)

    result = load_bars_cached(source, root, symbol, start, end)
    bars = result.bars
    if bars.empty:
        st.info(result.message or f"No bars for {symbol} between {start} and {end}.")
        return
    report = result.report
    if report is not None and report.status != "ok":
        st.warning(f"Data quality check failed ({report.status}), so no charts are shown.")
        return
    if result.message:
        st.warning(result.message)

    price_picks: list[tuple[str, dict[str, float]]] = []
    try:
        if "SMA" in kinds:
            price_picks += [("sma", {"n": n}) for n in _parse_periods(sma_text)]
    except ValueError:
        st.warning("SMA periods must be whole numbers of 2 or more, for example 50, 200.")
        return
    if "EMA" in kinds:
        price_picks.append(("ema", {"n": ema_period}))
    if "Bollinger bands" in kinds:
        price_picks.append(("bbands", {"n": bb_period, "k": 2.0}))
    osc_picks: list[tuple[str, dict[str, float]]] = []
    if osc_key is not None:
        osc_picks.append((osc_key, {"n": osc_period}))

    # Recursive indicators (RSI, ATR, EMA) depend on all earlier bars, so compute them on the
    # whole history from the start of the data extent, then trim to the visible range below.
    if start > first:
        history = load_bars_cached(source, root, symbol, first, end)
        calc_bars = history.bars if not history.bars.empty else bars
    else:
        calc_bars = bars
    available = len(calc_bars)
    short = [(name, p) for name, p in price_picks if name in ("sma", "ema") and p["n"] > available]
    for name, params in short:
        st.info(
            f"{name.upper()} {int(params['n'])} needs {int(params['n'])} bars of history, but "
            f"only {available} are available for {symbol}, so it is not drawn."
        )
    price_picks = [pick for pick in price_picks if pick not in short]
    try:
        price_overlays = charts.compute_overlays(calc_bars, price_picks)
        osc_overlays = charts.compute_overlays(calc_bars, osc_picks)
    except ValueError as exc:
        st.warning(f"Could not compute the indicators: {exc}")
        return
    # Bollinger %B and bandwidth are not prices, so they stay off the candlestick chart.
    price_overlays = {
        label: series.reindex(bars.index)
        for label, series in price_overlays.items()
        if not label.endswith((" pctb", " bandwidth"))
    }
    osc_overlays = {label: series.reindex(bars.index) for label, series in osc_overlays.items()}

    st.plotly_chart(
        charts.candlestick_with_volume(bars, symbol, price_overlays),
        width="stretch",
    )
    st.caption(
        "Prices are adjusted for splits and dividends so long-run charts are comparable. "
        "Stocks and ETFs follow the NYSE calendar (weekdays, no market holidays). "
        "BTC, ETH and SOL trade every day, including weekends."
    )
    if osc_key is not None and osc_overlays:
        hlines = (30.0, 70.0) if osc_key == "rsi" else ()
        st.plotly_chart(
            charts.indicator_subplot(osc_overlays, f"{osc_label} {osc_period}", hlines),
            width="stretch",
        )

    st.subheader("Data quality")
    cols = st.columns(4)
    cols[0].metric("Rows", len(bars))
    cols[1].metric("First date", bars.index[0].date().isoformat())
    cols[2].metric("Last date", bars.index[-1].date().isoformat())
    cols[3].metric("Duplicates dropped", report.duplicates_dropped if report else 0)
    if report is not None and report.gaps:
        st.caption(f"Gaps in the data: {len(report.gaps)} day(s) with missing bars.")
    if report is not None and report.outliers:
        st.caption(f"Outlier price moves flagged: {len(report.outliers)}.")
    if report is not None and report.warnings:
        for warning in report.warnings:
            st.warning(warning)
    else:
        st.success("No problems found.")
