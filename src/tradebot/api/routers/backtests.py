"""Backtest endpoints: POST queues a job, GETs read the persisted result."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import col, select

from tradebot.api.deps import Db
from tradebot.api.jobs import ALLOWED_OVERRIDES
from tradebot.api.schemas import (
    BacktestRunDetail,
    BacktestRunIn,
    BacktestRunSummary,
    EquityPoint,
    TradeOut,
)
from tradebot.db import repo
from tradebot.db.models import BacktestEquityPoint, BacktestRun, BacktestTrade, StrategyConfig
from tradebot.runner.clock import bar_complete_time

router = APIRouter(prefix="/backtests", tags=["backtests"])


def _d(x: float) -> Decimal:
    return Decimal(repr(x))


def check_overrides(v: dict[str, Any]) -> None:
    """Raise ``ValueError`` unless ``initial_capital`` is a positive whole-cents amount and
    ``rf_annual`` a finite number in [0, 0.2]. Done by hand (not a pydantic validator) so an
    ``inf`` input is never echoed into the error body."""
    if "initial_capital" in v:
        raw = v["initial_capital"]
        try:
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise InvalidOperation
            cap = Decimal(str(raw))
        except InvalidOperation:
            raise ValueError("initial_capital must be a decimal number") from None
        exp = cap.as_tuple().exponent
        if not cap.is_finite() or cap <= 0 or (isinstance(exp, int) and exp < -2):
            raise ValueError("initial_capital must be a positive amount in whole cents")
    if "rf_annual" in v:
        rf = v["rf_annual"]
        if isinstance(rf, bool) or not isinstance(rf, (int, float)):
            raise ValueError("rf_annual must be a number")
        if not math.isfinite(rf) or not 0.0 <= rf <= 0.2:
            raise ValueError("rf_annual must be a finite number in [0, 0.2]")


def _last_closed_bar(market: str, now: datetime) -> date | None:
    """Latest bar date of ``market`` that is complete at ``now`` (None if none in 10 days)."""
    now = now.astimezone(UTC)
    for back in range(11):
        d = now.date() - timedelta(days=back)
        done = bar_complete_time("crypto" if market == "crypto" else "equity", d)
        if done is not None and done <= now:
            return d
    return None


def _pct(x: float | None) -> float | None:
    return None if x is None else x * 100.0


def _summary(r: BacktestRun) -> BacktestRunSummary:
    assert r.id is not None
    return BacktestRunSummary(
        id=r.id,
        strategy_id=r.strategy_config_id,
        status=r.status,
        started_at=r.started_at,
        finished_at=r.finished_at,
        total_return_pct=_pct(r.total_return),
        sharpe=r.sharpe,
        sortino=r.sortino,
        max_drawdown_pct=_pct(r.max_drawdown),
        n_trades=r.n_trades,
        benchmark_symbol=r.benchmark_symbol or "",
    )


@router.post("", status_code=202, response_model=BacktestRunSummary)
def create_backtest(db: Db, body: BacktestRunIn, request: Request) -> BacktestRunSummary:
    """Insert a ``queued`` row and hand it to the single-worker pool."""
    strategy = db.get(StrategyConfig, body.strategy_id)
    if strategy is None:
        raise HTTPException(404, f"strategy {body.strategy_id} not found")
    bad = sorted(set(body.overrides) - ALLOWED_OVERRIDES)
    if bad:
        raise HTTPException(
            422, f"unsupported overrides {bad}; allowed {sorted(ALLOWED_OVERRIDES)}"
        )
    try:
        check_overrides(body.overrides)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.end is not None and body.end <= body.start:
        raise HTTPException(422, "end must be after start")
    now = request.app.state.clock.now()
    last_closed = _last_closed_bar(strategy.market, now)
    if body.end is not None and (last_closed is None or body.end > last_closed):
        raise HTTPException(422, f"end {body.end} is after the last closed bar {last_closed}")
    row = BacktestRun(
        strategy_config_id=body.strategy_id,
        status="queued",
        params_json=json.dumps(body.model_dump(mode="json"), sort_keys=True),
        created_at=repo.naive_utc(now),
    )
    db.add(row)
    db.commit()  # the worker must see the row before it starts
    db.refresh(row)
    assert row.id is not None
    request.app.state.jobs.submit(row.id)
    return _summary(row)


@router.get("", response_model=list[BacktestRunSummary])
def list_backtests(db: Db, strategy_id: int | None = None) -> list[BacktestRunSummary]:
    """Runs, newest first."""
    stmt = select(BacktestRun).order_by(col(BacktestRun.id).desc())
    if strategy_id is not None:
        stmt = stmt.where(BacktestRun.strategy_config_id == strategy_id)
    return [_summary(r) for r in db.exec(stmt)]


@router.get("/{run_id}", response_model=BacktestRunDetail)
def get_backtest(db: Db, run_id: int) -> BacktestRunDetail:
    """Summary plus params, the stored result, equity points and closed trades."""
    r = db.get(BacktestRun, run_id)
    if r is None:
        raise HTTPException(404, f"backtest {run_id} not found")
    pts = db.exec(
        select(BacktestEquityPoint)
        .where(BacktestEquityPoint.run_id == run_id)
        .order_by(col(BacktestEquityPoint.ts))
    )
    trades = db.exec(
        select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(col(BacktestTrade.id))
    )
    return BacktestRunDetail(
        **_summary(r).model_dump(),
        params=json.loads(r.params_json),
        result=json.loads(r.result_json) if r.result_json else None,
        equity=[
            EquityPoint(
                ts=p.ts,
                equity=_d(p.equity),
                drawdown_pct=(p.drawdown or 0.0) * 100.0,
                benchmark_equity=_d(p.benchmark_equity) if p.benchmark_equity is not None else None,
                benchmark_missing=p.benchmark_equity is None,
            )
            for p in pts
        ],
        trades=[
            TradeOut(
                ts=t.exit_ts or t.entry_ts,
                sleeve_key=t.sleeve_key or "",
                symbol=t.symbol,
                side=t.side,
                qty=_d(t.qty),
                price=_d(t.exit_px if t.exit_px is not None else t.entry_px),
                fee=_d(t.fees or 0.0),
                slippage=None,
                reason="closed_trade",
            )
            for t in trades
        ],
        error=r.error,
    )
