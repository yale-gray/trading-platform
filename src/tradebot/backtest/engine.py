"""Backtest engine and report (SPEC-002 sections 6, 7, 9).

``run_backtest`` has no per-bar logic of its own: it builds the engine config, constructs
``PortfolioEngine`` and ``BarDriver``, iterates, and derives results with ``trades.py``.
``run_with_benchmark`` adds buy-and-hold SPY / BTC/USD (and a static blend for a
multi-class fund) run through the *same* engine, costs and window. Benchmark runs relax the
fund's per-asset / crypto caps and the kill switch (they are fund policy, not market
costs); fees and slippage are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from tradebot.backtest.driver import BarDriver, StepResult, build_engine_config
from tradebot.backtest.metrics import Metrics, compute_metrics, periods_per_year
from tradebot.backtest.trades import (
    ClosedTrade,
    OpenPosition,
    closed_trades,
    equity_series,
    exposure_series,
    open_positions,
)
from tradebot.data.symbols import asset_class, normalize_symbol
from tradebot.data.types import AssetClass
from tradebot.portfolio.config import EngineConfig, SleeveConfig
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import LedgerEvent
from tradebot.risk.config import RiskConfig
from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.schema import RulesSpec, StrategySpec

__all__ = [
    "BacktestConfig",
    "BacktestReport",
    "BacktestResult",
    "SleeveSpec",
    "run_backtest",
    "run_with_benchmark",
    "validate_bars",
]

_COLUMNS = ["open", "high", "low", "close", "volume"]
_DELTA_FIELDS = ("total_return", "cagr", "max_drawdown", "sharpe", "sortino")


class SleeveSpec(BaseModel):
    """A sleeve and its strategy (``None`` = cash buffer). Weight lives in ``SleeveConfig``."""

    model_config = ConfigDict(frozen=True)

    sleeve_id: str
    strategy: StrategySpec | None


class BacktestConfig(BaseModel):
    """Everything one backtest run needs besides the bars."""

    model_config = ConfigDict(frozen=True)

    sleeves: list[SleeveSpec]
    start: date
    end: date
    engine: EngineConfig
    rf_annual: float = 0.0


class BacktestResult(BaseModel):
    """Results of one run, derived from the ledger and snapshots."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config_hash: str
    data_fingerprint: str
    periods_per_year: int
    equity: pd.Series
    exposure: pd.Series
    trades: list[ClosedTrade]
    open_positions: list[OpenPosition]
    killed_on: date | None
    killed_dates: list[date] = []  # every trip (killed_on is the first); not in to_json
    ledger: list[LedgerEvent]
    n_signals: int


class BacktestReport(BaseModel):
    """Fund result plus metrics and buy-and-hold benchmarks over the identical window."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: BacktestResult
    metrics: Metrics
    benchmark_metrics: dict[str, Metrics]
    benchmark_equity: dict[str, pd.Series]
    deltas: dict[str, dict[str, float]]  # fund minus benchmark, per benchmark

    @property
    def killed_on(self) -> date | None:
        """Date the kill switch tripped, or ``None`` (already in ``to_json`` as ``killed_on``)."""
        return self.result.killed_on

    def to_json(self) -> str:
        """Canonical JSON (sorted keys, NaN -> null): byte-identical for identical inputs."""
        r = self.result
        payload: dict[str, Any] = {
            "config_hash": r.config_hash,
            "data_fingerprint": r.data_fingerprint,
            "periods_per_year": r.periods_per_year,
            "metrics": self.metrics.model_dump(),
            "benchmark_metrics": {k: v.model_dump() for k, v in self.benchmark_metrics.items()},
            "deltas": self.deltas,
            "equity": _series_rows(r.equity),
            "benchmark_equity": {k: _series_rows(v) for k, v in self.benchmark_equity.items()},
            "exposure": _series_rows(r.exposure),
            "trades": [t.model_dump(mode="json") for t in r.trades],
            "open_positions": [p.model_dump(mode="json") for p in r.open_positions],
            "killed_on": r.killed_on.isoformat() if r.killed_on else None,
            "n_signals": r.n_signals,
            "ledger_sha256": _ledger_hash(r.ledger),
        }
        return json.dumps(_clean(payload), sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------- helpers


def _clean(x: Any) -> Any:
    """NaN/inf -> None, recursively (JSON has no NaN)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _series_rows(s: pd.Series) -> list[list[Any]]:
    return [[ts.date().isoformat(), float(v)] for ts, v in s.items()]


