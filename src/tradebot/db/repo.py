"""Repository functions (SPEC-004 T3). Every function takes an open ``Session``.

Writers are called inside one ``session_scope`` per portfolio per bar by the runner;
readers are for the API. Money is converted to and from TEXT here and nowhere else.
Sums and ordering of money happen in Python on ``Decimal``, never in SQL.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import delete, func
from sqlmodel import Session, col, select

from tradebot.db.models import (
    EngineCheckpoint,
    EquitySnapshot,
    KillSwitchEvent,
    KillSwitchState,
    LedgerEventRow,
    OrderRow,
    Portfolio,
    PositionSnapshot,
    RunLog,
    RunnerHeartbeat,
    RunnerState,
    Sleeve,
    StrategyConfig,
)
from tradebot.portfolio.models import LedgerEvent, PortfolioSnapshot
from tradebot.portfolio.money import from_text, to_text
from tradebot.sim.orders import Order

TOTAL_SLEEVE_ID = 0
"""``equity_snapshot.sleeve_id`` of the whole-portfolio row."""


# ---------------------------------------------------------------------------- helpers


def dec_to_text(value: Decimal) -> str:
    """Canonical TEXT form of a Decimal (inverse: :func:`text_to_dec`)."""
    return to_text(value)


def text_to_dec(text: str) -> Decimal:
    """Parse a TEXT money/quantity column back into a ``Decimal``."""
    return from_text(text)


def naive_utc(value: pd.Timestamp | datetime | date) -> datetime:
    """UTC, timezone-naive ``datetime`` (a ``date`` becomes its midnight)."""
    if isinstance(value, pd.Timestamp):
        value = value.tz_convert("UTC").to_pydatetime() if value.tzinfo else value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            from datetime import UTC

            value = value.astimezone(UTC).replace(tzinfo=None)
        return value
    return datetime(value.year, value.month, value.day)


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else to_text(value)


# ---------------------------------------------------------------------------- portfolios


def get_portfolio(session: Session, key: int | str) -> Portfolio | None:
    """Portfolio by integer id or unique name."""
    if isinstance(key, int):
        return session.get(Portfolio, key)
    return session.exec(select(Portfolio).where(Portfolio.name == key)).first()


def list_portfolios(session: Session, *, include_archived: bool = False) -> list[Portfolio]:
    """Portfolios in id order (archived ones only on request)."""
    stmt = select(Portfolio).order_by(col(Portfolio.id))
    if not include_archived:
        stmt = stmt.where(Portfolio.status != "archived")
    return list(session.exec(stmt))


def list_sleeves(session: Session, portfolio_id: int) -> list[Sleeve]:
    """Sleeves of a portfolio in id order."""
    return list(
        session.exec(
            select(Sleeve).where(Sleeve.portfolio_id == portfolio_id).order_by(col(Sleeve.id))
        )
    )


def sleeve_ids_by_key(session: Session, portfolio_id: int) -> dict[str, int]:
    """``{engine sleeve key: sleeve.id}`` for a portfolio."""
    return {s.key: int(s.id or 0) for s in list_sleeves(session, portfolio_id)}


def get_or_create_strategy_config(
    session: Session, *, name: str, market: str, spec_json: str, now: datetime
) -> StrategyConfig:
    """Return the config with this canonical JSON (dedupe on sha256), else insert a new version."""
    h = hashlib.sha256(spec_json.encode()).hexdigest()
    found = session.exec(
        select(StrategyConfig)
        .where(StrategyConfig.name == name, StrategyConfig.schema_hash == h)
        .order_by(col(StrategyConfig.version).desc())
    ).first()
    if found is not None:
        return found
    latest = session.exec(
        select(func.max(StrategyConfig.version)).where(StrategyConfig.name == name)
    ).one()
    row = StrategyConfig(
        name=name,
        version=int(latest or 0) + 1,
        market=market,
        spec_json=spec_json,
        schema_hash=h,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------- runner state


def get_runner_state(session: Session, portfolio_id: int) -> RunnerState:
    """The portfolio's runner_state row, created empty on first use."""
    row = session.get(RunnerState, portfolio_id)
    if row is None:
        row = RunnerState(portfolio_id=portfolio_id)
        session.add(row)
        session.flush()
    return row


def record_status(
    session: Session, portfolio_id: int, *, status: str, error: str | None, now: datetime
) -> RunnerState:
    """Update ``last_status`` / ``last_error`` / failure counter (never the bar marker)."""
    row = get_runner_state(session, portfolio_id)
    row.last_run_at = now
    row.last_status = status
    row.last_error = error
    row.consecutive_failures = 0 if status == "ok" else row.consecutive_failures + 1
    session.add(row)
    return row


