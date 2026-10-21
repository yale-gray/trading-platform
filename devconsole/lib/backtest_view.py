"""Pure helpers for the Backtests page: build a config, run the backtester, shape the tables.

No streamlit import. Everything runs through ``tradebot.backtest.engine.run_with_benchmark``
on the committed synthetic fixtures. Nothing here places an order, changes a file or touches
the network. Money is shown as text from ``Decimal``, never converted to float for display.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pandas as pd
import plotly.graph_objects as go

from devconsole.lib.sources import DataSource, load_bars
from tradebot.backtest.engine import (
    BacktestConfig,
    BacktestReport,
    SleeveSpec,
    run_with_benchmark,
)
from tradebot.backtest.trades import ClosedTrade
from tradebot.portfolio.config import default_config
from tradebot.strategy.schema import STRATEGIES

__all__ = [
    "BacktestError",
    "BENCHMARK_LABELS",
    "EXPLAINER",
    "FULL_FUND",
    "METRIC_HELP",
    "build_config",
    "choices",
    "drawdown_frame",
    "equity_frame",
    "equity_figure",
    "drawdown_figure",
    "metrics_table",
    "run_report",
    "trades_table",
]

FULL_FUND = "full"
# Fixture bars start 2018-01-02. The run starts 2019-01-02 so the 200-day trend filter has
# a year of warmup history inside the data (the engine refuses a run without it).
FETCH_START = date(2018, 1, 2)
RUN_START = date(2019, 1, 2)
RUN_END = date(2024, 12, 31)
BENCHMARK_SYMBOLS = ("SPY", "BTC/USD")

BENCHMARK_LABELS = {
    "SPY": "SPY (US stocks, buy and hold)",
    "BTC/USD": "BTC/USD (bitcoin, buy and hold)",
    "blend": "Blend (fixed mix of SPY and BTC)",
}
FUND_LABEL = "Your fund"

EXPLAINER = (
    "A **backtest** replays the rules of a strategy over past prices, one day at a time, as if "
    "the fake money had been invested then. It charges the same simulated fees and slippage as "
    "the paper fund, and it only acts on the next day's open price, never on a price it could "
    "not yet have seen. Each result is compared with simply buying and holding SPY (US stocks) "
    "or BTC/USD (bitcoin), and with a fixed blend of the two. A good-looking backtest is not a "
    "promise of future results. This page is for learning how the numbers are built."
)

_EXIT_WORDS = {
    "signal": "The strategy's rule said sell",
    "stop": "Stop-loss triggered",
    "rebalance": "Monthly rebalance",
    "manual": "Sold by hand",
}

METRIC_HELP: dict[str, str] = {
    "Total return": "How much the money grew (or shrank) over the whole test, as a percentage.",
    "Yearly growth rate": "The steady yearly rate that would produce the same total growth.",
    "Worst drop from a peak": "The biggest fall from a previous high point, as a percentage. "
    "Smaller (closer to zero) is calmer.",
    "Sharpe ratio": "Return earned per unit of day-to-day bumpiness. Higher is better; "
    "above 1 is considered strong.",
    "Sortino ratio": "Like the Sharpe ratio, but only counts the bad days as risk. "
    "Higher is better.",
    "Share of trades that made money": "Of the finished round trips (buy then sell), the share "
    "that finished with a profit after fees.",
    "Finished trades": "Round trips that were bought and then fully sold during the test.",
    "Average money invested": "On average, how much of the fund's value sat in positions "
    "rather than cash.",
    "Share of days invested": "The fraction of days the fund held at least one position.",
}
_METRIC_ROWS = (
    ("Total return", "total_return", "pct_signed"),
    ("Yearly growth rate", "cagr", "pct_signed"),
    ("Worst drop from a peak", "max_drawdown", "pct_signed"),
    ("Sharpe ratio", "sharpe", "ratio"),
    ("Sortino ratio", "sortino", "ratio"),
    ("Share of trades that made money", "win_rate", "pct"),
    ("Finished trades", "trade_count", "int"),
    ("Average money invested", "exposure_avg", "pct"),
    ("Share of days invested", "time_in_market", "pct"),
)


class BacktestError(Exception):
    """A backtest could not be loaded or run. The message is safe to show to the owner."""


@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    sleeve_id: str | None  # None means the whole fund


def choices() -> list[Choice]:
    """The full fund first, then each strategy sleeve on its own."""
    out = [
        Choice(FULL_FUND, "Full fund (50% core, 20% mean reversion, 20% crypto, 10% cash)", None)
    ]
    for sid, spec in STRATEGIES.items():
        out.append(Choice(sid, f"Single sleeve: {spec.name}", sid))
    return out


def _choice(key: str) -> Choice:
    for c in choices():
        if c.key == key:
            return c
    raise BacktestError(f"Unknown backtest choice {key!r}.")


def build_config(key: str) -> BacktestConfig:
    """Config for the full fund or for one strategy sleeve holding 100% of the fund."""
    base = default_config()
    choice = _choice(key)
    if choice.sleeve_id is None:
        sleeves = [SleeveSpec(sleeve_id=s, strategy=STRATEGIES[s]) for s in STRATEGIES]
        sleeves.append(SleeveSpec(sleeve_id="cash", strategy=None))
        return BacktestConfig(sleeves=sleeves, start=RUN_START, end=RUN_END, engine=base)
    sid = choice.sleeve_id
    sc = next(s for s in base.sleeves if s.sleeve_id == sid)
    engine = base.model_copy(
        update={"sleeves": (sc.model_copy(update={"target_weight": Decimal(1)}),)}
    )
    return BacktestConfig(
        sleeves=[SleeveSpec(sleeve_id=sid, strategy=STRATEGIES[sid])],
        start=RUN_START,
        end=RUN_END,
        engine=engine,
    )


def _needed_symbols(key: str) -> set[str]:
    symbols = set(BENCHMARK_SYMBOLS)
    for sid, spec in STRATEGIES.items():
        if key == FULL_FUND or key == sid:
            symbols.update(str(s) for s in spec.required_symbols())
    return symbols


def load_fixture_bars(key: str) -> dict[str, pd.DataFrame]:
    """Synthetic bars for every symbol the chosen run needs, read-only."""
    bars: dict[str, pd.DataFrame] = {}
    for symbol in sorted(_needed_symbols(key)):
        result = load_bars(DataSource.SYNTHETIC, symbol, FETCH_START, RUN_END)
        if result.message is not None:
            raise BacktestError(result.message)
        bars[symbol] = result.bars
    return bars


def run_report(key: str) -> BacktestReport:
    """Load the synthetic bars and run the fund plus its buy-and-hold benchmarks."""
    cfg = build_config(key)
    bars = load_fixture_bars(key)
    try:
        return run_with_benchmark(cfg, bars)
    except ValueError as exc:
        raise BacktestError(f"The backtest could not run: {exc}") from exc


# ---------------------------------------------------------------- series


def _series_map(report: BacktestReport) -> dict[str, pd.Series]:
    out = {FUND_LABEL: report.result.equity}
    for name, eq in report.benchmark_equity.items():
        out[BENCHMARK_LABELS.get(name, name)] = eq
    return out


def equity_frame(report: BacktestReport) -> pd.DataFrame:
    """Dollar value per day: the fund and each benchmark, on the fund's dates."""
    return pd.DataFrame(_series_map(report))


