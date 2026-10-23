"""SQLModel tables (SPEC-004 "DB tables", ADR-0001 D7/D8).

Conventions
-----------
* Money, quantities, prices and ratios are ``TEXT`` holding the canonical ``str(Decimal)``
  of the engine value (no second rounding layer). Never ``int`` / ``float`` columns.
* Bar and event timestamps are UTC, stored timezone-naive (``bar_ts`` is the bar's UTC
  midnight label). ``asof`` columns (``AsOfType``) hold a plain date for a daily bar (stored
  as ``YYYY-MM-DD``, unchanged) and the full naive-UTC bar-open timestamp for a 15m bar
  (ADR-0001 A21), so one column type serves both.
* The ledger plus the latest ``engine_checkpoint`` are the source of truth; every other
  table exists so the API can read quickly.
* Deviation from the spec text: ``order`` is keyed by ``(portfolio_id, order_id)`` (the
  engine's deterministic ``O{n:08d}`` id, ADR A3) and carries the lossless ``order_json``.
  ``idempotency_key`` is kept (``"{portfolio_id}:{order_id}"``, unique).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pydantic import NaiveDatetime
from sqlalchemy import Column, String, Text, TypeDecorator, UniqueConstraint
from sqlmodel import Field, SQLModel

SCHEMA_VERSION = 1


class AsOfType(TypeDecorator[Any]):
    """A ``date`` (daily bar) or a ``datetime`` (15m bar open, stored as naive UTC).

    Text storage: ``2024-01-02`` for a date, ``2024-01-02T14:30:00`` for a timestamp, so rows
    stay readable, sort chronologically and a column created as ``DATE`` by the old schema
    reads back unchanged (SQLite does not enforce declared types).
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(UTC).replace(tzinfo=None)
            return value.isoformat(timespec="seconds")
        return value.isoformat()

    def process_result_value(self, value: Any, dialect: Any) -> date | datetime | None:
        if value is None:
            return None
        text = str(value)
        return date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text)


__all__ = [
    "SCHEMA_VERSION",
    "AsOfType",
    "BacktestEquityPoint",
    "BacktestRun",
    "BacktestTrade",
    "EngineCheckpoint",
    "EquitySnapshot",
    "KillSwitchEvent",
    "KillSwitchState",
    "LedgerEventRow",
    "OrderRow",
    "Portfolio",
    "PositionSnapshot",
    "RunLog",
    "RunnerHeartbeat",
    "RunnerState",
    "SchemaVersion",
    "Sleeve",
    "StrategyConfig",
]


class SchemaVersion(SQLModel, table=True):
    """One row: the schema version the file was created with."""

    __tablename__ = "schema_version"
    id: int = Field(default=1, primary_key=True)
    version: int


class StrategyConfig(SQLModel, table=True):
    """A versioned, immutable strategy definition (``name`` + ``version`` unique)."""

    __tablename__ = "strategy_config"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: int | None = Field(default=None, primary_key=True)
    name: str
    version: int = 1
    market: str  # equity | crypto
    spec_json: str = Field(sa_column=Column(Text, nullable=False))
    schema_hash: str = Field(index=True)
    is_template: bool = False
    created_at: NaiveDatetime


class BacktestRun(SQLModel, table=True):
    """A backtest requested through the API (status machine ``queued/running/done/failed``)."""

    __tablename__ = "backtest_run"
    id: int | None = Field(default=None, primary_key=True)
    strategy_config_id: int | None = Field(default=None, foreign_key="strategy_config.id")
    status: str = "queued"
    params_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    data_fingerprint: str | None = None
    started_at: NaiveDatetime | None = None
    finished_at: NaiveDatetime | None = None
    error: str | None = Field(default=None, sa_column=Column(Text))
    result_json: str | None = Field(default=None, sa_column=Column(Text))
    total_return: float | None = None
    cagr: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    n_trades: int | None = None
    benchmark_symbol: str | None = None
    created_at: NaiveDatetime


