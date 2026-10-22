"""Backtests page: list of past runs, a form to queue a new run, and one run's details."""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import client
from dashboard.views import common

MODES = {
    "single": "Single run (one test over the whole period)",
    "walk_forward": "Walk-forward (test in rolling time slices)",
    "sensitivity": "Sensitivity (nudge settings by ±20% and see if results hold)",
}

_EXPLAINER = """
A **backtest** runs a strategy's rules over past prices to see how it would have done.
It is a history test, not a promise. Fees and slippage (the small price gap you pay when
trading) are included. Every backtest is compared with buying and holding the benchmark.
Queue a run below, then pick it in the list to read the details.
"""


def render() -> None:
    st.title("Backtests")
    common.banner()
    common.explain(_EXPLAINER)
    _new_run_form()
    runs = common.fetch(client.list_backtests)
    if runs is None:
        return
    st.subheader("Past runs")
    if not runs:
        st.info("No backtests yet. Queue one above.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Run": r["id"],
                    "Strategy": r["strategy_id"],
                    "Status": r["status"],
                    "Finished": common.fmt_ts(r["finished_at"]),
                    "Total return": common.pct(r["total_return_pct"]),
                    "Sharpe": common.num(r["sharpe"]),
                    "Sortino": common.num(r["sortino"]),
                    "Worst drop": common.pct(r["max_drawdown_pct"]),
                    "Trades": r["n_trades"] if r["n_trades"] is not None else "—",
                    "Benchmark": r["benchmark_symbol"],
                }
                for r in runs
            ]
        ),
        hide_index=True,
    )
    run_id = st.selectbox(
        "Run details",
        [r["id"] for r in runs],
        format_func=lambda rid: f"Run {rid}",
        key="backtest_run",
    )
    detail = common.fetch(client.get_backtest, int(run_id))
    if detail is not None:
        _detail(detail)


def _new_run_form() -> None:
    strategies = common.fetch(client.list_strategies)
    if strategies is None:
        return
    if not strategies:
        st.info("No strategies stored yet, so there is nothing to test.")
        return
    with st.expander("Queue a new backtest"), st.form("new_backtest"):
        strategy = st.selectbox(
            "Strategy",
            strategies,
            format_func=lambda s: f"{s['name']} (v{s['version']}, {s['market']})",
            key="new_bt_strategy",
        )
        start = st.date_input("Start", value=date(2024, 1, 1), key="new_bt_start")
        end = st.date_input("End (leave empty for today)", value=None, key="new_bt_end")
        mode = st.selectbox(
            "Test type", list(MODES), format_func=MODES.__getitem__, key="new_bt_mode"
        )
        submitted = st.form_submit_button("Queue backtest")
    if submitted:
        if end is not None and end <= start:
            st.warning("The end date must be after the start date.")
            return
        created = common.fetch(
            client.create_backtest,
            int(strategy["id"]),
            start,
            end if isinstance(end, date) else None,
            mode,
        )
        if created is not None:
            st.success(f"Queued run {created['id']}. It appears in the list when it finishes.")


def _detail(detail: dict) -> None:
    if detail["error"]:
        st.error(f"This run failed: {detail['error']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total return", common.pct(detail["total_return_pct"]))
    c2.metric("Sharpe", common.num(detail["sharpe"]), help="Return per unit of total volatility.")
    c3.metric("Sortino", common.num(detail["sortino"]), help=_SORTINO)
    c4.metric("Worst drop", common.pct(detail["max_drawdown_pct"]))

    result = detail.get("result") or {}
    equity = detail.get("equity", [])
    if equity:
        df = pd.DataFrame(equity)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["ts"], y=common.rebased(list(df["equity"])), name="Strategy"))
        if df["benchmark_equity"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=df["ts"],
                    y=common.rebased(list(df["benchmark_equity"])),
                    name=f"Buy and hold ({detail['benchmark_symbol']})",
                )
            )
        fig.update_layout(
            title="Strategy against buying and holding (starts at 100)",
            height=340,
            margin=dict(l=10, r=10, t=50, b=10),
        )
        st.plotly_chart(fig)

    deltas = result.get("deltas")
    if deltas:
        st.subheader("Strategy minus benchmark")
        st.caption("Positive numbers mean the strategy did better than buying and holding.")
        st.dataframe(pd.DataFrame(deltas).T, width="stretch")

    if "walk_forward" in result:
        st.subheader("Walk-forward results")
        st.json(result["walk_forward"])
    else:
        st.caption(
            "This stored result has no walk-forward table. In-sample and out-of-sample "
            "splits are not part of the saved output."
        )

    trades = detail.get("trades", [])
    st.subheader("Closed trades")
    if not trades:
        st.info("This run made no closed trades.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Time": common.fmt_ts(t["ts"]),
                        "Symbol": t["symbol"],
                        "Side": t["side"],
                        "Quantity": t["qty"],
                        "Price": common.money(t["price"]),
                        "Fee": common.money(t["fee"]),
                    }
                    for t in trades
                ]
            ),
            hide_index=True,
        )


_SORTINO = (
    "Return per unit of downside risk. Only losses count as risk. Higher is better; "
    "above 1 is generally considered good."
)
