"""Portfolio creation, presets, restore and the operator kill-switch reset (SPEC-004 T4).

``create_portfolio`` is what the CLI (``portfolio create``) calls; ``restore_context`` is what
the runner calls on start and after any failed bar; ``reset_kill_switch`` is the CLI-only
operator action (the API ack never resets).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import Engine
from sqlmodel import Session

from tradebot.backtest.driver import TargetBook
from tradebot.backtest.engine import BacktestConfig, SleeveSpec, _bench_config
from tradebot.db import repo
from tradebot.db.engine import session_scope
from tradebot.db.models import KillSwitchState, Portfolio, Sleeve
from tradebot.portfolio.config import EngineConfig, default_config, sleeve_deposits
from tradebot.portfolio.engine import PortfolioEngine, StateCorruptionError
from tradebot.portfolio.money import to_text
from tradebot.runner.clock import market_of
from tradebot.runner.state import PortfolioContext, PortfolioDef, RunnerError
from tradebot.strategy.io import STRATEGIES_DIR, dump_strategy, load_strategy

__all__ = [
    "benchmark_definition",
    "create_portfolio",
    "fund_definition",
    "reset_kill_switch",
    "restore_context",
]

DEFAULT_HISTORY_DAYS = 1100
"""Calendar days of history before the inception bar (SMA200/ret126 warm-up with margin)."""


def fund_definition(
    start: date,
    *,
    data_start: date | None = None,
    name: str = "paper_fund",
    engine: EngineConfig | None = None,
    strategies_dir: Path | None = None,
    benchmark: str | None = "bench_spy",
) -> PortfolioDef:
    """The 50/20/20/10 paper fund (STRATEGY section 4) using ``strategies/<sleeve>.yaml``."""
    eng = engine or default_config()
    root = strategies_dir or STRATEGIES_DIR
    sleeves = tuple(
        SleeveSpec(
            sleeve_id=s.sleeve_id,
            strategy=None if s.sleeve_id == "cash" else load_strategy(root / f"{s.sleeve_id}.yaml"),
        )
        for s in eng.sleeves
    )
    return PortfolioDef(
        name=name,
        kind="fund",
        engine=eng,
        sleeves=sleeves,
        start=start,
        data_start=data_start or start - timedelta(days=DEFAULT_HISTORY_DAYS),
        benchmark=benchmark,
    )


def benchmark_definition(
    symbol: str,
    start: date,
    *,
    data_start: date | None = None,
    name: str | None = None,
    engine: EngineConfig | None = None,
) -> PortfolioDef:
    """Buy-and-hold ``symbol`` through the same engine (identical to ``run_with_benchmark``)."""
    base = engine or default_config()
    sid = "bh_crypto" if market_of(symbol) == "crypto" else "bh_equity"
    cfg = _bench_config(
        BacktestConfig(sleeves=[], start=start, end=start, engine=base),
        [(sid, Decimal(1), symbol)],
        Decimal(0),
    )
    return PortfolioDef(
        name=name or f"bench_{symbol.split('/')[0].lower()}",
        kind="benchmark",
        engine=cfg.engine,
        sleeves=tuple(cfg.sleeves),
        start=start,
        data_start=data_start or start - timedelta(days=DEFAULT_HISTORY_DAYS),
    )


def create_portfolio(session: Session, defn: PortfolioDef, now: datetime) -> Portfolio:
    """Insert the portfolio with its sleeves, runner_state and kill-switch rows.

    Raises ``IntegrityError`` if the name exists. Portfolios are created by the CLI only.
    """
    now = repo.naive_utc(now)
    cfg = defn.engine_config()
    deposits = sleeve_deposits(cfg)
    p = Portfolio(
        name=defn.name,
        kind=defn.kind,
        initial_cash=to_text(cfg.initial_capital),
        started_at=datetime(defn.start.year, defn.start.month, defn.start.day),
        peak_equity=to_text(cfg.initial_capital),
        config_json=defn.to_json(),
        created_at=now,
    )
    session.add(p)
    session.flush()
    assert p.id is not None
    by_id = {s.sleeve_id: s for s in defn.sleeves}
    for sc in cfg.sleeves:
        spec = by_id[sc.sleeve_id].strategy
        cfg_id: int | None = None
        market = "cash"
        if spec is not None:
            market = spec.asset_class
            cfg_id = repo.get_or_create_strategy_config(
                session,
                name=spec.id,
                market=market,
                spec_json=dump_strategy(spec, "json"),
                now=now,
            ).id
        session.add(
            Sleeve(
                portfolio_id=p.id,
                key=sc.sleeve_id,
                strategy_config_id=cfg_id,
                target_weight_bp=int(sc.target_weight * 10000),
                market=market,
                cash=to_text(deposits[sc.sleeve_id]),
                created_at=now,
            )
        )
    session.add(KillSwitchState(portfolio_id=p.id, peak_equity=to_text(cfg.initial_capital)))
    repo.get_runner_state(session, p.id)
    session.flush()
    return p


def restore_context(session: Session, portfolio: Portfolio) -> PortfolioContext:
    """Rebuild the engine from the latest checkpoint, then ``verify()`` it (SPEC-004 step 0).

    A portfolio without any checkpoint (and without a processed bar) gets a fresh engine.
    Raises ``StateCorruptionError`` when the checkpoint, the ledger rows and the runner
    marker disagree, or ``verify()`` fails: the portfolio then never trades.
    """
    assert portfolio.id is not None
    pid = portfolio.id
    defn = PortfolioDef.from_json(portfolio.config_json)
    cfg = defn.engine_config()
    sleeve_ids = repo.sleeve_ids_by_key(session, pid)
    rs = repo.get_runner_state(session, pid)
    cp = repo.latest_checkpoint(session, pid)
    strategy_ids = defn.strategy_sleeve_ids()
    if cp is None:
        if rs.last_processed_bar_ts is not None or repo.ledger_count(session, pid) != 0:
            raise StateCorruptionError("runner state/ledger exist but no checkpoint was found")
        engine = PortfolioEngine(cfg)
        return PortfolioContext(pid, defn, engine, TargetBook(strategy_ids), sleeve_ids)
    from tradebot.portfolio.state import EngineState

    state = EngineState.from_json(cp.state_json)
    try:
        engine = PortfolioEngine(cfg, state=state)
    except ValueError as exc:
        if isinstance(exc, StateCorruptionError):
            raise
        raise StateCorruptionError(
            f"checkpoint does not match the portfolio config: {exc}"
        ) from exc
    engine.verify()
    if rs.last_processed_bar_ts != cp.bar_ts:
        raise StateCorruptionError(
            f"runner marker {rs.last_processed_bar_ts} != latest checkpoint {cp.bar_ts}"
        )
    n_db = repo.ledger_count(session, pid)
    if n_db != len(engine.ledger) or repo.ledger_digest(session, pid) != repo.events_digest(
        engine.ledger
    ):
        raise StateCorruptionError("ledger_event rows differ from the checkpoint ledger")
    book = TargetBook.from_state(strategy_ids, json.loads(cp.target_book_json))
    return PortfolioContext(
        pid,
        defn,
        engine,
        book,
        sleeve_ids,
        n_ledger=len(engine.ledger),
        last_bar=cp.bar_ts,
        orders={o.order_id: o for o in engine.orders()},
    )


def reset_kill_switch(
    db: Engine, portfolio_name: str, *, operator: str, note: str, now: datetime
) -> date:
    """Operator reset (CLI only; the caller must hold the runner lock).

    Restores the engine, calls ``reset_kill_switch``, appends the ledger event, mirrors the
    kill-switch row and *replaces* the latest checkpoint (same bar) in one transaction, so
    the next restore continues from the reset state. Returns the bar date it was booked on.
    """
    now = repo.naive_utc(now)
    with session_scope(db) as s:
        p = repo.get_portfolio(s, portfolio_name)
        if p is None or p.id is None:
            raise RunnerError(f"unknown portfolio {portfolio_name!r}")
        ctx = restore_context(s, p)
        asof = ctx.engine.last_processed_asof
        if asof is None:
            raise RunnerError("portfolio has not processed any bar")
        ctx.engine.reset_kill_switch(operator, note, asof)
        new_events = list(ctx.engine.ledger)[ctx.n_ledger :]
        repo.append_ledger_events(s, p.id, new_events, ctx.sleeve_ids)
        snap = ctx.engine.snapshot()
        bar_ts = datetime(asof.year, asof.month, asof.day)
        repo.mirror_kill_switch(
            s,
            p.id,
            tripped=snap.killed,
            peak_equity=snap.peak_equity,
            bar_ts=bar_ts,
            drawdown=snap.drawdown,
            events=[e.type.value for e in new_events],
            now=now,
        )
        repo.replace_checkpoint(
            s,
            p.id,
            bar_ts,
            ctx.engine.export_state().to_json(),
            json.dumps(ctx.book.state(), sort_keys=True),
            len(ctx.engine.ledger),
            now,
        )
        p.peak_equity = to_text(snap.peak_equity)
        if p.status == "halted":
            p.status = "active"
        s.add(p)
    return asof


class ResetFromResult(NamedTuple):
    """What :func:`reset_from` did."""

    skipped_after: date
    cancelled_orders: list[str]


def reset_from(
    db: Engine, portfolio_name: str, resume: date, *, operator: str, note: str, now: datetime
) -> ResetFromResult:
    """Accept a gap: skip the missed bars and resume at ``resume`` (human override of the cap).

    The caller must hold the runner lock. One transaction: cancel pending orders (placed before
    the gap, they must not fill at a stale signal), clear the TargetBook (strategies re-decide on
    the next bar), write a checkpoint labelled ``resume - 1 day`` with the otherwise unchanged
    engine state, advance ``runner_state`` to it and log the override in ``run_log``.
    Refuses a ``resume`` in the future (per ``now``) or not after the last processed bar.
    """
    from tradebot.sim.orders import OrderStatus

    now_utc = repo.naive_utc(now)
    if resume > now_utc.date():
        raise RunnerError(f"DATE {resume} is in the future (today is {now_utc.date()})")
    label = datetime(resume.year, resume.month, resume.day) - timedelta(days=1)
    with session_scope(db) as s:
        p = repo.get_portfolio(s, portfolio_name)
        if p is None or p.id is None:
            raise RunnerError(f"unknown portfolio {portfolio_name!r}")
        rs = repo.get_runner_state(s, p.id)
        if rs.last_processed_bar_ts is None:
            raise RunnerError("portfolio has not processed any bar; recreate it with --start DATE")
        if label <= rs.last_processed_bar_ts:
            raise RunnerError(
                f"DATE must be after the last processed bar {rs.last_processed_bar_ts.date()}"
            )
        ctx = restore_context(s, p)
        asof = ctx.engine.last_processed_asof
        assert asof is not None
        cancelled = [
            ctx.engine.cancel_order(o.order_id)
            for o in ctx.engine.orders()
            if o.status is OrderStatus.PENDING
        ]
        new_events = list(ctx.engine.ledger)[ctx.n_ledger :]
        repo.append_ledger_events(s, p.id, new_events, ctx.sleeve_ids)
        repo.upsert_orders(s, p.id, cancelled, ctx.sleeve_ids, now_utc)
        repo.write_checkpoint(
            s,
            p.id,
            label,
            ctx.engine.export_state().to_json(),
            json.dumps(TargetBook(ctx.defn.strategy_sleeve_ids()).state(), sort_keys=True),
            len(ctx.engine.ledger),
            now_utc,
        )
        repo.insert_run_log(
            s,
            p.id,
            label,
            started_at=now_utc,
            finished_at=now_utc,
            n_orders_created=0,
            n_fills=0,
            detail={
                "reset_from": resume.isoformat(),
                "skipped_after": asof.isoformat(),
                "cancelled_orders": [o.order_id for o in cancelled],
                "target_book_cleared": True,
                "operator": operator,
                "note": note,
            },
            outcome="reset",
        )
        repo.record_status(s, p.id, status="ok", error=None, now=now_utc)
        rs = repo.get_runner_state(s, p.id)
        rs.last_processed_bar_ts = label
        s.add(rs)
    return ResetFromResult(asof, [o.order_id for o in cancelled])
