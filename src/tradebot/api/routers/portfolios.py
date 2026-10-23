"""Portfolio read endpoints (mapping only; the API never touches broker/sim code)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session, col, func, select

from tradebot.api.deps import Db
from tradebot.api.schemas import (
    DailyOut,
    EquityPoint,
    KillSwitchOut,
    OrderOut,
    Page,
    PortfolioDetail,
    PortfolioSummary,
    PositionOut,
    RoundTripOut,
    RoundTripPage,
    SleeveBreakdown,
    TradeOut,
)
from tradebot.backtest.trades import ClosedTrade, closed_trades
from tradebot.data.universe import SP500_LABEL, is_sp500_spec
from tradebot.db import repo
from tradebot.db.models import (
    EquitySnapshot,
    LedgerEventRow,
    OrderRow,
    Portfolio,
    StrategyConfig,
)
from tradebot.runner.clock import has_bar, pause_end_bar, pause_end_bar_15m
from tradebot.runner.state import PortfolioDef
from tradebot.sim.orders import Order

router = APIRouter(prefix="/portfolios", tags=["portfolios"])

ZERO = Decimal(0)
TOTAL = repo.TOTAL_SLEEVE_ID


def _pct(x: Decimal | float) -> float:
    return float(x) * 100.0


def require_portfolio(db: Session, portfolio_id: int) -> Portfolio:
    """The portfolio row or a 404."""
    p = repo.get_portfolio(db, portfolio_id)
    if p is None or p.id is None:
        raise HTTPException(404, f"portfolio {portfolio_id} not found")
    return p


def _total_rows(db: Session, pid: int) -> list[EquitySnapshot]:
    return list(
        db.exec(
            select(EquitySnapshot)
            .where(EquitySnapshot.portfolio_id == pid, EquitySnapshot.sleeve_id == TOTAL)
            .order_by(col(EquitySnapshot.ts))
        )
    )


def kill_switch_out(db: Session, p: Portfolio) -> KillSwitchOut:
    """Read-only kill-switch view (threshold from the stored portfolio config)."""
    assert p.id is not None
    ks = repo.get_kill_switch(db, p.id)
    risk = json.loads(p.config_json)["engine"]["risk"]
    threshold = float(risk["kill_switch_drawdown"])
    ends: date | datetime | None = None
    if ks and ks.tripped and ks.tripped_at_bar:
        defn = PortfolioDef.from_json(p.config_json)
        if defn.timeframe() == "15m":
            ends = pause_end_bar_15m(ks.tripped_at_bar, int(risk.get("kill_switch_pause_bars", 20)))
        else:
            ends = pause_end_bar(
                defn.markets(),
                ks.tripped_at_bar.date(),
                int(risk.get("kill_switch_pause_bars", 20)),
            )
    return KillSwitchOut(
        pause_ends_after_bar=ends,
        tripped=bool(ks and ks.tripped),
        tripped_at_bar=ks.tripped_at_bar if ks else None,
        drawdown_pct=_pct(Decimal(ks.trigger_drawdown)) if ks and ks.trigger_drawdown else None,
        threshold_pct=threshold * 100.0,
        acknowledged_at=ks.acknowledged_at if ks else None,
        note=ks.ack_note if ks else None,
    )


def summarize(db: Session, p: Portfolio) -> PortfolioSummary:
    """``PortfolioSummary`` from the latest total snapshot (initial cash if none yet)."""
    assert p.id is not None
    rows = _total_rows(db, p.id)
    initial = Decimal(p.initial_cash)
    equity = Decimal(rows[-1].equity) if rows else initial
    max_dd = max((Decimal(r.drawdown) for r in rows if r.drawdown is not None), default=ZERO)
    ks = repo.get_kill_switch(db, p.id)
    state = repo.get_runner_state(db, p.id)
    return PortfolioSummary(
        id=p.id,
        name=p.name,
        kind=p.kind,  # type: ignore[arg-type]
        status=p.status,
        equity=equity,
        initial_cash=initial,
        total_return_pct=_pct(equity / initial - 1) if initial else 0.0,
        max_drawdown_pct=_pct(max_dd),
        kill_switch_tripped=bool(ks and ks.tripped),
        last_processed_bar=state.last_processed_bar_ts,
        started_at=p.started_at,
    )


@router.get("", response_model=list[PortfolioSummary])
def list_portfolios(
    db: Db,
) -> list[PortfolioSummary]:
    """All non-archived portfolios."""
    return [summarize(db, p) for p in repo.list_portfolios(db)]


@router.get("/{portfolio_id}", response_model=PortfolioDetail)
def get_portfolio(db: Db, portfolio_id: int) -> PortfolioDetail:
    """Summary plus sleeve breakdown, kill switch and the benchmark portfolios."""
    p = require_portfolio(db, portfolio_id)
    assert p.id is not None
    summary = summarize(db, p)
    positions = repo.latest_positions(db, p.id)
    initial = Decimal(p.initial_cash)
    sleeves: list[SleeveBreakdown] = []
    for sl in repo.list_sleeves(db, p.id):
        last = db.exec(
            select(EquitySnapshot)
            .where(EquitySnapshot.portfolio_id == p.id, EquitySnapshot.sleeve_id == sl.id)
            .order_by(col(EquitySnapshot.ts).desc())
        ).first()
        start_eq = initial * sl.target_weight_bp / 10000
        eq = Decimal(last.equity) if last else start_eq
        name = db.get(StrategyConfig, sl.strategy_config_id) if sl.strategy_config_id else None
        sleeves.append(
            SleeveBreakdown(
                key=sl.key,
                strategy_name=name.name if name else None,
                target_weight_pct=sl.target_weight_bp / 100.0,
                actual_weight_pct=_pct(eq / summary.equity) if summary.equity else 0.0,
                equity=eq,
                return_pct=_pct(eq / start_eq - 1) if start_eq else 0.0,
                n_positions=sum(1 for x in positions if x.sleeve_id == sl.id),
            )
        )
    bench_name = json.loads(p.config_json).get("benchmark")
    benchmarks = [
        summarize(db, b)
        for b in repo.list_portfolios(db)
        if b.kind == "benchmark"
        and p.kind == "fund"
        and (bench_name is None or b.name == bench_name)
    ]
    caveats = (
        [SP500_LABEL]
        if any(
            sl.strategy is not None and is_sp500_spec(sl.strategy.id)
            for sl in PortfolioDef.from_json(p.config_json).sleeves
        )
        else []
    )
    cash, unsettled, as_of = _cash_figures(db, p)
    return PortfolioDetail(
        caveats=caveats,
        **summary.model_dump(),
        cash=cash,
        unsettled_cash=unsettled,
        settled_cash=cash - unsettled,
        as_of=as_of,
        sleeves=sleeves,
        kill_switch=kill_switch_out(db, p),
        benchmarks=benchmarks,
    )


def _cash_figures(db: Session, p: Portfolio) -> tuple[Decimal, Decimal, datetime | None]:
    """``(cash, unsettled, bar ts)`` at the latest total snapshot.

    ``unsettled`` is stored per snapshot; a row written before that column existed (NULL) of a
    T+1 portfolio is recomputed from the ledger by restoring the engine. Before the first bar:
    the initial cash, nothing unsettled.
    """
    assert p.id is not None
    rows = _total_rows(db, p.id)
    if not rows:
        return Decimal(p.initial_cash), ZERO, None
    last = rows[-1]
    cash = Decimal(last.cash)
    if last.unsettled is not None:
        return cash, Decimal(last.unsettled), last.ts
    if PortfolioDef.from_json(p.config_json).engine_config().settlement == "none":
        return cash, ZERO, last.ts
    from tradebot.runner.bootstrap import restore_context  # lazy: heavy, rarely needed

    try:
        snap = restore_context(db, p).engine.snapshot()
    except Exception:  # noqa: BLE001 - a display figure must not break the page
        return cash, ZERO, last.ts
    return cash, snap.unsettled, last.ts


def _as_datetime(asof: date | datetime) -> datetime:
    """A ledger ``asof`` as a datetime: the bar-open timestamp (15m) or midnight of the date."""
    return asof if isinstance(asof, datetime) else datetime(asof.year, asof.month, asof.day)


def _bound(text: str) -> tuple[datetime, bool]:
    """Parse a ``from``/``to`` value into a naive UTC datetime; True when it was a bare date."""
    try:
        if len(text) == 10:
            d = date.fromisoformat(text)
            return datetime(d.year, d.month, d.day), True
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(422, f"bad date/time {text!r}") from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt, False


@router.get("/{portfolio_id}/equity", response_model=list[EquityPoint])
def get_equity(
    db: Db,
    portfolio_id: int,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    sleeve: str | None = None,
) -> list[EquityPoint]:
    """Equity series (total, or one sleeve) with write-time drawdown and benchmark equity.

    ``from``/``to`` take a date (``to`` is inclusive of that whole day) or, for 15m portfolios, a
    UTC timestamp (``2024-01-08T14:30:00``; both ends inclusive). Points carry bar-open ``ts``.
    """
    p = require_portfolio(db, portfolio_id)
    assert p.id is not None
    sleeve_id = TOTAL
    if sleeve is not None:
        sleeve_id = repo.sleeve_ids_by_key(db, p.id).get(sleeve, -1)
        if sleeve_id < 0:
            raise HTTPException(404, f"sleeve {sleeve!r} not found")
    stmt = select(EquitySnapshot).where(
        EquitySnapshot.portfolio_id == p.id, EquitySnapshot.sleeve_id == sleeve_id
    )
    if from_ is not None:
        lo, _ = _bound(from_)
        stmt = stmt.where(EquitySnapshot.ts >= lo)
    if to is not None:
        hi, whole_day = _bound(to)
        stmt = stmt.where(
            EquitySnapshot.ts < hi + timedelta(days=1) if whole_day else EquitySnapshot.ts <= hi
        )
    bench_name = json.loads(p.config_json).get("benchmark")
    bench_markets: list[str] | None = None  # None: benchmark portfolio absent -> always expected
    if bench_name is not None:
        bp = repo.get_portfolio(db, bench_name)
        if bp is not None:
            bench_markets = PortfolioDef.from_json(bp.config_json).markets()

    def missing(r: EquitySnapshot) -> bool:
        if bench_name is None or sleeve_id != TOTAL or r.benchmark_equity is not None:
            return False
        return bench_markets is None or any(has_bar(m, r.ts.date()) for m in bench_markets)

    return [
        EquityPoint(
            ts=r.ts,
            equity=Decimal(r.equity),
            drawdown_pct=_pct(Decimal(r.drawdown)) if r.drawdown is not None else 0.0,
            benchmark_equity=Decimal(r.benchmark_equity) if r.benchmark_equity else None,
            benchmark_missing=missing(r),
        )
        for r in db.exec(stmt.order_by(col(EquitySnapshot.ts)))
    ]


@router.get("/{portfolio_id}/positions", response_model=list[PositionOut])
def get_positions(db: Db, portfolio_id: int) -> list[PositionOut]:
    """Open positions at the latest snapshot."""
    p = require_portfolio(db, portfolio_id)
    assert p.id is not None
    keys = {v: k for k, v in repo.sleeve_ids_by_key(db, p.id).items()}
    rows = repo.latest_positions(db, p.id)
    total = _total_rows(db, p.id)
    equity = Decimal(total[-1].equity) if total else Decimal(p.initial_cash)
    out: list[PositionOut] = []
    for r in rows:
        qty, mv = Decimal(r.qty), Decimal(r.market_value)
        q = Decimal("0.00000001")
        out.append(
            PositionOut(
                sleeve_key=keys.get(r.sleeve_id, str(r.sleeve_id)),
                symbol=r.symbol,
                qty=qty,
                avg_cost=(Decimal(r.cost_basis) / qty).quantize(q) if qty else ZERO,
                mark_price=(mv / qty).quantize(q) if qty else ZERO,
                market_value=mv,
                unrealized_pnl=Decimal(r.unrealized),
                weight_pct=_pct(mv / equity) if equity else 0.0,
            )
        )
    return out


@router.get("/{portfolio_id}/trades", response_model=Page[TradeOut])
def get_trades(
    db: Db,
    portfolio_id: int,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    symbol: str | None = None,
    sleeve: str | None = None,
) -> Page[TradeOut]:
    """Fills from the ledger, newest first, with the order's fee and reason."""
    p = require_portfolio(db, portfolio_id)
    cond = [LedgerEventRow.portfolio_id == p.id, LedgerEventRow.type == "FILL"]
    if symbol:
        cond.append(LedgerEventRow.symbol == symbol)
    if sleeve:
        cond.append(LedgerEventRow.sleeve_key == sleeve)
    total = db.exec(select(func.count()).select_from(LedgerEventRow).where(*cond)).one()
    rows = db.exec(
        select(LedgerEventRow)
        .where(*cond)
        .order_by(col(LedgerEventRow.seq).desc())
        .offset(offset)
        .limit(limit)
    ).all()
    orders = {
        o.order_id: o
        for o in db.exec(
            select(OrderRow).where(
                OrderRow.portfolio_id == p.id,
                col(OrderRow.order_id).in_([r.order_id for r in rows if r.order_id]),
            )
        )
    }
    items = []
    for r in rows:
        o = orders.get(r.order_id or "")
        items.append(
            TradeOut(
                ts=_as_datetime(r.asof),
                sleeve_key=r.sleeve_key,
                symbol=r.symbol or "",
                side=r.side or "",
                qty=Decimal(r.qty or "0"),
                price=Decimal(r.price or "0"),
                fee=Decimal(o.fee) if o and o.fee else ZERO,
                slippage=Decimal(r.slippage_cost or "0"),
                reason=(o.reason or o.origin) if o else "",
            )
        )
    return Page[TradeOut](items=items, total=int(total), limit=limit, offset=offset)


