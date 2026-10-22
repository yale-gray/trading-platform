"""Single-worker backtest job runner (SPEC-004 T8).

One daemon-free ``ThreadPoolExecutor(max_workers=1)`` runs queued ``backtest_run`` rows one
at a time. Every status change is its own short transaction, so a killed process leaves at
most a ``running`` row, which :func:`fail_stale_runs` marks ``failed`` on the next startup.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine
from sqlmodel import col, select

from tradebot.clock import Clock
from tradebot.data import MarketData
from tradebot.db import repo
from tradebot.db.engine import session_scope
from tradebot.db.models import BacktestEquityPoint, BacktestRun, BacktestTrade, StrategyConfig

log = logging.getLogger(__name__)

ALLOWED_OVERRIDES = frozenset({"rf_annual", "initial_capital"})
WARMUP_DAYS = 1100  # calendar days of history before ``start`` (SMA200 / ret126 warm-up)

DataFactory = Callable[[], MarketData]


def _f(x: float | None) -> float | None:
    """NaN / inf -> None."""
    return None if x is None or not math.isfinite(x) else float(x)


def fail_stale_runs(engine: Engine, now: datetime) -> int:
    """Mark ``queued`` / ``running`` rows left by a killed process as ``failed``."""
    with session_scope(engine) as s:
        rows = list(
            s.exec(select(BacktestRun).where(col(BacktestRun.status).in_(["queued", "running"])))
        )
        for r in rows:
            r.status = "failed"
            r.error = "interrupted: the API stopped before this run finished"
            r.finished_at = now
            s.add(r)
    return len(rows)


class BacktestJobs:
    """Owns the one-worker pool; ``submit(run_id)`` returns immediately."""

    def __init__(self, engine: Engine, data_factory: DataFactory, clock: Clock) -> None:
        self.engine = engine
        self.data_factory = data_factory
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest")
        self._futures: list[Future[None]] = []

    def submit(self, run_id: int) -> None:
        """Queue a run."""
        self._futures.append(self._pool.submit(self._run, run_id))

    def wait_idle(self, timeout: float | None = None) -> None:
        """Block until every submitted run has finished (tests)."""
        for f in list(self._futures):
            f.result(timeout)

    def shutdown(self) -> None:
        """Stop accepting work and wait for the running job."""
        self._pool.shutdown(wait=True)

    # ------------------------------------------------------------------ the job

    def _now(self) -> datetime:
        return repo.naive_utc(self.clock.now())

    def _run(self, run_id: int) -> None:
        try:
            with session_scope(self.engine) as s:
                row = s.get(BacktestRun, run_id)
                if row is None:
                    return
                row.status, row.started_at = "running", self._now()
                s.add(row)
            self._execute(run_id)
        except Exception as exc:  # noqa: BLE001 - any failure is recorded, never raised
            log.exception("backtest %s failed", run_id)
            with session_scope(self.engine) as s:
                row = s.get(BacktestRun, run_id)
                if row is not None:
                    row.status, row.error, row.finished_at = (
                        "failed",
                        f"{type(exc).__name__}: {exc}",
                        self._now(),
                    )
                    s.add(row)

    def _execute(self, run_id: int) -> None:
        from tradebot.strategy.io import load_strategy

        with session_scope(self.engine) as s:
            row = s.get(BacktestRun, run_id)
            assert row is not None
            params: dict[str, Any] = json.loads(row.params_json)
            sc_row = s.get(StrategyConfig, row.strategy_config_id)
            if sc_row is None:
                raise ValueError("strategy not found")
            spec_json = sc_row.spec_json
        spec = load_strategy(spec_json)
        start = date.fromisoformat(params["start"])
        end = date.fromisoformat(params["end"]) if params.get("end") else self.clock.now().date()
        outcome = compute_backtest(
            spec,
            start,
            end,
            params.get("overrides") or {},
            params.get("mode", "single"),
            self.data_factory(),
        )
        persist_done(self.engine, run_id, outcome, self._now())


@dataclass
class BacktestOutcome:
    """A finished run, ready to persist or print."""

    report: Any  # BacktestReport
    payload: dict[str, Any]
    benchmark: str


def single_sleeve_config(spec: Any, start: date, end: date, over: dict[str, Any]) -> Any:
    """A ``BacktestConfig`` with ``spec`` as the one sleeve holding 100% of the fund."""
    from tradebot.backtest.engine import BacktestConfig, SleeveSpec
    from tradebot.data.types import AssetClass
    from tradebot.portfolio.config import SleeveConfig, default_config

    cls = AssetClass.CRYPTO if spec.asset_class == "crypto" else AssetClass.EQUITY
    sleeve = SleeveConfig(
        sleeve_id=spec.id,
        name=spec.id,
        target_weight=Decimal(1),
        vol_scaled=True,
        allowed_asset_classes=frozenset({cls}),
    )
    upd: dict[str, Any] = {"sleeves": (sleeve,)}
    if "initial_capital" in over:
        upd["initial_capital"] = Decimal(str(over["initial_capital"]))
    return BacktestConfig(
        sleeves=[SleeveSpec(sleeve_id=spec.id, strategy=spec)],
        start=start,
        end=end,
        engine=default_config().model_copy(update=upd),
        rf_annual=float(over.get("rf_annual", 0.0)),
    )


def compute_backtest(
    spec: Any, start: date, end: date, over: dict[str, Any], mode: str, data: MarketData
) -> BacktestOutcome:
    """Load cached bars and run ``run_with_benchmark`` (plus validation for non-single modes)."""
    from tradebot.backtest.engine import run_with_benchmark

    cfg = single_sleeve_config(spec, start, end, over)
    bench = "BTC/USD" if spec.asset_class == "crypto" else "SPY"
    symbols = sorted({str(x) for x in spec.required_symbols()} | {bench})
    got = data.get_bars_many(symbols, start - timedelta(days=WARMUP_DAYS), end, refresh=False)
    bars = {str(k): v for k, v in got.items()}
    report = run_with_benchmark(cfg, bars)
    payload = json.loads(report.to_json())
    if mode != "single":
        from tradebot.backtest.validation import run_validation

        payload["validation"] = json.loads(run_validation(cfg, bars).model_dump_json())
    return BacktestOutcome(report, payload, bench)


def persist_done(engine: Engine, run_id: int, outcome: BacktestOutcome, now: datetime) -> None:
    """Write the result, denormalized metrics, equity points and closed trades; ``status=done``."""
    report, bench = outcome.report, outcome.benchmark
    res, m = report.result, report.metrics
    eq = res.equity.astype(float)
    dd = 1 - eq.to_numpy() / np.maximum.accumulate(eq.to_numpy())
    bench_eq = report.benchmark_equity.get(bench)
    with session_scope(engine) as s:
        row = s.get(BacktestRun, run_id)
        assert row is not None
        row.status, row.finished_at = "done", now
        row.data_fingerprint = res.data_fingerprint
        row.result_json = json.dumps(outcome.payload)
        row.total_return, row.cagr = _f(m.total_return), _f(m.cagr)
        row.sharpe, row.sortino = _f(m.sharpe), _f(m.sortino)
        row.max_drawdown = _f(abs(m.max_drawdown))
        row.n_trades, row.benchmark_symbol = m.trade_count, bench
        s.add(row)
        for i, (ts, v) in enumerate(eq.items()):
            b = None
            if bench_eq is not None and ts in bench_eq.index:
                b = _f(float(bench_eq.loc[ts]))
            s.add(
                BacktestEquityPoint(
                    run_id=run_id,
                    ts=repo.naive_utc(pd.Timestamp(ts)),
                    equity=float(v),
                    benchmark_equity=b,
                    drawdown=float(dd[i]),
                )
            )
        for t in res.trades:
            s.add(
                BacktestTrade(
                    run_id=run_id,
                    symbol=t.symbol,
                    entry_ts=datetime(t.entry_asof.year, t.entry_asof.month, t.entry_asof.day),
                    exit_ts=datetime(t.exit_asof.year, t.exit_asof.month, t.exit_asof.day),
                    qty=float(t.qty),
                    entry_px=float(t.entry_price),
                    exit_px=float(t.exit_price),
                    pnl=float(t.net_pnl),
                    fees=float(t.fees),
                    sleeve_key=t.sleeve_id,
                )
            )