def drawdown_frame(report: BacktestReport) -> pd.DataFrame:
    """Percent below the running peak per day (0 at a new high, negative in a drop)."""
    out = {}
    for label, eq in _series_map(report).items():
        peak = eq.cummax()
        out[label] = (eq / peak - 1.0) * 100.0
    return pd.DataFrame(out)


_LINE_COLORS = ("#1f77b4", "#ff7f0e", "#9467bd", "#8c564b")
_LAYOUT = {
    "paper_bgcolor": "rgba(0, 0, 0, 0)",
    "plot_bgcolor": "rgba(0, 0, 0, 0)",
    "margin": {"l": 60, "r": 20, "t": 40, "b": 40},
    "legend": {"orientation": "h", "y": -0.2},
    "hovermode": "x unified",
}


def _figure(frame: pd.DataFrame, title: str, y_title: str, y_format: str) -> go.Figure:
    fig = go.Figure()
    for i, label in enumerate(frame.columns):
        fig.add_trace(
            go.Scatter(
                x=frame.index,
                y=frame[label],
                mode="lines",
                name=label,
                line={
                    "width": 3 if i == 0 else 2,
                    "color": _LINE_COLORS[i % len(_LINE_COLORS)],
                    "dash": "solid" if i == 0 else "dot",
                },
            )
        )
    fig.update_layout(title=title, yaxis_title=y_title, yaxis_tickformat=y_format, **_LAYOUT)
    fig.update_xaxes(showgrid=True, gridcolor="rgba(128, 128, 128, 0.25)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(128, 128, 128, 0.25)")
    return fig