@router.get("/{portfolio_id}/orders", response_model=list[OrderOut])
def get_orders(db: Db, portfolio_id: int, status: str | None = None) -> list[OrderOut]:
    """Orders (all, or filtered by engine status such as ``PENDING``)."""
    p = require_portfolio(db, portfolio_id)
    assert p.id is not None
    keys = {v: k for k, v in repo.sleeve_ids_by_key(db, p.id).items()}
    stmt = select(OrderRow).where(OrderRow.portfolio_id == p.id)
    if status:
        stmt = stmt.where(OrderRow.status == status)
    return [
        OrderOut(
            order_id=o.order_id,
            sleeve_key=keys.get(o.sleeve_id, str(o.sleeve_id)),
            symbol=o.symbol,
            side=o.side,
            origin=o.origin,
            status=o.status,
            qty_requested=Decimal(o.qty_requested),
            qty_filled=Decimal(o.qty_filled),
            created_asof=o.created_asof,
            fill_asof=o.fill_asof,
            fill_price=Decimal(o.fill_price) if o.fill_price else None,
            fee=Decimal(o.fee) if o.fee else None,
            reason=o.reason,
        )
        for o in db.exec(stmt.order_by(col(OrderRow.id)))
    ]


def _closed_trades(db: Session, pid: int) -> list[ClosedTrade]:
    """Round trips of the whole persisted ledger (the same pairing the backtester uses)."""
    orders = {
        o.order_id: Order.model_validate_json(o.order_json)
        for o in db.exec(select(OrderRow).where(OrderRow.portfolio_id == pid))
    }
    return closed_trades(repo.ledger_events(db, pid), orders)