class BacktestEquityPoint(SQLModel, table=True):
    """Analytics series (floats are fine here)."""

    __tablename__ = "backtest_equity_point"
    run_id: int = Field(foreign_key="backtest_run.id", primary_key=True)
    ts: NaiveDatetime = Field(primary_key=True)
    equity: float
    benchmark_equity: float | None = None
    drawdown: float | None = None


class BacktestTrade(SQLModel, table=True):
    """A closed round trip from a backtest (analytics floats)."""

    __tablename__ = "backtest_trade"
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="backtest_run.id", index=True)
    symbol: str
    side: str = "long"
    entry_ts: NaiveDatetime
    exit_ts: NaiveDatetime | None = None
    qty: float
    entry_px: float
    exit_px: float | None = None
    pnl: float | None = None
    fees: float | None = None
    sleeve_key: str | None = None


class Portfolio(SQLModel, table=True):
    """A paper portfolio (the fund or a benchmark). Created by the CLI/bootstrap only."""

    __tablename__ = "portfolio"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    kind: str  # fund | benchmark
    initial_cash: str
    base_ccy: str = "USD"
    status: str = "active"  # active | halted | archived
    started_at: NaiveDatetime  # inception bar (UTC midnight label)
    peak_equity: str
    config_json: str = Field(sa_column=Column(Text, nullable=False))
    created_at: NaiveDatetime


class Sleeve(SQLModel, table=True):
    """A strategy allocation inside a portfolio (``key`` is the engine's sleeve id)."""

    __tablename__ = "sleeve"
    __table_args__ = (UniqueConstraint("portfolio_id", "key"),)
    id: int | None = Field(default=None, primary_key=True)
    portfolio_id: int = Field(foreign_key="portfolio.id", index=True)
    key: str
    strategy_config_id: int | None = Field(default=None, foreign_key="strategy_config.id")
    target_weight_bp: int
    market: str  # equity | crypto | cash
    cash: str  # TEXT Decimal, copied from the engine snapshot each bar
    enabled: bool = True
    created_at: NaiveDatetime


class OrderRow(SQLModel, table=True):
    """An order, upserted in the bar transaction from the engine's order book."""

    __tablename__ = "order"
    __table_args__ = (UniqueConstraint("portfolio_id", "order_id"),)
    id: int | None = Field(default=None, primary_key=True)
    portfolio_id: int = Field(foreign_key="portfolio.id", index=True)
    sleeve_id: int = Field(foreign_key="sleeve.id")
    order_id: str  # engine id, e.g. "O00000012"
    symbol: str
    side: str  # BUY | SELL
    order_type: str = "market"
    origin: str  # STRATEGY | STOP | REBALANCE | MANUAL
    status: str  # engine OrderStatus value
    qty_requested: str
    qty_accepted: str | None = None
    qty_filled: str
    created_asof: date | datetime = Field(sa_column=Column(AsOfType, nullable=False))  # signal bar
    fill_asof: date | datetime | None = Field(default=None, sa_column=Column(AsOfType))
    ref_price: str | None = None
    fill_price: str | None = None
    fee: str | None = None
    reason: str | None = None
    order_json: str = Field(sa_column=Column(Text, nullable=False))
    idempotency_key: str = Field(unique=True)
    updated_at: NaiveDatetime


class LedgerEventRow(SQLModel, table=True):
    """Append-only ledger. ``event_json`` is the engine event verbatim (lossless restore)."""

    __tablename__ = "ledger_event"
    __table_args__ = (UniqueConstraint("portfolio_id", "seq"),)
    id: int | None = Field(default=None, primary_key=True)
    portfolio_id: int = Field(foreign_key="portfolio.id", index=True)
    seq: int
    sleeve_id: int | None = Field(default=None, foreign_key="sleeve.id")
    sleeve_key: str
    asof: date | datetime = Field(sa_column=Column(AsOfType, nullable=False))
    stage: str
    type: str
    order_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    qty: str | None = None
    price: str | None = None
    amount: str  # cents, sign implied by type
    slippage_cost: str | None = None
    realized_pnl: str | None = None
    event_json: str = Field(sa_column=Column(Text, nullable=False))


