"""One portfolio section (Index funds, Stocks or Crypto): explainer, sleeves only, positions,
value-over-time charts and recent trades. Shared by the three section pages.

Reads only through ``dashboard/client.py``. Trades are fetched once and filtered client-side.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import client
from dashboard.views import common

TRADES_PAGE_SIZE = 50
NO_TRADES = "No trades yet — the first orders fill at the next market open."


def render_section(title: str, explainer: str, note: str | None = None) -> None:
    st.title(title)
    common.banner()
    common.explain(explainer)
    if note:
        st.caption(note)
    portfolios = common.fetch(client.list_portfolios)
    if portfolios is None:
        return
    if not portfolios:
        st.info(
            "No portfolios yet. Create one from the command line with `tradebot portfolio create`."
        )
        return
    choice = st.selectbox(
        "Portfolio",
        portfolios,
        index=common.default_portfolio_index(portfolios),
        format_func=lambda p: f"{p['name']} ({p['kind']})",
        key=f"section_portfolio_{title}",
    )
    pid = int(choice["id"])
    detail = common.fetch(client.get_portfolio, pid)
    if detail is None:
        return
    sleeves = [s for s in detail.get("sleeves", []) if common.section_of(s["key"]) == title]
    if not sleeves:
        st.info(f"This portfolio has no {title.lower()} sleeve. Pick another portfolio above.")
        return
    keys = {s["key"] for s in sleeves}
    _positions(pid, keys)
    _value_charts(pid, title, sleeves)
    _trades(pid, keys)


def _positions(pid: int, keys: set[str]) -> None:
    st.subheader("Current positions")
    rows = common.fetch(client.get_positions, pid)
    if rows is None:
        return
    mine = [r for r in rows if r["sleeve_key"] in keys]
    if not mine:
        st.info("No open positions in this section yet.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Sleeve": r["sleeve_key"],
                    "Symbol": r["symbol"],
                    "Quantity": r["qty"],
                    "Average cost": common.money(r["avg_cost"]),
                    "Price now": common.money(r["mark_price"]),
                    "Market value": common.money(r["market_value"]),
                    "Unrealized profit or loss": common.money(r["unrealized_pnl"]),
                    "Share of portfolio": common.pct(r["weight_pct"]),
                }
                for r in mine
            ]
        ),
        hide_index=True,
    )


def _value_charts(pid: int, title: str, sleeves: list[dict]) -> None:
    st.subheader("Value over time")
    for sl in sleeves:
        name = sl["strategy_name"] or sl["key"]
        points = common.fetch(client.get_equity, pid, sleeve=sl["key"])
        if points is None:
            continue
        if not points:
            st.caption(f"{name}: no value history yet. The runner adds a point each bar.")
            continue
        df = pd.DataFrame(points)
        fig = go.Figure(
            go.Scatter(x=df["ts"], y=pd.to_numeric(df["equity"]), mode="lines", name=name)
        )
        fig.update_layout(
            title=f"{name}: value in dollars",
            yaxis_title="Value ($)",
            height=280,
            margin=dict(l=10, r=10, t=50, b=10),
        )
        st.plotly_chart(fig, key=f"section_chart_{title}_{pid}_{sl['key']}")


def _trades(pid: int, keys: set[str]) -> None:
    st.subheader("Recent trades")
    page = common.fetch(client.get_trades, pid, limit=TRADES_PAGE_SIZE)
    if page is None:
        return
    if not page["items"]:
        st.info(NO_TRADES)
        return
    mine = [t for t in page["items"] if t["sleeve_key"] in keys]
    st.caption(f"Filtered from the latest {TRADES_PAGE_SIZE} trades in this portfolio.")
    if not mine:
        st.info("No trades in this section among the latest trades.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Time": common.fmt_ts(t["ts"]),
                    "Sleeve": t["sleeve_key"],
                    "Symbol": t["symbol"],
                    "Side": t["side"],
                    "Quantity": t["qty"],
                    "Price": common.money(t["price"]),
                    "Fee": common.money(t["fee"]),
                    "Reason": t["reason"],
                }
                for t in mine
            ]
        ),
        hide_index=True,
    )
