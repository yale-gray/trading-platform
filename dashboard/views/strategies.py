"""Strategies page: one performance card per strategy, with the benchmark beside it."""

from __future__ import annotations

import streamlit as st

from dashboard import client
from dashboard.views import common

_EXPLAINER = """
A **strategy** is a set of rules for when to buy and sell. Each card shows how a strategy
did over the last year, next to a **benchmark**: simply buying and holding the market
(SPY for stocks, BTC for crypto). A strategy only earns its keep if it beats the benchmark
after fees. Results come from either a **paper** run (the bot trading simulated money live)
or a **backtest** (the same rules run over past prices).
"""

_HELP_RETURN = "Growth over the last 12 months, after simulated fees and slippage."
_HELP_BENCH = "What simply buying and holding the benchmark returned over the same year."
_HELP_DD = "The biggest fall from a previous high. Smaller is calmer."
_HELP_SORTINO = (
    "Return per unit of downside risk. Only losses count as risk. Higher is better; "
    "above 1 is generally considered good."
)


def render() -> None:
    st.title("Strategies")
    common.banner()
    common.explain(_EXPLAINER)
    cards = common.fetch(client.strategy_cards)
    if cards is None:
        return
    if not cards:
        st.info("No strategies stored yet.")
        return
    cols = st.columns(3)
    for i, card in enumerate(cards):
        with cols[i % 3].container(border=True):
            _card(card)


def _card(card: dict) -> None:
    source = "Paper trading" if card["source"] == "paper" else "Backtest"
    st.subheader(card["name"])
    st.caption(
        f"Version {card['version']} · {card['market']} · {source}"
        + (f" · as of {common.fmt_ts(card['as_of'])}" if card["as_of"] else "")
    )
    if card["return_1y_pct"] is None:
        st.info("Not enough history yet. Run a backtest to fill this card.")
    c1, c2 = st.columns(2)
    c1.metric("1-year return", common.pct(card["return_1y_pct"]), help=_HELP_RETURN)
    c2.metric(
        f"Benchmark ({card['benchmark_symbol']})",
        common.pct(card["benchmark_return_1y_pct"]),
        help=_HELP_BENCH,
    )
    c3, c4 = st.columns(2)
    c3.metric("Worst drop", common.pct(card["max_drawdown_pct"]), help=_HELP_DD)
    c4.metric("Sortino ratio", common.num(card["sortino"]), help=_HELP_SORTINO)