class PositionSnapshot(SQLModel, table=True):
    """Open positions as of each processed bar."""

    __tablename__ = "position_snapshot"
    portfolio_id: int = Field(foreign_key="portfolio.id", primary_key=True)
    sleeve_id: int = Field(foreign_key="sleeve.id", primary_key=True)
    ts: NaiveDatetime = Field(primary_key=True)
    symbol: str = Field(primary_key=True)
    qty: str
    cost_basis: str
    market_value: str
    unrealized: str
    highest_close: str | None = None
    stop_level: str | None = None
    entry_asof: date | datetime | None = Field(default=None, sa_column=Column(AsOfType))


class EquitySnapshot(SQLModel, table=True):
    """Equity per sleeve and in total (``sleeve_id`` 0 means the whole portfolio).

    The spec says NULL for the total row; SQLite primary-key columns may hold NULL and then
    stop being unique, so the total row uses ``sleeve_id = 0`` (no sleeve has id 0).
    """

    __tablename__ = "equity_snapshot"
    portfolio_id: int = Field(foreign_key="portfolio.id", primary_key=True)
    sleeve_id: int = Field(default=0, primary_key=True)
    ts: NaiveDatetime = Field(primary_key=True)
    equity: str
    cash: str
    drawdown: str | None = None
    benchmark_equity: str | None = None
    unsettled: str | None = None  # sale money not yet spendable (T+1); NULL on rows before A21


class EngineCheckpoint(SQLModel, table=True):
    """One per processed bar: ``PortfolioEngine.export_state()`` JSON plus the TargetBook."""

    __tablename__ = "engine_checkpoint"
    portfolio_id: int = Field(foreign_key="portfolio.id", primary_key=True)
    bar_ts: NaiveDatetime = Field(primary_key=True)
    state_json: str = Field(sa_column=Column(Text, nullable=False))
    target_book_json: str = Field(sa_column=Column(Text, nullable=False))
    ledger_seq: int
    created_at: NaiveDatetime


class KillSwitchState(SQLModel, table=True):
    """Mirror of the engine's kill switch (the engine owns it; copied each bar)."""

    __tablename__ = "kill_switch_state"
    portfolio_id: int = Field(foreign_key="portfolio.id", primary_key=True)
    tripped: bool = False
    tripped_at_bar: NaiveDatetime | None = None
    trigger_drawdown: str | None = None
    peak_equity: str
    acknowledged_at: NaiveDatetime | None = None
    ack_note: str | None = None


class KillSwitchEvent(SQLModel, table=True):
    """Audit log: ``trip`` / ``ack`` / ``reset`` / ``rearm`` (``reset``: CLI only)."""

    __tablename__ = "kill_switch_event"
    id: int | None = Field(default=None, primary_key=True)
    portfolio_id: int = Field(foreign_key="portfolio.id", index=True)
    ts: NaiveDatetime
    kind: str
    note: str | None = None


class RunnerState(SQLModel, table=True):
    """Idempotency anchor, one row per portfolio."""

    __tablename__ = "runner_state"
    portfolio_id: int = Field(foreign_key="portfolio.id", primary_key=True)
    last_processed_bar_ts: NaiveDatetime | None = None
    last_run_at: NaiveDatetime | None = None
    last_status: str = "ok"  # ok | error | data_unavailable
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    consecutive_failures: int = 0


class RunLog(SQLModel, table=True):
    """One row per (portfolio, bar); the UNIQUE constraint is defence in depth."""

    __tablename__ = "run_log"
    __table_args__ = (UniqueConstraint("portfolio_id", "bar_ts"),)
    id: int | None = Field(default=None, primary_key=True)
    portfolio_id: int = Field(foreign_key="portfolio.id", index=True)
    bar_ts: NaiveDatetime
    started_at: NaiveDatetime
    finished_at: NaiveDatetime
    outcome: str = "ok"
    n_orders_created: int = 0
    n_fills: int = 0
    detail_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))


class RunnerHeartbeat(SQLModel, table=True):
    """Single row for the dashboard status badge."""

    __tablename__ = "runner_heartbeat"
    id: int = Field(default=1, primary_key=True)
    ts: NaiveDatetime
    pid: int
    version: str