def write_heartbeat(
    session: Session, *, now: datetime, pid: int | None = None, version: str = ""
) -> None:
    """Upsert the single heartbeat row."""
    row = session.get(RunnerHeartbeat, 1)
    pid = os.getpid() if pid is None else pid
    if row is None:
        session.add(RunnerHeartbeat(id=1, ts=now, pid=pid, version=version))
    else:
        row.ts, row.pid, row.version = now, pid, version
        session.add(row)


# ---------------------------------------------------------------------------- ledger


def ledger_count(session: Session, portfolio_id: int) -> int:
    """Number of persisted ledger events (== highest ``seq``)."""
    return int(
        session.exec(
            select(func.count())
            .select_from(LedgerEventRow)
            .where(LedgerEventRow.portfolio_id == portfolio_id)
        ).one()
    )


def append_ledger_events(
    session: Session,
    portfolio_id: int,
    events: Iterable[LedgerEvent],
    sleeve_ids: Mapping[str, int],
) -> int:
    """Append events (append-only; a duplicate ``seq`` raises ``IntegrityError``). Returns count."""
    n = 0
    for ev in events:
        session.add(
            LedgerEventRow(
                portfolio_id=portfolio_id,
                seq=ev.seq,
                sleeve_id=sleeve_ids.get(ev.sleeve_id),
                sleeve_key=ev.sleeve_id,
                asof=ev.asof,
                stage=ev.stage.value,
                type=ev.type.value,
                order_id=ev.order_id,
                symbol=ev.symbol,
                side=ev.side.value if ev.side else None,
                qty=_opt(ev.qty),
                price=_opt(ev.price),
                amount=to_text(ev.amount),
                slippage_cost=_opt(ev.slippage_cost),
                realized_pnl=_opt(ev.realized_pnl),
                event_json=ev.model_dump_json(),
            )
        )
        n += 1
    session.flush()
    return n


def ledger_events(session: Session, portfolio_id: int) -> list[LedgerEvent]:
    """The persisted ledger, in ``seq`` order, parsed back into engine events."""
    rows = session.exec(
        select(LedgerEventRow.event_json)
        .where(LedgerEventRow.portfolio_id == portfolio_id)
        .order_by(col(LedgerEventRow.seq))
    )
    return [LedgerEvent.model_validate_json(r) for r in rows]


def ledger_digest(session: Session, portfolio_id: int) -> str:
    """sha256 over the persisted ``event_json`` rows in ``seq`` order (equality checks)."""
    h = hashlib.sha256()
    rows = session.exec(
        select(LedgerEventRow.event_json)
        .where(LedgerEventRow.portfolio_id == portfolio_id)
        .order_by(col(LedgerEventRow.seq))
    )
    for text in rows:
        h.update(text.encode())
        h.update(b"\n")
    return h.hexdigest()


def events_digest(events: Iterable[LedgerEvent]) -> str:
    """Same digest as :func:`ledger_digest` computed from in-memory engine events."""
    h = hashlib.sha256()
    for ev in events:
        h.update(ev.model_dump_json().encode())
        h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------------------- orders


def upsert_orders(
    session: Session,
    portfolio_id: int,
    orders: Iterable[Order],
    sleeve_ids: Mapping[str, int],
    now: datetime,
) -> int:
    """Insert new orders and update changed ones (keyed by engine order id). Returns count."""
    n = 0
    for o in orders:
        row = session.exec(
            select(OrderRow).where(
                OrderRow.portfolio_id == portfolio_id, OrderRow.order_id == o.order_id
            )
        ).first()
        if row is None:
            row = OrderRow(
                portfolio_id=portfolio_id,
                sleeve_id=sleeve_ids[o.sleeve_id],
                order_id=o.order_id,
                symbol=o.symbol,
                side=o.side.value,
                origin=o.origin.value,
                status=o.status.value,
                qty_requested=to_text(o.qty_requested),
                qty_filled=to_text(o.qty_filled),
                created_asof=o.created_asof,
                order_json=o.model_dump_json(),
                idempotency_key=f"{portfolio_id}:{o.order_id}",
                updated_at=now,
            )
        row.status = o.status.value
        row.qty_accepted = _opt(o.qty_accepted)
        row.qty_filled = to_text(o.qty_filled)
        row.fill_asof = o.fill_asof
        row.ref_price = _opt(o.ref_price)
        row.fill_price = _opt(o.fill_price)
        row.fee = _opt(o.fee)
        row.reason = o.reason.value if o.reason else None
        row.order_json = o.model_dump_json()
        row.updated_at = now
        session.add(row)
        n += 1
    session.flush()
    return n


# ---------------------------------------------------------------------------- snapshots


