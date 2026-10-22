"""Strategy config endpoints and NexusTrade-style performance cards."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from sqlmodel import col, select

from tradebot.api.deps import Db
from tradebot.api.schemas import StrategyCard, StrategyConfigIn, StrategyConfigOut
from tradebot.db import repo
from tradebot.db.models import BacktestRun, StrategyConfig
from tradebot.strategy.io import dump_strategy, load_strategy

router = APIRouter(prefix="/strategies", tags=["strategies"])


def _out(row: StrategyConfig) -> StrategyConfigOut:
    assert row.id is not None
    return StrategyConfigOut(
        id=row.id,
        name=row.name,
        market=row.market,
        schema_=json.loads(row.spec_json),
        version=row.version,
        created_at=row.created_at,
    )


@router.get("", response_model=list[StrategyConfigOut])
def list_strategies(
    db: Db,
) -> list[StrategyConfigOut]:
    """Every stored strategy version, oldest first."""
    return [_out(r) for r in db.exec(select(StrategyConfig).order_by(col(StrategyConfig.id)))]


@router.post("", response_model=StrategyConfigOut)
def create_strategy(db: Db, body: StrategyConfigIn, request: Request) -> StrategyConfigOut:
    """Validate with the SPEC-002 schema and store (new version when the name exists)."""
    try:
        spec = load_strategy(json.dumps(body.schema_))
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, f"invalid strategy: {exc}") from exc
    if spec.asset_class != body.market:
        raise HTTPException(
            422, f"market {body.market!r} != strategy asset_class {spec.asset_class!r}"
        )
    now = repo.naive_utc(request.app.state.clock.now())
    row = repo.get_or_create_strategy_config(
        db, name=body.name, market=body.market, spec_json=dump_strategy(spec, "json"), now=now
    )
    return _out(row)


@router.get("/cards", response_model=list[StrategyCard])
def strategy_cards(
    db: Db,
) -> list[StrategyCard]:
    """Stored metrics of each strategy's latest finished single backtest (never recomputed)."""
    cards = []
    for s in db.exec(select(StrategyConfig).order_by(col(StrategyConfig.id))):
        assert s.id is not None
        run = db.exec(
            select(BacktestRun)
            .where(BacktestRun.strategy_config_id == s.id, BacktestRun.status == "done")
            .order_by(col(BacktestRun.id).desc())
        ).first()
        bench = "BTC/USD" if s.market == "crypto" else "SPY"
        bench_ret: float | None = None
        if run is not None and run.result_json:
            bm: dict[str, Any] = json.loads(run.result_json).get("benchmark_metrics", {})
            tr = bm.get(run.benchmark_symbol or bench, {}).get("total_return")
            bench_ret = None if tr is None else float(tr) * 100.0
        pct = lambda v: None if v is None else float(v) * 100.0  # noqa: E731
        cards.append(
            StrategyCard(
                strategy_id=s.id,
                name=s.name,
                version=s.version,
                market=s.market,
                return_1y_pct=pct(run.total_return) if run else None,
                max_drawdown_pct=pct(run.max_drawdown) if run else None,
                sortino=run.sortino if run else None,
                benchmark_symbol=(run.benchmark_symbol if run else None) or bench,
                benchmark_return_1y_pct=bench_ret,
                source="backtest",
                as_of=run.finished_at if run else None,
            )
        )
    return cards


@router.get("/{strategy_id}", response_model=StrategyConfigOut)
def get_strategy(db: Db, strategy_id: int) -> StrategyConfigOut:
    """One stored strategy."""
    row = db.get(StrategyConfig, strategy_id)
    if row is None:
        raise HTTPException(404, f"strategy {strategy_id} not found")
    return _out(row)
