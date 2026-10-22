"""Portfolios page: every portfolio with its equity, return, worst drop and kill-switch state."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import client
from dashboard.views import common

_EXPLAINER = """
A **portfolio** is a pot of simulated money that the bot manages. **Equity** is what the pot
is worth right now. **Total return** is how much it has grown since it started.
**Worst drop** (maximum drawdown) is the biggest fall from a previous high. It tells you
how much pain the pot has taken. A **kill switch** stops new trades when losses pass a set limit.
Open a portfolio on the *Portfolio detail* page to see the full picture.
"""


def render() -> None:
    st.title("Portfolios")
    common.banner()
    common.explain(_EXPLAINER)
    rows = common.fetch(client.list_portfolios)
    if rows is None:
        return
    common.runner_badge()
    if not rows:
        st.info(
            "No portfolios yet. Create one from the command line with `tradebot portfolio create`."
        )
        return
    tripped = [r["name"] for r in rows if r["kill_switch_tripped"]]
    if tripped:
        st.warning(
            f"Kill switch tripped on: {', '.join(tripped)}. Open the portfolio to review it."
        )
    table = pd.DataFrame(
        [
            {
                "Name": r["name"],
                "Kind": r["kind"],
                "Status": r["status"],
                "Equity": common.money(r["equity"]),
                "Started with": common.money(r["initial_cash"]),
                "Total return": common.pct(r["total_return_pct"]),
                "Worst drop": common.pct(r["max_drawdown_pct"]),
                "Kill switch": "TRIPPED" if r["kill_switch_tripped"] else "ok",
                "Last bar processed": common.fmt_ts(r["last_processed_bar"]),
            }
            for r in rows
        ]
    )
    st.dataframe(table, hide_index=True)