def write_snapshots(
    session: Session,
    portfolio_id: int,
    ts: datetime,
    snap: PortfolioSnapshot,
    sleeve_ids: Mapping[str, int],
    *,
    benchmark_equity: Decimal | None = None,
) -> None:
    """Write the sleeve and total ``equity_snapshot`` rows and ``position_snapshot`` rows."""
    session.add(
        EquitySnapshot(
            portfolio_id=portfolio_id,
            sleeve_id=TOTAL_SLEEVE_ID,
            ts=ts,
            equity=to_text(snap.equity),
            cash=to_text(snap.cash),
            drawdown=to_text(snap.drawdown),
            benchmark_equity=_opt(benchmark_equity),
            unsettled=to_text(snap.unsettled),
        )
    )
    for sl in snap.sleeves:
        sid = sleeve_ids[sl.sleeve_id]
        session.add(
            EquitySnapshot(
                portfolio_id=portfolio_id,
                sleeve_id=sid,
                ts=ts,
                equity=to_text(sl.equity),
                cash=to_text(sl.cash),
                unsettled=to_text(sl.unsettled),
            )
        )
        for p in sl.positions:
            session.add(
                PositionSnapshot(
                    portfolio_id=portfolio_id,
                    sleeve_id=sid,
                    ts=ts,
                    symbol=p.symbol,
                    qty=to_text(p.qty),
                    cost_basis=to_text(p.cost_basis),
                    market_value=to_text(p.mv),
                    unrealized=to_text(p.unrealized),
                    highest_close=_opt(p.high_water_close),
                    stop_level=_opt(p.stop_level),
                    entry_asof=p.entry_asof,
                )
            )
    session.flush()


def equity_curve(
    session: Session, portfolio_id: int
) -> list[tuple[datetime, Decimal, Decimal | None]]:
    """``(ts, equity, benchmark_equity)`` of the total rows, ascending."""
    rows = session.exec(
        select(EquitySnapshot)
        .where(
            EquitySnapshot.portfolio_id == portfolio_id, EquitySnapshot.sleeve_id == TOTAL_SLEEVE_ID
        )
        .order_by(col(EquitySnapshot.ts))
    )
    return [
        (
            r.ts,
            text_to_dec(r.equity),
            text_to_dec(r.benchmark_equity) if r.benchmark_equity is not None else None,
        )
        for r in rows
    ]


def total_equity_at(session: Session, portfolio_id: int, ts: datetime) -> Decimal | None:
    """Total equity of a portfolio at exactly ``ts`` (``None`` if no such snapshot)."""
    row = session.get(EquitySnapshot, (portfolio_id, TOTAL_SLEEVE_ID, ts))
    return None if row is None else text_to_dec(row.equity)


def latest_positions(session: Session, portfolio_id: int) -> list[PositionSnapshot]:
    """Open positions at the latest processed bar (none after an end-of-day flatten).

    The bar is the latest equity snapshot, not the latest position row: a flat book writes no
    position rows, and the last bar holding something would show stale positions.
    """
    last = session.exec(
        select(func.max(EquitySnapshot.ts)).where(EquitySnapshot.portfolio_id == portfolio_id)
    ).one()
    if last is None:
        return []
    return list(
        session.exec(
            select(PositionSnapshot)
            .where(PositionSnapshot.portfolio_id == portfolio_id, PositionSnapshot.ts == last)
            .order_by(col(PositionSnapshot.sleeve_id), col(PositionSnapshot.symbol))
        )
    )


# ---------------------------------------------------------------------------- checkpoints


def write_checkpoint(
    session: Session,
    portfolio_id: int,
    bar_ts: datetime,
    state_json: str,
    target_book_json: str,
    ledger_seq: int,
    now: datetime,
) -> None:
    """Insert the bar's checkpoint (one per bar; a duplicate raises ``IntegrityError``)."""
    session.add(
        EngineCheckpoint(
            portfolio_id=portfolio_id,
            bar_ts=bar_ts,
            state_json=state_json,
            target_book_json=target_book_json,
            ledger_seq=ledger_seq,
            created_at=now,
        )
    )
    session.flush()


def latest_checkpoint(session: Session, portfolio_id: int) -> EngineCheckpoint | None:
    """The checkpoint with the greatest ``bar_ts``."""
    return session.exec(
        select(EngineCheckpoint)
        .where(EngineCheckpoint.portfolio_id == portfolio_id)
        .order_by(col(EngineCheckpoint.bar_ts).desc())
    ).first()


def get_checkpoint(
    session: Session, portfolio_id: int, bar_ts: datetime
) -> EngineCheckpoint | None:
    """The checkpoint at an exact bar."""
    return session.get(EngineCheckpoint, (portfolio_id, bar_ts))


