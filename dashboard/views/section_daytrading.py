"""Day trading section page: a placeholder until the market-data key is set up."""

from __future__ import annotations

import streamlit as st

from dashboard.views import common

EXPLAINER = """
**Day trading is coming soon.** The idea: a separate simulated account of $10,000 trades
S&P 500 stocks on 15-minute price bars, which means several decisions per trading day
instead of one a month.

It is waiting on a market-data key from Alpaca. Until that key is set up, the bot has no
intraday prices to trade on, so this page has nothing to show yet.
"""


def render() -> None:
    st.title("Day trading")
    common.banner()
    common.explain(EXPLAINER)
    st.info("Coming once the Alpaca market-data key is set up.")
