"""Portfolio read endpoints (mapping only; the API never touches broker/sim code)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session, col, func, select

from tradebot.api.deps import Db
from tradebot.api.schemas import (
    EquityPoint,
    KillSwitchOut,
    OrderOut,
    Page,
    PortfolioDetail,
    PortfolioSummary,
    PositionOut,
    SleeveBreakdown,
    TradeOut,
)
from tradebot.db import repo
from tradebot.db.models import (
    EquitySnapshot,
    LedgerEventRow,
    OrderRow,
    Portfolio,
    StrategyConfig,
)
from tradebot.runner.clock import has_bar
from tradebot.runner.state import PortfolioDef

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
    threshold = float(json.loads(p.config_json)["engine"]["risk"]["kill_switch_drawdown"])
    return KillSwitchOut(
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
    return PortfolioDetail(
        **summary.model_dump(),
        sleeves=sleeves,
        kill_switch=kill_switch_out(db, p),
        benchmarks=benchmarks,
    )


@router.get("/{portfolio_id}/equity", response_model=list[EquityPoint])
def get_equity(
    db: Db,
    portfolio_id: int,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    sleeve: str | None = None,
) -> list[EquityPoint]:
    """Equity series (total, or one sleeve) with write-time drawdown and benchmark equity."""
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
        stmt = stmt.where(EquitySnapshot.ts >= datetime(from_.year, from_.month, from_.day))
    if to is not None:
        stmt = stmt.where(
            EquitySnapshot.ts < datetime(to.year, to.month, to.day) + timedelta(days=1)
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
                ts=datetime(r.asof.year, r.asof.month, r.asof.day),
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