def prune_checkpoints(session: Session, portfolio_id: int, keep: int) -> int:
    """Keep the newest ``keep`` checkpoints plus the first one of each month; delete the rest."""
    stamps = list(
        session.exec(
            select(EngineCheckpoint.bar_ts)
            .where(EngineCheckpoint.portfolio_id == portfolio_id)
            .order_by(col(EngineCheckpoint.bar_ts))
        )
    )
    if len(stamps) <= keep:
        return 0
    protected = set(stamps[-keep:])
    seen: set[tuple[int, int]] = set()
    for ts in stamps:
        key = (ts.year, ts.month)
        if key not in seen:
            seen.add(key)
            protected.add(ts)
    doomed = [ts for ts in stamps if ts not in protected]
    if doomed:
        session.exec(  # type: ignore[call-overload]
            delete(EngineCheckpoint).where(
                EngineCheckpoint.portfolio_id == portfolio_id,
                col(EngineCheckpoint.bar_ts).in_(doomed),
            )
        )
    return len(doomed)


# ---------------------------------------------------------------------------- kill switch


def get_kill_switch(session: Session, portfolio_id: int) -> KillSwitchState | None:
    """Kill-switch mirror row."""
    return session.get(KillSwitchState, portfolio_id)


def mirror_kill_switch(
    session: Session,
    portfolio_id: int,
    *,
    tripped: bool,
    peak_equity: Decimal,
    bar_ts: datetime,
    drawdown: Decimal,
    events: Sequence[str],
    now: datetime,
) -> None:
    """Copy the engine's kill-switch state; log ``trip`` / ``reset`` / ``rearm`` audit rows.

    ``events`` holds the ledger event types of this bar (``KILL_SWITCH_TRIPPED`` /
    ``KILL_SWITCH_RESET`` / ``KILL_SWITCH_REARMED``). A trip clears any earlier acknowledgement.
    """
    row = session.get(KillSwitchState, portfolio_id)
    if row is None:
        row = KillSwitchState(portfolio_id=portfolio_id, peak_equity=to_text(peak_equity))
    if "KILL_SWITCH_TRIPPED" in events:
        row.tripped_at_bar = bar_ts
        row.trigger_drawdown = to_text(drawdown)
        row.acknowledged_at = None
        row.ack_note = None
        session.add(
            KillSwitchEvent(portfolio_id=portfolio_id, ts=now, kind="trip", note=str(bar_ts.date()))
        )
    if "KILL_SWITCH_RESET" in events:
        row.tripped_at_bar = None
        row.trigger_drawdown = None
        session.add(
            KillSwitchEvent(
                portfolio_id=portfolio_id, ts=now, kind="reset", note=str(bar_ts.date())
            )
        )
    if "KILL_SWITCH_REARMED" in events:
        row.tripped_at_bar = None
        row.trigger_drawdown = None
        row.acknowledged_at = None
        row.ack_note = None
        session.add(
            KillSwitchEvent(
                portfolio_id=portfolio_id, ts=now, kind="rearm", note=str(bar_ts.date())
            )
        )
    row.tripped = tripped
    row.peak_equity = to_text(peak_equity)
    session.add(row)


def acknowledge_kill_switch(
    session: Session, portfolio_id: int, note: str | None, now: datetime
) -> bool:
    """API ack: sets the review flag only. Never resets. Returns False if not tripped."""
    row = session.get(KillSwitchState, portfolio_id)
    if row is None or not row.tripped:
        return False
    row.acknowledged_at = now
    row.ack_note = note
    session.add(row)
    session.add(KillSwitchEvent(portfolio_id=portfolio_id, ts=now, kind="ack", note=note))
    return True


# ---------------------------------------------------------------------------- run log


def insert_run_log(
    session: Session,
    portfolio_id: int,
    bar_ts: datetime,
    *,
    started_at: datetime,
    finished_at: datetime,
    n_orders_created: int,
    n_fills: int,
    detail: Mapping[str, Any],
    outcome: str = "ok",
) -> None:
    """Insert the (portfolio, bar) log row; a second insert raises ``IntegrityError``."""
    session.add(
        RunLog(
            portfolio_id=portfolio_id,
            bar_ts=bar_ts,
            started_at=started_at,
            finished_at=finished_at,
            outcome=outcome,
            n_orders_created=n_orders_created,
            n_fills=n_fills,
            detail_json=json.dumps(detail, sort_keys=True),
        )
    )
    session.flush()


def replace_checkpoint(
    session: Session,
    portfolio_id: int,
    bar_ts: datetime,
    state_json: str,
    target_book_json: str,
    ledger_seq: int,
    now: datetime,
) -> None:
    """Overwrite the checkpoint at ``bar_ts`` (operator actions such as a kill-switch reset)."""
    session.exec(  # type: ignore[call-overload]
        delete(EngineCheckpoint).where(
            EngineCheckpoint.portfolio_id == portfolio_id, EngineCheckpoint.bar_ts == bar_ts
        )
    )
    write_checkpoint(session, portfolio_id, bar_ts, state_json, target_book_json, ledger_seq, now)
