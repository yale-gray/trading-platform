"""Backtests page: replay a strategy over the synthetic fixtures and compare it with buy-and-hold.

Read-only. Pressing "Run backtest" calls the backtester on fixture bars; the result is kept in
the browser session so switching between choices does not rerun anything. No trading logic
lives here: the page only lays out what ``devconsole.lib.backtest_view`` and the engine return.
"""

from __future__ import annotations

import streamlit as st

from devconsole import ui
from devconsole.lib import backtest_view as bv
from devconsole.lib.sources import DataSource
from tradebot.backtest.engine import BacktestReport

_RESULTS_KEY = "backtest_results"
_RUN_KEY = "run_backtest"


def render() -> None:
    st.title("Backtests")
    ui.explainer(bv.EXPLAINER)
    ui.source_banner(DataSource.SYNTHETIC)

    options = {c.key: c.label for c in bv.choices()}
    key = st.selectbox(
        "What to test",
        list(options),
        format_func=options.__getitem__,
        key="backtest_choice",
        help="The full fund uses all three strategies and the cash buffer. A single sleeve "
        "holds 100% of the fund in that one strategy.",
    )

    results = st.session_state.setdefault(_RESULTS_KEY, {})
    if st.button("Run backtest", key=_RUN_KEY, type="primary"):
        try:
            with st.spinner("Replaying the rules day by day..."):
                results[key] = bv.run_report(key)
        except bv.BacktestError as exc:
            st.error(str(exc))
            return

    report = results.get(key)
    if report is None:
        st.info("Press **Run backtest** to replay this choice. It takes a few seconds.")
        return
    _show(report)


def _show(report: BacktestReport) -> None:
    st.subheader("Equity curve against the benchmarks")
    st.caption(
        "Starts at the fund's $100,000. The solid line is the fund; dotted lines are buy-and-hold "
        "comparisons over the same days, with the same simulated costs."
    )
    st.plotly_chart(bv.equity_figure(report), config={"displayModeBar": False})

    st.subheader("Drawdown")
    st.caption("How far each line sat below its own previous high. Closer to zero is calmer.")
    st.plotly_chart(bv.drawdown_figure(report), config={"displayModeBar": False})

    st.subheader("How the fund compares")
    st.dataframe(bv.metrics_table(report), hide_index=True, width="stretch")

    st.subheader("Finished trades")
    trades = bv.trades_table(report.result.trades)
    if trades.empty:
        st.caption("No round trips finished in this test (positions may still be open).")
    else:
        st.dataframe(trades, hide_index=True, width="stretch")
    open_count = len(report.result.open_positions)
    if open_count:
        st.caption(
            f"{open_count} position(s) were still open at the end. They are not counted above."
        )