def equity_figure(report: BacktestReport) -> go.Figure:
    return _figure(equity_frame(report), "Value of the money over time", "Dollars", ",.0f")


def drawdown_figure(report: BacktestReport) -> go.Figure:
    fig = _figure(drawdown_frame(report), "Drops from a previous high", "Percent below peak", ".0f")
    fig.update_yaxes(ticksuffix="%")
    return fig


# ---------------------------------------------------------------- tables


def _fmt(value: float, kind: str) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    if kind == "pct_signed":
        return f"{value * 100:+.1f}%"
    if kind == "pct":
        return f"{value * 100:.0f}%"
    if kind == "ratio":
        return f"{value:.2f}"
    return f"{int(value)}"


def metrics_table(report: BacktestReport) -> pd.DataFrame:
    """Fund against each benchmark. Rows are plain-English metrics; a help column explains each."""
    columns: dict[str, dict[str, str]] = {FUND_LABEL: {}}
    for label in BENCHMARK_LABELS.values():
        columns[label] = {}
    for name, m in report.benchmark_metrics.items():
        columns[BENCHMARK_LABELS.get(name, name)] = {
            metric: _fmt(getattr(m, attr), kind) for metric, attr, kind in _METRIC_ROWS
        }
    fund = report.metrics
    columns[FUND_LABEL] = {
        metric: _fmt(getattr(fund, attr), kind) for metric, attr, kind in _METRIC_ROWS
    }
    rows = []
    for metric, _attr, _kind in _METRIC_ROWS:
        row: dict[str, str] = {"Measure": metric}
        for label, values in columns.items():
            row[label] = values.get(metric, "not compared here")
        row["What it means"] = METRIC_HELP[metric]
        rows.append(row)
    return pd.DataFrame(rows)


def _shares(qty: Decimal) -> str:
    return format(qty.normalize(), "f")


def _money(amount: Decimal) -> str:
    return format(amount, ",.2f")


def _sleeve_name(sleeve_id: str) -> str:
    spec = STRATEGIES.get(sleeve_id)
    return spec.name if spec is not None else sleeve_id


def trades_table(trades: list[ClosedTrade]) -> pd.DataFrame:
    """One row per finished round trip, with money as text formatted from Decimal values."""
    rows = []
    for t in trades:
        rows.append(
            {
                "Strategy": _sleeve_name(t.sleeve_id),
                "Symbol": t.symbol,
                "Bought on": t.entry_asof.isoformat(),
                "Sold on": t.exit_asof.isoformat(),
                "Shares": _shares(t.qty),
                "Average buy price": _money(t.entry_price),
                "Average sell price": _money(t.exit_price),
                "Fees": _money(t.fees),
                "Profit or loss after fees": _money(t.net_pnl),
                "Why it was sold": _EXIT_WORDS.get(t.exit_reason, t.exit_reason),
            }
        )
    return pd.DataFrame(rows)