def _ledger_hash(ledger: list[LedgerEvent]) -> str:
    h = hashlib.sha256()
    for ev in ledger:
        h.update(ev.model_dump_json().encode())
        h.update(b"\n")
    return h.hexdigest()


def _config_payload(cfg: BacktestConfig) -> dict[str, Any]:
    eng = cfg.engine
    return {
        "start": cfg.start.isoformat(),
        "end": cfg.end.isoformat(),
        "rf_annual": cfg.rf_annual,
        "initial_capital": str(eng.initial_capital),
        "risk": eng.risk.model_dump(mode="json"),
        "costs": {k.value: v.model_dump(mode="json") for k, v in sorted(eng.costs.items())},
        "engine_sleeves": [
            {**s.model_dump(mode="json"), "allowed_asset_classes": sorted(s.allowed_asset_classes)}
            for s in eng.sleeves
        ],
        "sleeves": [
            {
                "sleeve_id": s.sleeve_id,
                "strategy": s.strategy.resolve().model_dump(mode="json", by_alias=True)
                if s.strategy
                else None,
            }
            for s in sorted(cfg.sleeves, key=lambda s: s.sleeve_id)
        ],
    }


def config_hash(cfg: BacktestConfig) -> str:
    """sha256 of the canonical JSON of the resolved config."""
    text = json.dumps(_config_payload(cfg), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def data_fingerprint(bars: Mapping[str, pd.DataFrame]) -> str:
    """sha256 over every symbol's index and OHLCV arrays, in sorted symbol order."""
    h = hashlib.sha256()
    for sym in sorted(bars):
        df = bars[sym]
        h.update(sym.encode())
        h.update(df.index.asi8.tobytes())
        for col in _COLUMNS:
            h.update(np.ascontiguousarray(df[col].to_numpy(dtype="float64")).tobytes())
    return h.hexdigest()


def validate_bars(bars: Mapping[str, pd.DataFrame]) -> None:
    """Assert the bar contract (SPEC-002 data interface); ``ValueError`` on breach."""
    for sym, df in sorted(bars.items()):
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None or str(idx.tz) != "UTC":
            raise ValueError(f"{sym}: index must be a tz-aware UTC DatetimeIndex")
        if not (idx.is_monotonic_increasing and idx.is_unique):
            raise ValueError(f"{sym}: index must be strictly increasing and unique")
        if list(df.columns) != _COLUMNS:
            raise ValueError(f"{sym}: columns must be exactly {_COLUMNS}, got {list(df.columns)}")
        if any(df[c].dtype != np.float64 for c in _COLUMNS):
            raise ValueError(f"{sym}: columns must be float64")
        if df.isna().any().any():
            raise ValueError(f"{sym}: NaN in bars")
        lo, hi = df["low"], df["high"]
        if ((lo > df["open"]) | (lo > df["close"]) | (hi < df["open"]) | (hi < df["close"])).any():
            raise ValueError(f"{sym}: low <= open,close <= high violated")


def _required(cfg: BacktestConfig) -> list[str]:
    syms: set[str] = set()
    for s in cfg.sleeves:
        if s.strategy is not None:
            syms.update(str(normalize_symbol(x)) for x in s.strategy.required_symbols())
    return sorted(syms)


def _p_for(cfg: BacktestConfig) -> int:
    return periods_per_year(_required(cfg))


# ---------------------------------------------------------------------------- run


def _check_warmup(cfg: BacktestConfig, bars: Mapping[str, pd.DataFrame]) -> None:
    """Raise unless each strategy sleeve has ``warmup_bars`` calendar bars before ``cfg.start``."""
    cut = pd.Timestamp(cfg.start, tz="UTC")
    for sleeve in sorted(cfg.sleeves, key=lambda s: s.sleeve_id):
        if sleeve.strategy is None:
            continue
        compiled = compile_strategy(sleeve.strategy, sleeve.sleeve_id)
        cal: pd.DatetimeIndex | None = None
        for sym in compiled.required:
            cal = bars[sym].index if cal is None else cal.union(bars[sym].index)
        have = 0 if cal is None else int((cal < cut).sum())
        if have < compiled.warmup_bars:
            raise ValueError(
                f"sleeve {sleeve.sleeve_id!r}: only {have} bars before start {cfg.start}, "
                f"needs warmup_bars={compiled.warmup_bars}"
            )


def run_backtest(cfg: BacktestConfig, bars: Mapping[str, pd.DataFrame]) -> BacktestResult:
    """Run one backtest: validate, build engine and driver, step every bar, derive results.

    Open positions at the end are reported, not force-closed. Strategies are compiled fresh
    for the run (audit F1). Bars before ``cfg.start`` serve as indicator warm-up: every
    strategy sleeve must have at least ``warmup_bars`` bars of its own calendar (the union
    of its symbols) before ``start``, otherwise ``ValueError`` is raised, so trading and the
    benchmark both begin on ``start`` rather than the fund idling through warm-up.
    """
    canon = {str(normalize_symbol(k)): v for k, v in bars.items()}
    needed = _required(cfg)
    missing = sorted(set(needed) - set(canon))
    if missing:
        raise ValueError(f"missing bars for required symbols: {missing}")
    validate_bars({s: canon[s] for s in needed})
    _check_warmup(cfg, canon)
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
    driver = BarDriver(engine, cfg.sleeves, canon, start=cfg.start)
    steps: list[StepResult] = list(driver.run(end=cfg.end))
    if not steps:
        raise ValueError("no bars in the backtest window")
    snaps = [s.bar_result.snapshot for s in steps]
    orders = {o.order_id: o for o in engine.orders()}
    killed_on = next((s.asof for s in steps if s.bar_result.kill_switch_tripped), None)
    return BacktestResult(
        config_hash=config_hash(cfg),
        data_fingerprint=data_fingerprint({s: canon[s] for s in needed}),
        periods_per_year=_p_for(cfg),
        equity=equity_series(snaps),
        exposure=exposure_series(snaps),
        trades=closed_trades(engine.ledger, orders),
        open_positions=open_positions(snaps[-1]),
        killed_on=killed_on,
        killed_dates=[s.asof for s in steps if s.bar_result.kill_switch_tripped],
        ledger=list(engine.ledger),
        n_signals=sum(len(s.signals) for s in steps),
    )


# ---------------------------------------------------------------------------- benchmark


def _hold_spec(symbol: str) -> StrategySpec:
    """Buy-and-hold: enter on bar one (entry always true), never exit."""
    ac = "crypto" if asset_class(normalize_symbol(symbol)) is AssetClass.CRYPTO else "equity"
    return RulesSpec.model_validate(
        {
            "id": f"bh_{symbol.replace('/', '_').lower()}",
            "name": f"Buy and hold {symbol}",
            "asset_class": ac,
            "universe": [symbol],
            "schedule": {"freq": "daily"},
            "entry": {"always": True},
            "position": {"weight": 1.0},
        }
    )


def _bench_config(
    cfg: BacktestConfig, parts: list[tuple[str, Decimal, str]], cash: Decimal
) -> BacktestConfig:
    """``parts`` = (sleeve_id, weight, symbol). Same costs/capital, relaxed risk caps."""
    sleeves: list[SleeveSpec] = []
    scs: list[SleeveConfig] = []
    for sid, weight, sym in parts:
        ac = asset_class(normalize_symbol(sym))
        sleeves.append(SleeveSpec(sleeve_id=sid, strategy=_hold_spec(sym)))
        scs.append(
            SleeveConfig(
                sleeve_id=sid,
                name=f"Buy and hold {sym}",
                target_weight=weight,
                vol_scaled=False,
                allowed_asset_classes=frozenset({ac}),
            )
        )
    if cash > 0:
        sleeves.append(SleeveSpec(sleeve_id="cash", strategy=None))
        scs.append(
            SleeveConfig(
                sleeve_id="cash",
                name="Cash",
                target_weight=cash,
                vol_scaled=False,
                allowed_asset_classes=frozenset(),
            )
        )
    risk = RiskConfig(
        max_asset_fraction=Decimal(1),
        max_crypto_fraction=Decimal(1),
        kill_switch_drawdown=Decimal(1),
        max_pending_bars=cfg.engine.risk.max_pending_bars,
    )
    eng = cfg.engine.model_copy(update={"sleeves": tuple(scs), "risk": risk})
    return BacktestConfig(
        sleeves=sleeves, start=cfg.start, end=cfg.end, engine=eng, rf_annual=cfg.rf_annual
    )


def _align(s: pd.Series, index: pd.DatetimeIndex, fill: float) -> pd.Series:
    """Carry a series forward onto the fund's index; before its first point use ``fill``."""
    union = s.index.union(index)
    return s.reindex(union).ffill().reindex(index).fillna(fill)


def run_with_benchmark(cfg: BacktestConfig, bars: Mapping[str, pd.DataFrame]) -> BacktestReport:
    """Fund run plus buy-and-hold SPY / BTC/USD (and ``blend``) through the same engine."""
    fund = run_backtest(cfg, bars)
    p = fund.periods_per_year
    metrics = compute_metrics(fund.equity, fund.trades, fund.exposure, p, cfg.rf_annual)
    weights = {c: Decimal(0) for c in ("equity", "crypto")}
    for sc in cfg.engine.sleeves:
        spec = next(s for s in cfg.sleeves if s.sleeve_id == sc.sleeve_id).strategy
        if spec is not None:
            weights[spec.asset_class] += sc.target_weight
    cash = Decimal(1) - weights["equity"] - weights["crypto"]
    runs: dict[str, BacktestConfig] = {}
    if weights["equity"] > 0:
        runs["SPY"] = _bench_config(cfg, [("bh_equity", Decimal(1), "SPY")], Decimal(0))
    if weights["crypto"] > 0:
        runs["BTC/USD"] = _bench_config(cfg, [("bh_crypto", Decimal(1), "BTC/USD")], Decimal(0))
    if weights["equity"] > 0 and weights["crypto"] > 0:
        runs["blend"] = _bench_config(
            cfg,
            [
                ("bh_crypto", weights["crypto"], "BTC/USD"),
                ("bh_equity", weights["equity"], "SPY"),
            ],
            cash,
        )
    b_metrics: dict[str, Metrics] = {}
    b_equity: dict[str, pd.Series] = {}
    deltas: dict[str, dict[str, float]] = {}
    for name in sorted(runs):
        res = run_backtest(runs[name], bars)
        eq = _align(res.equity, fund.equity.index, float(cfg.engine.initial_capital))
        ex = _align(res.exposure, fund.equity.index, 0.0)
        b_equity[name] = eq
        b_metrics[name] = compute_metrics(eq, res.trades, ex, p, cfg.rf_annual)
        deltas[name] = {f: getattr(metrics, f) - getattr(b_metrics[name], f) for f in _DELTA_FIELDS}
    return BacktestReport(
        result=fund,
        metrics=metrics,
        benchmark_metrics=b_metrics,
        benchmark_equity=b_equity,
        deltas=deltas,
    )