@router.get("/{portfolio_id}/roundtrips", response_model=RoundTripPage)
def get_roundtrips(
    db: Db,
    portfolio_id: int,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    sleeve: str | None = None,
) -> RoundTripPage:
    """Completed round trips (entry to exit), newest exit first, with win rate and total P&L.

    ``from``/``to`` filter on the exit time (a date, with ``to`` covering the whole day, or a UTC
    timestamp). ``exit_reason`` is ``signal``, ``stop``, ``flatten`` (end-of-day exit) or
    ``flatten_no_bar`` (end-of-day exit of a symbol with no price bar; sold with an extra haircut).
    """
    p = require_portfolio(db, portfolio_id)
    assert p.id is not None
    lo = _bound(from_)[0] if from_ is not None else None
    hi: datetime | None = None
    whole_day = False
    if to is not None:
        hi, whole_day = _bound(to)
    rows: list[tuple[datetime, datetime, ClosedTrade]] = []
    for t in _closed_trades(db, p.id):
        entry, exit_ = _as_datetime(t.entry_asof), _as_datetime(t.exit_asof)
        exit_naive = exit_.astimezone(UTC).replace(tzinfo=None) if exit_.tzinfo else exit_
        if sleeve is not None and t.sleeve_id != sleeve:
            continue
        if lo is not None and exit_naive < lo:
            continue
        if hi is not None and (
            exit_naive >= hi + timedelta(days=1) if whole_day else exit_naive > hi
        ):
            continue
        rows.append((entry, exit_, t))
    rows.sort(key=lambda r: (r[1], r[2].sleeve_id, r[2].symbol), reverse=True)
    wins = sum(1 for _, _, t in rows if t.net_pnl > 0)
    total_pnl = sum((t.net_pnl for _, _, t in rows), ZERO)
    items = [
        RoundTripOut(
            sleeve_key=t.sleeve_id,
            symbol=t.symbol,
            entry_ts=entry,
            exit_ts=exit_,
            qty=t.qty,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            fees=t.fees,
            pnl=t.net_pnl,
            pnl_pct=_pct(t.net_pnl / (t.qty * t.entry_price)) if t.qty * t.entry_price else 0.0,
            exit_reason=t.exit_reason,
        )
        for entry, exit_, t in rows[:limit]
    ]
    return RoundTripPage(
        items=items,
        total=len(rows),
        limit=limit,
        win_rate=wins / len(rows) if rows else None,
        total_pnl=total_pnl,
    )


