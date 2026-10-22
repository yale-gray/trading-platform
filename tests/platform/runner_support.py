"""Shared helpers for the runner and DB tests (no tests here)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import select

from tradebot.clock import FixedClock
from tradebot.config import Settings
from tradebot.data import MarketData
from tradebot.data.config import DataConfig
from tradebot.db import repo
from tradebot.db.engine import session_scope
from tradebot.db.models import (
    EngineCheckpoint,
    EquitySnapshot,
    LedgerEventRow,
    OrderRow,
    PositionSnapshot,
    RunLog,
    RunnerState,
)
from tradebot.runner import (
    Runner,
    benchmark_definition,
    create_portfolio,
    fund_definition,
)
from tradebot.runner.state import FaultHook, PortfolioDef

START = date(2019, 1, 2)
DATA_START = date(2018, 1, 2)
SYNTH_NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def make_data(cache_dir: Path) -> MarketData:
    """Offline ``MarketData`` over the writable copy of the synthetic fixture cache."""
    return MarketData(DataConfig(cache_dir=cache_dir), offline=True, clock=FixedClock(SYNTH_NOW))


def after_close(d: date, minutes: int = 11) -> datetime:
    """A UTC instant at which bar ``d`` is complete for both equity and crypto."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=1, minutes=minutes)


def defs(
    start: date = START, *, fund: bool = True, benchmarks: Sequence[str] = ("SPY", "BTC/USD")
) -> list[PortfolioDef]:
    out = [benchmark_definition(b, start, data_start=DATA_START) for b in benchmarks]
    if fund:
        out.append(fund_definition(start, data_start=DATA_START))
    return out


def seed(db: Engine, definitions: Sequence[PortfolioDef], now: datetime = SYNTH_NOW) -> None:
    with session_scope(db) as s:
        for d in definitions:
            create_portfolio(s, d, now)


def make_runner(
    db: Engine,
    data: MarketData,
    hook: FaultHook | None = None,
    *,
    max_catchup: int = 400,
) -> Runner:
    return Runner(
        db,
        data,
        Settings(max_catchup_bars=max_catchup),
        FixedClock(SYNTH_NOW),
        fault_hook=hook,
        refresh=False,
    )


class SimulatedCrash(BaseException):
    """Stands in for a process death: not an ``Exception``, so the runner never swallows it."""


def crash_at(point: str, on_call: int = 1) -> FaultHook:
    """Hook that raises ``SimulatedCrash`` the ``on_call``-th time ``point`` is reached."""
    seen = {"n": 0}

    def hook(p: str) -> None:
        if p == point:
            seen["n"] += 1
            if seen["n"] == on_call:
                raise SimulatedCrash(point)

    return hook


def _detail(text: str) -> str:
    """``detail_json`` without the ``catchup`` audit flag (it depends on tick batching)."""
    d = json.loads(text)
    d.pop("catchup", None)
    return json.dumps(d, sort_keys=True)


def db_fingerprint(db: Engine, name: str | None = None) -> dict[str, str]:
    """Digest of every execution table (wall-clock columns excluded) for equality checks."""
    out: dict[str, str] = {}
    with session_scope(db) as s:
        for p in repo.list_portfolios(s):
            if name is not None and p.name != name:
                continue
            pid = int(p.id or 0)
            parts: dict[str, list[object]] = {
                "ledger": [
                    (r.seq, r.event_json)
                    for r in s.exec(
                        select(LedgerEventRow)
                        .where(LedgerEventRow.portfolio_id == pid)
                        .order_by(LedgerEventRow.seq)  # type: ignore[arg-type]
                    )
                ],
                "orders": [
                    (r.order_id, r.order_json, r.status)
                    for r in s.exec(
                        select(OrderRow)
                        .where(OrderRow.portfolio_id == pid)
                        .order_by(OrderRow.order_id)  # type: ignore[arg-type]
                    )
                ],
                "equity": [
                    (str(r.ts), r.sleeve_id, r.equity, r.cash, r.drawdown, r.benchmark_equity)
                    for r in s.exec(
                        select(EquitySnapshot)
                        .where(EquitySnapshot.portfolio_id == pid)
                        .order_by(EquitySnapshot.ts, EquitySnapshot.sleeve_id)  # type: ignore[arg-type]
                    )
                ],
                "positions": [
                    (str(r.ts), r.sleeve_id, r.symbol, r.qty, r.market_value)
                    for r in s.exec(
                        select(PositionSnapshot)
                        .where(PositionSnapshot.portfolio_id == pid)
                        .order_by(
                            PositionSnapshot.ts, PositionSnapshot.sleeve_id, PositionSnapshot.symbol
                        )  # type: ignore[arg-type]
                    )
                ],
                "checkpoints": [
                    (str(r.bar_ts), r.state_json, r.target_book_json, r.ledger_seq)
                    for r in s.exec(
                        select(EngineCheckpoint)
                        .where(EngineCheckpoint.portfolio_id == pid)
                        .order_by(EngineCheckpoint.bar_ts)  # type: ignore[arg-type]
                    )
                ],
                "run_log": [
                    (
                        str(r.bar_ts),
                        r.outcome,
                        r.n_orders_created,
                        r.n_fills,
                        _detail(r.detail_json),
                    )
                    for r in s.exec(
                        select(RunLog).where(RunLog.portfolio_id == pid).order_by(RunLog.bar_ts)  # type: ignore[arg-type]
                    )
                ],
            }
            rs = s.get(RunnerState, pid)
            parts["marker"] = [str(rs.last_processed_bar_ts) if rs else None]
            for key, rows in parts.items():
                out[f"{p.name}.{key}"] = hashlib.sha256(
                    json.dumps(rows, sort_keys=True, default=str).encode()
                ).hexdigest()
    return out


def counts(db: Engine, name: str) -> dict[str, int]:
    """Row counts of the per-bar tables for one portfolio."""
    with session_scope(db) as s:
        p = repo.get_portfolio(s, name)
        assert p is not None and p.id is not None
        pid = p.id
        rs = s.get(RunnerState, pid)
        return {
            "ledger": len(
                s.exec(select(LedgerEventRow).where(LedgerEventRow.portfolio_id == pid)).all()
            ),
            "equity": len(
                s.exec(select(EquitySnapshot).where(EquitySnapshot.portfolio_id == pid)).all()
            ),
            "checkpoints": len(
                s.exec(select(EngineCheckpoint).where(EngineCheckpoint.portfolio_id == pid)).all()
            ),
            "run_log": len(s.exec(select(RunLog).where(RunLog.portfolio_id == pid)).all()),
            "marker": 0 if rs is None or rs.last_processed_bar_ts is None else 1,
        }


class LaggingData:
    """``MarketData`` stand-in whose provider has no bars after ``cutoff`` (yet)."""

    def __init__(self, inner: MarketData, cutoff: date) -> None:
        self.inner = inner
        self.cutoff = cutoff

    def get_bars_many(self, symbols: Sequence[str], start: date, end: date, **kw: object):  # type: ignore[no-untyped-def]
        return self.inner.get_bars_many(symbols, start, min(end, self.cutoff), **kw)  # type: ignore[arg-type]
