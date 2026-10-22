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


class OrderOut(BaseModel):
    order_id: str
    sleeve_key: str
    symbol: str
    side: str
    origin: str
    status: str
    qty_requested: Money
    qty_filled: Money
    created_asof: date
    fill_asof: date | None
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


class KillSwitchAckIn(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


class PortfolioDetail(PortfolioSummary):
    sleeves: list[SleeveBreakdown]
    kill_switch: KillSwitchOut
    benchmarks: list[PortfolioSummary]


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