_NY = ZoneInfo("America/New_York")


@router.get("/{portfolio_id}/daily", response_model=list[DailyOut])
def get_daily(
    db: Db,
    portfolio_id: int,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
) -> list[DailyOut]:
    """Per-session P&L (oldest first): the portfolio's and its benchmark's, in percent.

    A session's start equity is the previous session's end equity (the initial cash for the
    first one). Sessions are New York dates for 15m portfolios and bar dates for daily ones.
    """
    p = require_portfolio(db, portfolio_id)
    assert p.id is not None
    intraday = PortfolioDef.from_json(p.config_json).timeframe() == "15m"

    def session_of(ts: datetime) -> date:
        if not intraday:
            return ts.date()
        return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).astimezone(_NY).date()

    last_eq: dict[date, Decimal] = {}
    last_bench: dict[date, Decimal | None] = {}
    for r in _total_rows(db, p.id):  # ascending ts, so the last write per session wins
        d = session_of(r.ts)
        last_eq[d] = Decimal(r.equity)
        last_bench[d] = Decimal(r.benchmark_equity) if r.benchmark_equity else None
    trips: dict[date, int] = {}
    for t in _closed_trades(db, p.id):
        d = session_of(_as_datetime(t.exit_asof))
        trips[d] = trips.get(d, 0) + 1
    initial = Decimal(p.initial_cash)
    prev_eq, prev_bench = initial, initial  # a benchmark starts with the same capital
    lo = _bound(from_)[0].date() if from_ is not None else None
    hi = _bound(to)[0].date() if to is not None else None
    out: list[DailyOut] = []
    for d in sorted(last_eq):
        end, bench = last_eq[d], last_bench[d]
        if (lo is None or d >= lo) and (hi is None or d <= hi):
            out.append(
                DailyOut(
                    date=d,
                    start_equity=prev_eq,
                    end_equity=end,
                    pnl=end - prev_eq,
                    pnl_pct=_pct(end / prev_eq - 1) if prev_eq else 0.0,
                    n_roundtrips=trips.get(d, 0),
                    benchmark_pnl_pct=(
                        _pct(bench / prev_bench - 1) if bench is not None and prev_bench else None
                    ),
                )
            )
        prev_eq = end
        if bench is not None:
            prev_bench = bench
    return out
