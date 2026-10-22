"""Portfolio detail page: charts, positions, trades, sleeves and the kill-switch panel."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import client
from dashboard.views import common

TRADES_PAGE_SIZE = 50

_EXPLAINER = """
The **chart** compares this portfolio with simply buying and holding a benchmark (SPY is the
US stock market, BTC is bitcoin). Both lines start at 100, so you can see who is ahead.
The **drop chart** shows how far the value sits below its previous high at each point.
A **sleeve** is one strategy's share of the portfolio. **Target** is the planned share,
**actual** is the share the sleeve holds today.
"""

_KILL_SWITCH_HELP = (
    "Acknowledging records that a person reviewed the trip. **It does not reset the switch.** "
    "Reset is done only from the command line: `tradebot portfolio reset-kill-switch`."
)


def render() -> None:
    st.title("Portfolio detail")
    common.banner()
    portfolios = common.fetch(client.list_portfolios)
    if portfolios is None:
        return
    if not portfolios:
        st.info(
            "No portfolios yet. Create one from the command line with `tradebot portfolio create`."
        )
        return
    choice = st.sidebar.selectbox(
        "Portfolio",
        portfolios,
        format_func=lambda p: f"{p['name']} ({p['kind']})",
        key="portfolio_choice",
    )
    pid = int(choice["id"])
    detail = common.fetch(client.get_portfolio, pid)
    if detail is None:
        return
    common.explain(_EXPLAINER)
    _summary(detail)
    _equity_charts(pid, detail)
    _positions(pid)
    _trades(pid)
    _sleeves(detail)
    _kill_switch(pid, detail)


def _summary(detail: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", common.money(detail["equity"]), help="What the portfolio is worth now.")
    c2.metric("Started with", common.money(detail["initial_cash"]))
    c3.metric(
        "Total return",
        common.pct(detail["total_return_pct"]),
        help="Growth since the start, after all simulated fees and slippage.",
    )
    c4.metric(
        "Worst drop",
        common.pct(detail["max_drawdown_pct"]),
        help="The biggest fall from a previous high. Smaller is calmer.",
    )


def _equity_charts(pid: int, detail: dict) -> None:
    points = common.fetch(client.get_equity, pid)
    if points is None:
        return
    if not points:
        st.info("No equity history yet. The runner adds a point each time it processes a bar.")
        return
    df = pd.DataFrame(points)
    ts = df["ts"]
    fund = common.rebased(list(df["equity"]))
    bench_names = ", ".join(b["name"] for b in detail.get("benchmarks", [])) or "benchmark"
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ts, y=fund, name="This portfolio", mode="lines"))
    if df["benchmark_equity"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=ts,
                y=common.rebased(list(df["benchmark_equity"])),
                name=f"Buy and hold ({bench_names})",
                mode="lines",
            )
        )
    fig.update_layout(
        title="Growth compared with buying and holding (starts at 100)",
        yaxis_title="Value, starts at 100",
        height=360,
        margin=dict(l=10, r=10, t=50, b=10),
    )
    st.plotly_chart(fig)

    dd = -df["drawdown_pct"].astype(float)
    dd_fig = go.Figure(go.Scatter(x=ts, y=dd, fill="tozeroy", name="Drop", mode="lines"))
    dd_fig.update_layout(
        title="Drop from the previous high (%)",
        yaxis_title="% below the high",
        height=260,
        margin=dict(l=10, r=10, t=50, b=10),
    )
    st.plotly_chart(dd_fig)


def _positions(pid: int) -> None:
    st.subheader("Open positions")
    st.caption("A **position** is a holding the portfolio owns right now.")
    rows = common.fetch(client.get_positions, pid)
    if rows is None:
        return
    if not rows:
        st.info("No open positions.")
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
                for r in rows
            ]
        ),
        hide_index=True,
    )


def _trades(pid: int) -> None:
    st.subheader("Trades")
    st.caption("A **trade** is one completed buy or sell, newest first.")
    page = int(st.number_input("Page", min_value=1, value=1, step=1, key=f"trade_page_{pid}"))
    data = common.fetch(
        client.get_trades, pid, limit=TRADES_PAGE_SIZE, offset=(page - 1) * TRADES_PAGE_SIZE
    )
    if data is None:
        return
    st.caption(f"{data['total']} trades in total. Page {page}.")
    if not data["items"]:
        st.info("No trades on this page.")
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
                    "Slippage cost": common.money(t["slippage"]),
                    "Reason": t["reason"],
                }
                for t in data["items"]
            ]
        ),
        hide_index=True,
    )


def _sleeves(detail: dict) -> None:
    st.subheader("Sleeves")
    sleeves = detail.get("sleeves", [])
    if not sleeves:
        st.info("No sleeves configured.")
        return
    fig = go.Figure()
    for s in sleeves:
        fig.add_trace(
            go.Bar(
                name=s["strategy_name"] or s["key"],
                x=["Target", "Actual"],
                y=[s["target_weight_pct"], s["actual_weight_pct"]],
            )
        )
    fig.update_layout(
        barmode="stack",
        yaxis_title="% of portfolio",
        height=300,
        margin=dict(l=10, r=10, t=10, b=10),
    )
    st.plotly_chart(fig)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Sleeve": s["key"],
                    "Strategy": s["strategy_name"] or "—",
                    "Target share": common.pct(s["target_weight_pct"]),
                    "Actual share": common.pct(s["actual_weight_pct"]),
                    "Equity": common.money(s["equity"]),
                    "Return": common.pct(s["return_pct"]),
                    "Open positions": s["n_positions"],
                }
                for s in sleeves
            ]
        ),
        hide_index=True,
    )


def _kill_switch(pid: int, detail: dict) -> None:
    ks = detail["kill_switch"]
    st.subheader("Kill switch")
    if ks["tripped"]:
        st.error(
            f"The kill switch has TRIPPED (tripped at {common.fmt_ts(ks['tripped_at_bar'])}). "
            "New trades are stopped until it is reset from the command line."
        )
    else:
        st.success("Not tripped. Trading is allowed.")
    st.caption(
        f"Limit: losses beyond {common.pct(ks['threshold_pct'])} from the high stop trading."
    )
    if ks["acknowledged_at"]:
        st.caption(
            f"Last reviewed {common.fmt_ts(ks['acknowledged_at'])}: {ks['note'] or 'no note'}"
        )
    st.caption(_KILL_SWITCH_HELP)
    with st.form(f"ack_form_{pid}"):
        note = st.text_area("Review note", key=f"ack_note_{pid}", max_chars=1000)
        submitted = st.form_submit_button("Acknowledge", disabled=not ks["tripped"])
    if submitted:
        if not note.strip():
            st.warning("Write a short review note first.")
            return
        result = common.fetch(client.ack_kill_switch, pid, note.strip())
        if result is not None:
            st.success(
                "Acknowledged. The switch is still tripped. "
                "Reset it from the command line: `tradebot portfolio reset-kill-switch`."
            )
