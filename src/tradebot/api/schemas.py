"""Pydantic response/request models for the API (SPEC-004). Money is a decimal string."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, PlainSerializer


def _plain(value: Decimal) -> str:
    return format(value, "f")


Money = Annotated[Decimal, PlainSerializer(_plain, return_type=str, when_used="json")]
"""A Decimal serialised as a plain decimal string (``"100000.00"``), never a float."""


class Health(BaseModel):
    status: str
    db_ok: bool
    version: str


class PortfolioSummary(BaseModel):
    id: int
    name: str
    kind: Literal["fund", "benchmark"]
    status: str
    equity: Money
    initial_cash: Money
    total_return_pct: float
    max_drawdown_pct: float
    kill_switch_tripped: bool
    last_processed_bar: datetime | None
    started_at: datetime


class SleeveBreakdown(BaseModel):
    key: str
    strategy_name: str | None
    target_weight_pct: float
    actual_weight_pct: float
    equity: Money
    return_pct: float
    n_positions: int


class PositionOut(BaseModel):
    sleeve_key: str
    symbol: str
    qty: Money
    avg_cost: Money
    mark_price: Money
    market_value: Money
    unrealized_pnl: Money
    weight_pct: float


class TradeOut(BaseModel):
    ts: datetime
    sleeve_key: str
    symbol: str
    side: str
    qty: Money
    price: Money
    fee: Money
    slippage: Money | None  # None: not recorded (backtest round trips)
    reason: str


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class RoundTripOut(BaseModel):
    """One completed buy-then-sell of a symbol in a sleeve (from the ledger)."""

    sleeve_key: str
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    qty: Money
    entry_price: Money
    exit_price: Money
    fees: Money
    pnl: Money  # after fees
    pnl_pct: float  # pnl / (qty * entry_price), in percent
    exit_reason: Literal[
        "signal", "stop", "rebalance", "manual", "flatten", "flatten_no_bar", "delist_no_bar"
    ]


class RoundTripPage(BaseModel):
    items: list[RoundTripOut]  # newest exit first
    total: int  # round trips matching the filter (before `limit`)
    limit: int
    win_rate: float | None  # share of matching round trips with pnl > 0, 0..1
    total_pnl: Money


class DailyOut(BaseModel):
    """One New York trading session of a portfolio versus its benchmark."""

    date: date
    start_equity: Money
    end_equity: Money
    pnl: Money
    pnl_pct: float
    n_roundtrips: int
    benchmark_pnl_pct: float | None  # None when the benchmark has no snapshot for the session


class OrderOut(BaseModel):
    order_id: str
    sleeve_key: str
    symbol: str
    side: str
    origin: str
    status: str
    qty_requested: Money
    qty_filled: Money
    created_asof: date | datetime  # a date for daily bars, the bar-open timestamp for 15m
    fill_asof: date | datetime | None
    fill_price: Money | None
    fee: Money | None
    reason: str | None


class EquityPoint(BaseModel):
    ts: datetime
    equity: Money
    drawdown_pct: float
    benchmark_equity: Money | None = None
    benchmark_missing: bool = False  # a benchmark is configured but has no snapshot at this ts


class KillSwitchOut(BaseModel):
    tripped: bool
    tripped_at_bar: datetime | None
    drawdown_pct: float | None
    threshold_pct: float
    acknowledged_at: datetime | None
    note: str | None
    pause_ends_after_bar: date | datetime | None = None


class KillSwitchAckIn(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


class PortfolioDetail(PortfolioSummary):
    cash: Money  # all cash, settled or not
    unsettled_cash: Money  # sale money arriving at the next session (T+1 accounts; else 0)
    settled_cash: Money  # cash - unsettled_cash: what can fund a buy right now
    as_of: datetime | None = None  # bar the cash figures belong to (None before the first bar)
    sleeves: list[SleeveBreakdown]
    kill_switch: KillSwitchOut
    benchmarks: list[PortfolioSummary]
    caveats: list[str] = Field(default_factory=list)


class StrategyCard(BaseModel):
    strategy_id: int
    name: str
    version: int
    market: str
    return_1y_pct: float | None
    max_drawdown_pct: float | None
    sortino: float | None
    benchmark_symbol: str
    benchmark_return_1y_pct: float | None
    source: Literal["paper", "backtest"]
    as_of: datetime | None


class StrategyConfigIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    market: str
    schema_: dict[str, Any]


class StrategyConfigOut(BaseModel):
    id: int
    name: str
    market: str
    schema_: dict[str, Any]
    version: int
    created_at: datetime


class BacktestRunIn(BaseModel):
    strategy_id: int
    start: date
    end: date | None = None
    mode: Literal["single", "walk_forward", "sensitivity"] = "single"
    overrides: dict[str, Any] = {}


class BacktestRunSummary(BaseModel):
    id: int
    strategy_id: int | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    total_return_pct: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown_pct: float | None
    n_trades: int | None
    benchmark_symbol: str


class BacktestRunDetail(BacktestRunSummary):
    params: dict[str, Any]
    result: dict[str, Any] | None
    equity: list[EquityPoint]
    trades: list[TradeOut]
    error: str | None


class SleeveStatus(BaseModel):
    key: str
    last_processed_bar: datetime | None
    last_status: str
    last_error: str | None


class RunnerStatus(BaseModel):
    alive: bool
    last_heartbeat: datetime | None
    sleeves: list[SleeveStatus]
    next_run_after: datetime | None = None
