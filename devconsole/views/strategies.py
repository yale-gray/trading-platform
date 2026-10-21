"""Strategies page: each built-in strategy's rules in plain English, plus its raw YAML.

This page describes rules and draws the indicators they use. It evaluates nothing
and places no orders. Buy and sell signals arrive with the engine in Wave 3.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from devconsole import ui
from devconsole.lib import charts
from devconsole.lib.rules_english import describe_strategy, indicators_used
from devconsole.lib.sources import FULL_END, FULL_START, DataSource, resolve_root
from tradebot.strategy.io import dump_strategy
from tradebot.strategy.schema import STRATEGIES, StrategySpec

_EXPLAINER = (
    "A strategy is a written set of rules for when to buy and sell. These are the three "
    "strategies in the plan; each one manages its own slice of the fake money. Nothing here "
    "is buying or selling: signals appear after Wave 3."
)
_SIGNALS_INFO = (
    "Buy/sell signals appear here after Wave 3 (the engine). This page only describes the rules."
)
_WINDOW_YEARS = 2
_ASSET_WORDS = {"equity": "stocks and ETFs", "crypto": "crypto coins"}


def render() -> None:
    st.title("Strategies")
    ui.explainer(_EXPLAINER)
    st.info(_SIGNALS_INFO)
    source, cache_dir = ui.source_sidebar()
    ui.source_banner(source, cache_dir)

    strategy_id = st.selectbox(
        "Strategy",
        list(STRATEGIES),
        format_func=lambda sid: STRATEGIES[sid].name,
        key="strategy_id",
        help="The three starting strategies from the strategy document.",
    )
    spec = STRATEGIES[strategy_id]
    _describe(spec)
    _chart_section(spec, source, cache_dir)


def _bullets(heading: str, items: list[str]) -> None:
    st.markdown(f"**{heading}**")
    if not items:
        st.caption("None listed.")
        return
    lines = []
    for item in items:
        # Nested sub-bullets arrive already prefixed with "- "; indent them under their parent.
        lines.append(f"    {item}" if item.startswith("- ") else f"- {item}")
    st.markdown("\n".join(lines))


def _describe(spec: StrategySpec) -> None:
    text = describe_strategy(spec)
    st.header(spec.name)
    if spec.description:
        st.write(spec.description)
    st.caption(f"Applies to {_ASSET_WORDS[spec.asset_class]}.")

    summary_tab, rules_tab, params_tab, universe_tab, yaml_tab = st.tabs(
        ["Summary", "Entry, exit and stops", "Parameters", "Universe", "YAML"]
    )
    with summary_tab:
        st.write(text.summary)
        st.markdown(f"**Warmup:** {text.warmup}.")
    with rules_tab:
        _bullets("Entry", text.entry)
        _bullets("Exit", text.exit)
        _bullets("Stops", text.stops)
    with params_tab:
        if text.parameters:
            table = pd.DataFrame(
                {
                    "Parameter": list(text.parameters),
                    "Value and meaning": list(text.parameters.values()),
                }
            )
            st.dataframe(table, hide_index=True)
        else:
            st.caption("This strategy has no adjustable parameters.")
    with universe_tab:
        st.markdown(f"**Universe ({len(spec.universe)} symbols):** {', '.join(spec.universe)}")
        st.caption(f"Symbols the rules need data for: {', '.join(spec.required_symbols())}.")
    with yaml_tab:
        st.code(dump_strategy(spec), language="yaml")


def _windowed(series_map: dict[str, pd.Series], start: pd.Timestamp) -> dict[str, pd.Series]:
    return {label: s.loc[s.index >= start] for label, s in series_map.items()}


def _chart_section(spec: StrategySpec, source: DataSource, cache_dir: Path | None) -> None:
    st.subheader("Chart: the indicators this strategy uses")
    st.caption(
        "Averages and bands are drawn on the candles. Momentum and range measures get their "
        "own panel below."
    )
    symbol = st.selectbox(
        "Symbol",
        spec.universe,
        key=f"chart_symbol_{spec.id}",
        help="Only symbols from this strategy's universe.",
    )
    picks = indicators_used(spec)
    for name in dict.fromkeys(name for name, _ in picks):
        ui.term(name)

    root = resolve_root(source, cache_dir=cache_dir)
    result = ui.load_bars_cached(source, root, symbol, FULL_START, FULL_END)
    if result.message:
        st.warning(result.message)
        return
    bars = result.bars

    # Indicators are computed on the full history so long averages have their warmup,
    # then the view shows the last couple of years of the loaded data.
    start = bars.index[-1] - pd.DateOffset(years=_WINDOW_YEARS)
    view = bars.loc[bars.index >= start]
    st.caption(
        f"Showing {view.index[0]:%Y-%m-%d} to {view.index[-1]:%Y-%m-%d} "
        "(last two years of the loaded data)."
    )

    price_picks = [p for p in picks if p[0] in charts.PRICE_INDICATORS]
    oscillator_picks = [p for p in picks if p[0] not in charts.PRICE_INDICATORS]
    try:
        price_series = charts.compute_overlays(bars, price_picks)
        oscillator_groups = [charts.compute_overlays(bars, [p]) for p in oscillator_picks]
    except ValueError as exc:
        st.error(str(exc))
        return

    st.plotly_chart(
        charts.candlestick_with_volume(view, symbol, _windowed(price_series, start)),
        config={"displayModeBar": False},
    )
    for group in oscillator_groups:
        series = _windowed(group, start)
        st.plotly_chart(
            charts.indicator_subplot(series, ", ".join(series)),
            config={"displayModeBar": False},
        )
