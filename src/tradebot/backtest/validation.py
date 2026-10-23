"""Validation gates (SPEC-002 section 8, ADR-0001 D10): the honest-process machinery.

Gate 1 history + hold-out, gate 2 walk-forward, gate 3 out-of-sample vs benchmark, gate 4
trade count, gate 5 +-20% parameter sensitivity, gate 6 forward paper trading.

The hold-out is enforced structurally: every tuning, walk-forward and sweep function takes a
:class:`DataView` whose frames end *before* the cut date. The only function that reads the
out-of-sample window is :func:`run_oos`, which needs a config frozen in an
:class:`OosRegistry` beforehand and evaluates each config hash once. A second, different
hash for the same strategy id is flagged ``peeking``.

The engine is injected through :class:`Runners` so the machinery is testable on stand-in
backtesters (the "memorizer" negative control); the defaults are the real engine.
"""

from __future__ import annotations

import itertools
import logging
import math
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from tradebot.backtest.engine import (
    BacktestConfig,
    BacktestReport,
    SleeveSpec,
    config_hash,
    run_backtest,
    run_with_benchmark,
)
from tradebot.backtest.metrics import Metrics, compute_metrics
from tradebot.backtest.protocol import (
    DEFAULT_HOLDOUT_LOG,
    DsrResult,
    EdgeResult,
    HoldoutLog,
    OverlayResult,
    daily_returns,
    deflated_sharpe,
    edge_vs_tbills,
    holm_adjust,
    null_sharpe_variance,
    spy_overlay,
)
from tradebot.backtest.registry import DEFAULT_REGISTRY_PATH, TrialRecord, TrialRegistry
from tradebot.data.symbols import normalize_symbol
from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.schema import _INT_KINDS, StrategySpec, _map_params

__all__ = [
    "HOLDOUT_START",
    "BatchEntry",
    "CheckResult",
    "DataView",
    "ForwardStats",
    "GateConfig",
    "GateResult",
    "GridSpec",
    "HistoryCheck",
    "OosRegistry",
    "OosRun",
    "ProtocolVerdict",
    "Runners",
    "SensitivityResult",
    "Split",
    "ValidationReport",
    "WalkForwardConfig",
    "WalkForwardResult",
    "check_history",
    "evaluate_batch",
    "evaluate_gate",
    "forward_stats",
    "make_is_view",
    "open_holdout",
    "protocol_verdict",
    "run_fund_report",
    "run_oos",
    "run_validation",
    "sensitivity_sweep",
    "split_oos",
    "strategy_calendar",
    "walk_forward",
]

log = logging.getLogger(__name__)

Backtester = Callable[[BacktestConfig, Mapping[str, pd.DataFrame]], BacktestReport]
Status = Literal["passed", "failed", "pending"]
NAN = float("nan")
ATR_PERIOD = 14
HOLDOUT_START = date(2024, 1, 1)
"""ADR-0001 A25: bars on or after this date are the final hold-out, opened via open_holdout."""


# ---------------------------------------------------------------------------- runners


def run_fund_report(cfg: BacktestConfig, bars: Mapping[str, pd.DataFrame]) -> BacktestReport:
    """Fund-only run (no benchmark runs): cheap, used for sweeps, tuning and trade counts."""
    res = run_backtest(cfg, bars)
    m = compute_metrics(res.equity, res.trades, res.exposure, res.periods_per_year, cfg.rf_annual)
    return BacktestReport(
        result=res, metrics=m, benchmark_metrics={}, benchmark_equity={}, deltas={}
    )


@dataclass(frozen=True)
class Runners:
    """The two backtest entry points the validation code uses (injectable for tests)."""

    fund: Backtester = run_fund_report
    benchmarked: Backtester = run_with_benchmark


DEFAULT_RUNNERS = Runners()


# ---------------------------------------------------------------------------- config


class GridSpec(BaseModel):
    """Parameter grid for per-window tuning; the best ``objective`` on the train window wins."""

    model_config = ConfigDict(frozen=True)

    grid: dict[str, list[float]]
    objective: Literal["sharpe", "sortino", "total_return"] = "sharpe"


class WalkForwardConfig(BaseModel):
    """Rolling windows in strategy-calendar bars (Pardo 2008). ``tune=None``: fixed params."""

    model_config = ConfigDict(frozen=True)

    train_bars: int
    test_bars: int
    step_bars: int
    anchored: bool = False
    tune: GridSpec | None = None

    @classmethod
    def defaults(cls, asset_class: str) -> WalkForwardConfig:
        """Equities 756/252/252 (3y/1y/1y), crypto 548/182/182 (1.5y/6m/6m)."""
        if asset_class == "crypto":
            return cls(train_bars=548, test_bars=182, step_bars=182)
        return cls(train_bars=756, test_bars=252, step_bars=252)

    @classmethod
    def protocol_2026(cls, asset_class: str, bars_per_day: int = 1) -> WalkForwardConfig:
        """ADR-0001 A24: 2y/1y/1y in sessions (504/252/252) x ``bars_per_day`` (any class)."""
        del asset_class  # same session-based windows for every class
        return cls(
            train_bars=504 * bars_per_day,
            test_bars=252 * bars_per_day,
            step_bars=252 * bars_per_day,
        )


class GateConfig(BaseModel):
    """Gate thresholds (ADR-0001 D10 defaults; PM-tunable)."""

    model_config = ConfigDict(frozen=True)

    min_years_equity: float = 10.0
    min_years_crypto: float = 5.0
    wf_min_windows: int = 4
    wf_sharpe_frac: float = 0.6  # >= this share of test windows with Sharpe > 0
    wf_dd_frac: float = 0.6  # >= this share of windows with drawdown <= wf_dd_mult * bench
    wf_dd_mult: float = 1.0
    oos_dd_mult: float = 1.0
    min_trades: int = 30
    sens_sharpe_floor: float = 0.5  # variant collapses if sharpe < floor * base sharpe
    sens_dd_mult: float = 1.5  # ... or |max_dd| > mult * |base max_dd|
    forward_min_days: int = 90
    # Evidence protocol (ADR-0001 A24-A26); inert unless ``protocol`` is set
    protocol: bool = False
    dsr_min: float = 0.95
    edge_alpha: float = 0.05
    overlay_alpha_t: float = 2.0

    @classmethod
    def protocol_2026(cls) -> GateConfig:
        """ADR-0001 A24/A30 thresholds: 60 trades, 7y equity, 5y crypto, 3 walk-forward windows."""
        return cls(
            min_trades=60,
            min_years_equity=7.0,
            min_years_crypto=5.0,
            wf_min_windows=3,
            protocol=True,
        )


# ---------------------------------------------------------------------------- helpers


def _canon(bars: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {str(normalize_symbol(k)): v for k, v in bars.items()}


def _strategy_sleeves(cfg: BacktestConfig) -> list[SleeveSpec]:
    return sorted((s for s in cfg.sleeves if s.strategy is not None), key=lambda s: s.sleeve_id)


def _target_sleeve(cfg: BacktestConfig, sleeve_id: str | None) -> SleeveSpec:
    sleeves = _strategy_sleeves(cfg)
    if sleeve_id is None:
        if len(sleeves) != 1:
            raise ValueError("cfg has several strategy sleeves; pass sleeve_id")
        return sleeves[0]
    for s in sleeves:
        if s.sleeve_id == sleeve_id:
            return s
    raise ValueError(f"no strategy sleeve {sleeve_id!r}")


def _with_strategy(cfg: BacktestConfig, sleeve_id: str, spec: StrategySpec) -> BacktestConfig:
    sleeves = [
        SleeveSpec(sleeve_id=s.sleeve_id, strategy=spec) if s.sleeve_id == sleeve_id else s
        for s in cfg.sleeves
    ]
    return cfg.model_copy(update={"sleeves": sleeves})


def _strategy_id(cfg: BacktestConfig) -> str:
    return "+".join(s.strategy.id for s in _strategy_sleeves(cfg) if s.strategy is not None)


def strategy_calendar(cfg: BacktestConfig, bars: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """Union of the bar timestamps of every symbol the strategy sleeves need (ADR D10)."""
    canon = _canon(bars)
    cal: pd.DatetimeIndex | None = None
    for s in _strategy_sleeves(cfg):
        assert s.strategy is not None
        for sym in s.strategy.required_symbols():
            idx = canon[str(normalize_symbol(sym))].index
            cal = idx if cal is None else cal.union(idx)
    if cal is None:
        raise ValueError("cfg has no strategy sleeve")
    return pd.DatetimeIndex(cal).sort_values()


def _warmup(cfg: BacktestConfig) -> int:
    return max(
        compile_strategy(s.strategy, s.sleeve_id).warmup_bars
        for s in _strategy_sleeves(cfg)
        if s.strategy is not None
    )


def _bench_key(report: BacktestReport) -> str | None:
    keys = sorted(report.benchmark_metrics)
    if not keys:
        return None
    return "blend" if "blend" in keys else keys[0]


def _ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d, tz="UTC")


def _finite(x: float) -> bool:
    return isinstance(x, float) and math.isfinite(x)


def _num(x: float) -> float | None:
    return float(x) if _finite(float(x)) else None


# ---------------------------------------------------------------------------- gate 1


def split_oos(
    index: pd.DatetimeIndex, oos_frac: float = 0.30
) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    """``cut = index[floor(N * (1 - oos_frac))]``; in-sample is before the cut, OOS from it."""
    n = len(index)
    k = int(math.floor(round(n * (1.0 - oos_frac), 9)))
    if n < 2 or not 0 < k < n:
        raise ValueError(f"cannot split {n} bars with oos_frac={oos_frac}")
    return index[:k], index[k:]


class Split(BaseModel):
    """Where the hold-out starts, on the strategy calendar."""

    model_config = ConfigDict(frozen=True)

    cut: date
    is_start: date
    is_end: date
    oos_end: date
    n_is: int
    n_oos: int


class HistoryCheck(BaseModel):
    """Calendar span after warm-up on the strategy's calendar (not per symbol, ADR D10)."""

    model_config = ConfigDict(frozen=True)

    asset_class: str
    span_years: float
    required_years: float
    passed: bool


def check_history(
    calendar: pd.DatetimeIndex,
    asset_class: str,
    warmup_bars: int = 0,
    *,
    min_years_equity: float = 10.0,
    min_years_crypto: float = 5.0,
) -> HistoryCheck:
    """>= 10y (equity) / >= 5y (crypto) of span after ``warmup_bars`` of the strategy calendar."""
    required = min_years_crypto if asset_class == "crypto" else min_years_equity
    usable = calendar[warmup_bars:]
    span = (usable[-1] - usable[0]).days / 365.25 if len(usable) >= 2 else 0.0
    return HistoryCheck(
        asset_class=asset_class,
        span_years=span,
        required_years=required,
        passed=span >= required,
    )


@dataclass(frozen=True)
class DataView:
    """Bars restricted to the in-sample region (``oos_locked``): every frame ends before ``cut``."""

    bars: Mapping[str, pd.DataFrame]
    cut: date
    oos_locked: bool = True

    def __post_init__(self) -> None:
        lim = _ts(self.cut)
        for sym, df in self.bars.items():
            if len(df) and df.index[-1] >= lim:
                raise ValueError(
                    f"view for {sym} reaches the out-of-sample window (cut {self.cut})"
                )


def make_is_view(bars: Mapping[str, pd.DataFrame], cut: date) -> DataView:
    """Truncate every frame to bars strictly before ``cut``; the OOS bars are not copied."""
    lim = _ts(cut)
    return DataView({k: v.loc[v.index < lim] for k, v in _canon(bars).items()}, cut)


# ---------------------------------------------------------------------------- gate 2


class WindowResult(BaseModel):
    """One walk-forward test window."""

    model_config = ConfigDict(frozen=True)

    k: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    params: dict[str, float] | None
    sharpe: float
    total_return: float
    max_drawdown: float
    bench_max_drawdown: float | None
    train_sharpe: float | None = None
    efficiency: float | None = None  # annualised test return / annualised train return (tuned)


class WalkForwardResult(BaseModel):
    """Per-window table and the consistency fractions (gate 2)."""

    model_config = ConfigDict(frozen=True)

    windows: list[WindowResult]
    n_trials: int

    @property
    def n_windows(self) -> int:
        """Number of test windows."""
        return len(self.windows)

    def frac_sharpe_positive(self) -> float:
        """Share of windows with Sharpe > 0."""
        return _frac(w.sharpe > 0 for w in self.windows)

    def frac_dd_ok(self, mult: float = 1.0) -> float:
        """Share of windows where |max drawdown| <= mult * |benchmark's|."""
        return _frac(
            w.bench_max_drawdown is not None
            and abs(w.max_drawdown) <= mult * abs(w.bench_max_drawdown)
            for w in self.windows
        )

    def mean_efficiency(self) -> float | None:
        """Mean walk-forward efficiency over windows that have one (tuned runs only)."""
        vals = [w.efficiency for w in self.windows if w.efficiency is not None]
        return sum(vals) / len(vals) if vals else None


def _frac(flags: Any) -> float:
    items = list(flags)
    return sum(1 for f in items if f) / len(items) if items else 0.0


def walk_forward(
    cfg: BacktestConfig,
    view: DataView,
    wf: WalkForwardConfig,
    *,
    sleeve_id: str | None = None,
    runners: Runners = DEFAULT_RUNNERS,
) -> WalkForwardResult:
    """Rolling train/test windows inside the in-sample ``view`` (never sees the hold-out).

    Window k: train ``[s_k, s_k + train)``, test the next ``test`` bars, ``s_{k+1} = s_k + step``
    (anchored: ``s_k`` stays at the first bar). Positions are on the strategy calendar from
    ``cfg.start``; each test run warms up on the bars before it. ``wf.tune`` picks, per train
    window, the best grid point; ``None`` evaluates ``cfg``'s fixed parameters.
    """
    if not view.oos_locked:
        raise ValueError("walk_forward requires an out-of-sample-locked view")
    target = _target_sleeve(cfg, sleeve_id)
    assert target.strategy is not None
    cal = strategy_calendar(cfg, view.bars)
    cal = cal[cal >= _ts(cfg.start)]
    windows: list[WindowResult] = []
    trials = 0
    s = 0
    while s + wf.train_bars + wf.test_bars <= len(cal):
        tr_lo = 0 if wf.anchored else s
        tr_hi = s + wf.train_bars - 1
        te_lo = s + wf.train_bars
        te_hi = te_lo + wf.test_bars - 1
        d = [c.date() for c in (cal[tr_lo], cal[tr_hi], cal[te_lo], cal[te_hi])]
        params: dict[str, float] | None = None
        spec = target.strategy
        train_sharpe: float | None = None
        train_cagr = NAN
        if wf.tune is not None:
            best: tuple[float, dict[str, float], float] | None = None
            for overrides in _grid_points(wf.tune):
                try:
                    variant = target.strategy.resolve(overrides)
                except ValueError:
                    continue
                train_cfg = _with_strategy(cfg, target.sleeve_id, variant).model_copy(
                    update={"start": d[0], "end": d[1]}
                )
                rep = runners.fund(train_cfg, view.bars)
                trials += 1
                score = float(getattr(rep.metrics, wf.tune.objective))
                if _finite(score) and (best is None or score > best[0]):
                    best = (score, overrides, rep.metrics.cagr)
            if best is None:
                raise ValueError(f"window {len(windows)}: no valid grid point")
            train_sharpe, params, train_cagr = best[0], best[1], best[2]
            spec = target.strategy.resolve(params)
        test_cfg = _with_strategy(cfg, target.sleeve_id, spec).model_copy(
            update={"start": d[2], "end": d[3]}
        )
        rep = runners.benchmarked(test_cfg, view.bars)
        trials += 1
        key = _bench_key(rep)
        eff = None
        if wf.tune is not None and _finite(train_cagr) and train_cagr != 0:
            eff = rep.metrics.cagr / train_cagr
        windows.append(
            WindowResult(
                k=len(windows),
                train_start=d[0],
                train_end=d[1],
                test_start=d[2],
                test_end=d[3],
                params=params,
                sharpe=rep.metrics.sharpe,
                total_return=rep.metrics.total_return,
                max_drawdown=rep.metrics.max_drawdown,
                bench_max_drawdown=(
                    rep.benchmark_metrics[key].max_drawdown if key is not None else None
                ),
                train_sharpe=train_sharpe,
                efficiency=eff if eff is None or _finite(eff) else None,
            )
        )
        s += wf.step_bars
    return WalkForwardResult(windows=windows, n_trials=trials)


def _grid_points(grid: GridSpec) -> list[dict[str, float]]:
    names = sorted(grid.grid)
    return [
        dict(zip(names, combo, strict=True))
        for combo in itertools.product(*(grid.grid[n] for n in names))
    ]


# ---------------------------------------------------------------------------- gate 5


class Variant(BaseModel):
    """One sensitivity run."""

    model_config = ConfigDict(frozen=True)

    name: str
    overrides: dict[str, float]
    metrics: Metrics


class SensitivityResult(BaseModel):
    """Base metrics, every variant that ran, and the variants skipped (with reasons)."""

    model_config = ConfigDict(frozen=True)

    pct: float
    base: Metrics
    variants: list[Variant]
    skipped: list[tuple[str, str]]

    def collapses(self, cfg: GateConfig | None = None) -> list[tuple[str, list[str]]]:
        """Variants that collapse, each with the reasons (recomputed under ``cfg``)."""
        c = cfg or GateConfig()
        out = []
        for v in self.variants:
            why = collapse_reasons(self.base, v.metrics, c)
            if why:
                out.append((v.name, why))
        return out

    @property
    def informational(self) -> bool:
        """Base Sharpe <= 0: the strategy already failed gate 3; the Sharpe test is skipped."""
        return not (_finite(self.base.sharpe) and self.base.sharpe > 0)

    def passed(self, cfg: GateConfig | None = None) -> bool:
        """No variant collapses."""
        return not self.collapses(cfg)


def collapse_reasons(base: Metrics, v: Metrics, cfg: GateConfig) -> list[str]:
    """Why ``v`` collapses against ``base`` (empty list = robust).

    When base Sharpe <= 0 the sweep is informational (SPEC-005 section 8): nothing collapses.
    """
    why: list[str] = []
    informational = not (_finite(base.sharpe) and base.sharpe > 0)
    if not informational:
        if not _finite(v.sharpe) or v.sharpe < cfg.sens_sharpe_floor * base.sharpe:
            why.append("sharpe")
    if not informational:
        if _finite(v.max_drawdown) and abs(v.max_drawdown) > cfg.sens_dd_mult * abs(
            base.max_drawdown
        ):
            why.append("drawdown")
        if _finite(base.total_return) and _finite(v.total_return):
            if (base.total_return > 0) != (v.total_return > 0) and (
                v.total_return != base.total_return
            ):
                why.append("return_sign")
    return why


def _int_params(spec: Any) -> set[str]:
    """Parameter names used in an integer slot (period / skip / top) of ``spec``."""
    used: list[tuple[str, str]] = []

    def collect(ref: str, kind: str) -> str:
        used.append((ref[1:], kind))
        return ref

    _map_params(spec._body_dump(), collect)
    return {n for n, kind in used if kind in _INT_KINDS}


def _bounds(
    v: float, pct: float, *, integer: bool | None = None
) -> tuple[float | None, float | None]:
    """Low and high perturbation of ``v``.

    Integer-slot parameters (``integer=True``) use floor/ceil (clamped >= 1, forced to differ
    from ``v``). Float parameters use exactly ``(1 -/+ pct) * v``, even when whole-valued.
    ``integer=None`` infers integer-ness from the value (legacy behaviour).
    """
    if integer is None:
        integer = float(v).is_integer()
    if integer:
        n = int(v)
        low = math.floor(round(n * (1 - pct), 9))
        high = math.ceil(round(n * (1 + pct), 9))
        if low >= n:
            low = n - 1
        if high <= n:
            high = n + 1
        return (float(low) if low >= 1 else None), float(high)
    return round(v * (1 - pct), 12), round(v * (1 + pct), 12)


def sensitivity_sweep(
    cfg: BacktestConfig,
    view: DataView,
    pct: float = 0.20,
    *,
    sleeve_id: str | None = None,
    runners: Runners = DEFAULT_RUNNERS,
) -> SensitivityResult:
    """One-at-a-time +-``pct`` sweep plus all-low / all-high corners, on the in-sample view.

    Variants violating ``constraints`` or resolving to an ATR-stop period other than 14
    (unsupported, ADR D5) are skipped and listed.
    """
    if not view.oos_locked:
        raise ValueError("sensitivity_sweep requires an out-of-sample-locked view")
    target = _target_sleeve(cfg, sleeve_id)
    spec = target.strategy
    assert spec is not None
    params = dict(spec.parameters)
    ints = _int_params(spec)
    bounds = {k: _bounds(v, pct, integer=k in ints) for k, v in sorted(params.items())}

    def check(over: Mapping[str, float]) -> str | None:
        """Reason a variant cannot run (``None`` = runnable)."""
        try:
            variant = spec.resolve(over)
        except ValueError as exc:  # constraint violation (pydantic ValidationError is one)
            msg = str(exc)
            if "atr_trailing period" in msg:
                return f"atr period != {ATR_PERIOD} unsupported"
            return f"constraint: {' '.join(msg.split())[:100]}"
        if any(int(st.period) != ATR_PERIOD for st in variant.stops):
            return f"atr period != {ATR_PERIOD} unsupported"
        return None

    plan: list[tuple[str, dict[str, float]]] = []
    skipped: list[tuple[str, str]] = []
    fixed: set[str] = set()  # params the engine cannot vary (ATR period): kept at base in corners
    for k, (lo, hi) in bounds.items():
        for label, val in (("low", lo), ("high", hi)):
            if val is None:
                skipped.append((f"{k}={label}", "below 1"))
            elif (why := check({k: val})) is not None and why.startswith("atr"):
                skipped.append((f"{k}={label}", why))
                fixed.add(k)
            else:
                plan.append((f"{k}={label}", {k: val}))
    for label, pick in (("low", 0), ("high", 1)):
        corner = {
            k: float(b[pick])  # type: ignore[arg-type]
            for k, b in bounds.items()
            if b[pick] is not None and k not in fixed
        }
        plan.append((f"corner={label}", corner))

    def run(over: Mapping[str, float]) -> Metrics:
        s = spec.resolve(over) if over else spec
        return runners.fund(_with_strategy(cfg, target.sleeve_id, s), view.bars).metrics

    base = run({})
    variants: list[Variant] = []
    for name, over in plan:
        if (why := check(over)) is not None:
            skipped.append((name, why))
            continue
        variants.append(Variant(name=name, overrides=over, metrics=run(over)))
    return SensitivityResult(pct=pct, base=base, variants=variants, skipped=skipped)


# ---------------------------------------------------------------------------- gate 3


class OosRun(BaseModel):
    """The one out-of-sample evaluation of a frozen config."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    strategy_id: str
    config_hash: str
    cut: date
    report: BacktestReport
    peeking: bool
    cached: bool = False


@dataclass
class OosRegistry:
    """Honest-process ledger: configs frozen before touching OOS, and what was evaluated."""

    frozen: dict[str, str] = field(default_factory=dict)  # config_hash -> strategy id
    evaluated: dict[tuple[str, str, date], OosRun] = field(default_factory=dict)
    peeking: set[str] = field(default_factory=set)

    def freeze(self, cfg: BacktestConfig) -> str:
        """Record ``cfg``'s hash before the OOS call and return it."""
        h = config_hash(cfg)
        self.frozen[h] = _strategy_id(cfg)
        return h


def run_oos(
    frozen_cfg: BacktestConfig,
    bars: Mapping[str, pd.DataFrame],
    *,
    cut: date,
    registry: OosRegistry,
    runners: Runners = DEFAULT_RUNNERS,
) -> OosRun:
    """The only function allowed to read the OOS window (trades from ``cut``, warm-up before).

    ``frozen_cfg`` must have been frozen in ``registry`` first. A given hash is evaluated once
    (repeat calls return the stored result); evaluating a *different* hash for the same
    strategy id sets ``peeking`` and logs a warning.
    """
    h = config_hash(frozen_cfg)
    sid = _strategy_id(frozen_cfg)
    if registry.frozen.get(h) != sid:
        raise PermissionError("config was not frozen before the out-of-sample run")
    prior = registry.evaluated.get((sid, h, cut))
    if prior is not None:
        return prior.model_copy(update={"cached": True})
    peeking = any(k[0] == sid for k in registry.evaluated)
    if peeking:
        registry.peeking.add(sid)
        log.warning(
            "peeking: strategy %s evaluated on out-of-sample with a second config hash", sid
        )
    oos_cfg = frozen_cfg.model_copy(update={"start": cut})
    report = runners.benchmarked(oos_cfg, _canon(bars))
    run = OosRun(strategy_id=sid, config_hash=h, cut=cut, report=report, peeking=peeking)
    registry.evaluated[(sid, h, cut)] = run
    return run


# ---------------------------------------------------------------------------- gate 6


class ForwardStats(BaseModel):
    """Forward paper-trading statistics (same metrics as the backtest)."""

    model_config = ConfigDict(frozen=True)

    since: date
    span_days: int
    metrics: Metrics
    benchmark_metrics: Metrics
    passed: bool


def forward_stats(
    equity: pd.Series,
    benchmark: pd.Series,
    since: date,
    *,
    min_days: int = 90,
    periods_per_year: int = 252,
    rf_annual: float = 0.0,
) -> ForwardStats:
    """Metrics over the live curve from ``since``; passes at >= ``min_days`` calendar days."""
    lo = _ts(since)
    eq = equity.loc[equity.index >= lo]
    bm = benchmark.loc[benchmark.index >= lo]
    if len(eq) < 1:
        raise ValueError("no forward equity points")
    span = (eq.index[-1] - eq.index[0]).days
    zeros = pd.Series(0.0, index=eq.index)
    zeros_b = pd.Series(0.0, index=bm.index)
    return ForwardStats(
        since=since,
        span_days=span,
        metrics=compute_metrics(eq, [], zeros, periods_per_year, rf_annual),
        benchmark_metrics=compute_metrics(bm, [], zeros_b, periods_per_year, rf_annual),
        passed=span >= min_days,
    )


# ---------------------------------------------------------------------------- report + gate


class ValidationReport(BaseModel):
    """Everything the gate needs; ``None`` parts are reported as ``pending``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    strategy_id: str
    history: HistoryCheck
    split: Split
    walk_forward: WalkForwardResult | None = None
    oos: OosRun | None = None
    full_trade_count: int | None = None
    oos_trade_count: int | None = None
    sensitivity: SensitivityResult | None = None
    forward: ForwardStats | None = None
    wf_skip_reason: str | None = None  # walk-forward not run (too few in-sample bars)
    n_trials: int = 0  # backtests run on parameter sets (for a later deflated Sharpe)
    dsr: DsrResult | None = None  # protocol runs only (A24)
    edge: EdgeResult | None = None  # A26(a)
    overlay: OverlayResult | None = None  # A26(b)
    # Daily net returns of the full run (protocol runs); input to the batch-level DSR (A30)
    protocol_returns: list[float] | None = Field(default=None, exclude=True, repr=False)


class CheckResult(BaseModel):
    """One gate: pass/fail/pending with the numbers behind it."""

    model_config = ConfigDict(frozen=True)

    gate: int
    name: str
    status: Status
    detail: str
    values: dict[str, float | int | str | bool | None] = {}


class GateResult(BaseModel):
    """Per-gate results; the verdict is ``advance`` only when all six pass."""

    model_config = ConfigDict(frozen=True)

    checks: list[CheckResult]

    @property
    def verdict(self) -> Literal["advance", "hold"]:
        """``advance`` iff every gate passed."""
        return "advance" if all(c.status == "passed" for c in self.checks) else "hold"

    @property
    def passed(self) -> bool:
        """True iff the verdict is ``advance``."""
        return self.verdict == "advance"

    def by_gate(self, gate: int) -> CheckResult:
        """The check for gate number ``gate`` (1-6)."""
        return next(c for c in self.checks if c.gate == gate)

    def failed_gates(self) -> list[int]:
        """Numbers of the gates that failed."""
        return [c.gate for c in self.checks if c.status == "failed"]


def _ge(a: float, b: float) -> bool:
    return _finite(a) and _finite(b) and a >= b


def evaluate_gate(report: ValidationReport, cfg: GateConfig | None = None) -> GateResult:
    """Apply the six gates (SPEC-002 section 8) to ``report``."""
    c = cfg or GateConfig()
    out: list[CheckResult] = []

    # Gate 1: history + hold-out discipline
    h = report.history
    required = c.min_years_crypto if h.asset_class == "crypto" else c.min_years_equity
    peeked = report.oos is not None and report.oos.peeking
    ok1 = h.span_years >= required and not peeked
    out.append(
        CheckResult(
            gate=1,
            name="history_holdout",
            status="passed" if ok1 else "failed",
            detail=f"span {h.span_years:.2f}y vs {required:g}y required; peeking={peeked}",
            values={"span_years": h.span_years, "required_years": required, "peeking": peeked},
        )
    )

    # Gate 2: walk-forward consistency
    wf = report.walk_forward
    if wf is None:
        out.append(CheckResult(gate=2, name="walk_forward", status="pending", detail="not run"))
    else:
        fs, fd = wf.frac_sharpe_positive(), wf.frac_dd_ok(c.wf_dd_mult)
        ok2 = wf.n_windows >= c.wf_min_windows and fs >= c.wf_sharpe_frac and fd >= c.wf_dd_frac
        out.append(
            CheckResult(
                gate=2,
                name="walk_forward",
                status="passed" if ok2 else "failed",
                detail=(
                    f"{wf.n_windows} windows (min {c.wf_min_windows}); sharpe>0 in {fs:.0%} "
                    f"(need {c.wf_sharpe_frac:.0%}); dd<=bench in {fd:.0%} "
                    f"(need {c.wf_dd_frac:.0%})"
                    + (f"; {report.wf_skip_reason}" if report.wf_skip_reason else "")
                ),
                values={"n_windows": wf.n_windows, "frac_sharpe_pos": fs, "frac_dd_ok": fd},
            )
        )

    # Gate 3: OOS vs benchmark
    oos = report.oos
    if oos is None:
        out.append(CheckResult(gate=3, name="oos_vs_benchmark", status="pending", detail="not run"))
    else:
        rep = oos.report
        key = _bench_key(rep)
        if key is None:
            out.append(
                CheckResult(gate=3, name="oos_vs_benchmark", status="failed", detail="no benchmark")
            )
        else:
            m, b = rep.metrics, rep.benchmark_metrics[key]
            sh = _ge(m.sharpe, b.sharpe)
            so = _ge(m.sortino, b.sortino)
            dd = _finite(m.max_drawdown) and abs(m.max_drawdown) <= c.oos_dd_mult * abs(
                b.max_drawdown
            )
            out.append(
                CheckResult(
                    gate=3,
                    name="oos_vs_benchmark",
                    status="passed" if (sh and so and dd) else "failed",
                    detail=(
                        f"vs {key}: sharpe {m.sharpe:.3f}/{b.sharpe:.3f} {'ok' if sh else 'FAIL'}; "
                        f"sortino {m.sortino:.3f}/{b.sortino:.3f} {'ok' if so else 'FAIL'}; "
                        f"|dd| {abs(m.max_drawdown):.3f}/{abs(b.max_drawdown):.3f} "
                        f"{'ok' if dd else 'FAIL'}"
                    ),
                    values={
                        "benchmark": key,
                        "sharpe_ok": sh,
                        "sortino_ok": so,
                        "drawdown_ok": dd,
                        "sharpe": _num(m.sharpe),
                        "bench_sharpe": _num(b.sharpe),
                    },
                )
            )

    # Gate 4: trade count over the full history
    if report.full_trade_count is None:
        out.append(CheckResult(gate=4, name="trades", status="pending", detail="not run"))
    else:
        n = report.full_trade_count
        out.append(
            CheckResult(
                gate=4,
                name="trades",
                status="passed" if n >= c.min_trades else "failed",
                detail=f"{n} closed round trips (min {c.min_trades}); OOS {report.oos_trade_count}",
                values={"trades": n, "oos_trades": report.oos_trade_count},
            )
        )

    # Gate 5: +-20% sensitivity
    sens = report.sensitivity
    if sens is None:
        out.append(CheckResult(gate=5, name="sensitivity", status="pending", detail="not run"))
    else:
        bad = sens.collapses(c)
        out.append(
            CheckResult(
                gate=5,
                name="sensitivity",
                status="failed" if bad else "passed",
                detail=(
                    f"{len(sens.variants)} variants, {len(sens.skipped)} skipped; collapsed: "
                    + (", ".join(f"{n}({'/'.join(w)})" for n, w in bad) or "none")
                ),
                values={
                    "n_variants": len(sens.variants),
                    "n_collapsed": len(bad),
                    "informational_sharpe": sens.informational,
                },
            )
        )

    # Gate 6: forward paper trading
    fwd = report.forward
    if fwd is None or fwd.span_days < c.forward_min_days:
        out.append(
            CheckResult(
                gate=6,
                name="forward",
                status="pending",
                detail=(
                    "no forward data yet"
                    if fwd is None
                    else f"{fwd.span_days} of {c.forward_min_days} days"
                ),
                values={"span_days": None if fwd is None else fwd.span_days},
            )
        )
    else:
        out.append(
            CheckResult(
                gate=6,
                name="forward",
                status="passed",
                detail=f"{fwd.span_days} days of forward data",
                values={"span_days": fwd.span_days},
            )
        )

    # Gate 7 (protocol only): deflated Sharpe ratio
    if c.protocol:
        d = report.dsr
        if d is None:
            out.append(
                CheckResult(gate=7, name="deflated_sharpe", status="pending", detail="not run")
            )
        else:
            ok7 = _ge(d.dsr, c.dsr_min)
            out.append(
                CheckResult(
                    gate=7,
                    name="deflated_sharpe",
                    status="passed" if ok7 else "failed",
                    detail=(
                        f"DSR {d.dsr:.3f} (need {c.dsr_min:.2f}); N={d.n_trials}, T={d.n_obs}, "
                        f"SR0={d.sr0:.4f}/period"
                    ),
                    values={"dsr": _num(d.dsr), "n_trials": d.n_trials, "sr0": _num(d.sr0)},
                )
            )
    return GateResult(checks=out)


class ProtocolVerdict(BaseModel):
    """ADR-0001 A26 verdict: ``advance`` means "advance to hold-out", never "trade it"."""

    model_config = ConfigDict(frozen=True)

    advance: bool
    reasons: list[str]  # what failed (empty when advancing)
    holm_p: float | None = None


def protocol_verdict(
    report: ValidationReport,
    result: GateResult,
    holm_p: float | None,
    cfg: GateConfig | None = None,
) -> ProtocolVerdict:
    """Gates 1, 2, 4, 5, 7 + Holm-adjusted T-bill edge + SPY overlay value. Gate 3 is excluded."""
    c = cfg or GateConfig.protocol_2026()
    why: list[str] = []
    needed = ((1, "history"), (2, "walk-forward"), (4, "trades"), (5, "sensitivity"))
    for g, name in (*needed, (7, "deflated Sharpe")):
        ok = any(ch.gate == g and ch.status == "passed" for ch in result.checks)
        if not ok:
            why.append(f"gate {g} ({name}) not passed")
    if holm_p is None or not holm_p < c.edge_alpha:
        why.append(f"Holm-adjusted T-bill edge p={holm_p} (need < {c.edge_alpha})")
    ov = report.overlay
    if ov is None or not (
        (math.isfinite(ov.alpha_t) and ov.alpha_t > c.overlay_alpha_t)
        or (
            math.isfinite(ov.sharpe_mix)
            and math.isfinite(ov.sharpe_spy)
            and ov.sharpe_mix > ov.sharpe_spy
        )
    ):
        why.append("overlay adds no value (alpha t <= threshold and 50/50 Sharpe <= SPY)")
    return ProtocolVerdict(advance=not why, reasons=why, holm_p=holm_p)


class BatchEntry(BaseModel):
    """One strategy's batch outcome: Holm-adjusted p, its gates and the protocol verdict."""

    model_config = ConfigDict(frozen=True)

    strategy_id: str
    raw_p: float
    holm_p: float
    gate: GateResult
    verdict: ProtocolVerdict
    dsr: DsrResult | None = None  # the batch-level DSR behind gate 7 (A30)


def _batch_dsr(items: list[ValidationReport], trials: TrialRegistry) -> dict[str, DsrResult]:
    """Gate-7 DSR for every report with ``protocol_returns``, computed together (ADR A30).

    V comes only from protocol trials: this batch plus protocol-flagged registry rows (the
    seeded failures are in N but not V), floored per candidate at the null Sharpe variance
    ``(1 + SR^2/2)/T``. Nothing depends on the order of ``items`` or of registry appends.
    """
    cands = [r for r in items if r.protocol_returns]
    ids = {r.strategy_id for r in items}
    sr = {
        r.strategy_id: deflated_sharpe(r.protocol_returns or [], trial_sharpes=[]).sharpe_period
        for r in cands
    }
    pool = [s for s in sr.values() if math.isfinite(s)] + trials.protocol_sharpes(ids)
    v_pool = float(statistics.variance(pool)) if len(pool) > 1 else 0.0
    n = len({r.id for r in trials.records} | ids)
    out: dict[str, DsrResult] = {}
    for r in cands:
        rets = r.protocol_returns or []
        s = sr[r.strategy_id]
        floor = null_sharpe_variance(s, len(rets)) if math.isfinite(s) else 0.0
        out[r.strategy_id] = deflated_sharpe(
            rets, trial_sharpes=pool, n_trials=n, variance=max(v_pool, floor)
        )
    return out


def evaluate_batch(
    reports: Mapping[str, ValidationReport] | list[ValidationReport],
    cfg: GateConfig | None = None,
    *,
    trials: TrialRegistry | None = None,
    batch: str | None = None,
    record: bool = False,
    as_of: str | None = None,
    periods_per_year: float = 252.0,
) -> dict[str, BatchEntry]:
    """Holm across one batch (one-sided T-bill edge p) + DSR + verdicts, all batch-level.

    With ``trials`` the DSR (gate 7) is computed here for every report that carries
    ``protocol_returns`` (see :func:`_batch_dsr`); without it reports keep whatever ``dsr`` they
    have. ``record=True`` (needs ``trials`` and ``as_of``, an ISO date) then writes each report
    to the registry as a protocol trial (``status`` advanced/failed, an existing id is
    replaced), after all numbers are final so the order cannot matter.
    """
    c = cfg or GateConfig.protocol_2026()
    items = list(reports.values()) if isinstance(reports, Mapping) else list(reports)
    ids = [r.strategy_id for r in items]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate strategy ids in batch")
    if record and trials is None:
        raise ValueError("record=True needs a trial registry")
    if record and not as_of:
        raise ValueError("record=True needs as_of (ISO date of the batch run)")
    if trials is not None:
        dsrs = _batch_dsr(items, trials)
        items = [r.model_copy(update={"dsr": dsrs[r.strategy_id]}) if r.strategy_id in dsrs else r
                 for r in items]  # fmt: skip
    raw = [r.edge.p_value if r.edge is not None else 1.0 for r in items]
    adj = holm_adjust(raw)
    out: dict[str, BatchEntry] = {}
    for r, p, a in zip(items, raw, adj, strict=True):
        gr = evaluate_gate(r, c)
        out[r.strategy_id] = BatchEntry(
            strategy_id=r.strategy_id,
            raw_p=p,
            holm_p=a,
            gate=gr,
            verdict=protocol_verdict(r, gr, a, c),
            dsr=r.dsr,
        )
    if record and trials is not None:
        for r in items:
            d = r.dsr
            sp = d.sharpe_period if d is not None and math.isfinite(d.sharpe_period) else None
            rec = TrialRecord(
                id=r.strategy_id,
                date=as_of or "",
                status="advanced" if out[r.strategy_id].verdict.advance else "failed",
                sharpe_period=sp,
                sharpe_annual=None if sp is None else sp * math.sqrt(periods_per_year),
                n_obs=None if d is None else d.n_obs,
                batch=batch,
                protocol=True,
            )
            if r.strategy_id in trials:
                trials.replace(rec)
            else:
                trials.append(rec)
    return out


# ---------------------------------------------------------------------------- orchestration


def run_validation(
    cfg: BacktestConfig,
    bars: Mapping[str, pd.DataFrame],
    *,
    wf: WalkForwardConfig | None = None,
    oos_frac: float = 0.30,
    sweep_pct: float = 0.20,
    sleeve_id: str | None = None,
    registry: OosRegistry | None = None,
    forward: ForwardStats | None = None,
    gate: GateConfig | None = None,
    runners: Runners = DEFAULT_RUNNERS,
    holdout_start: date | None = None,
    protocol: bool = False,
    spy_bars: pd.DataFrame | None = None,
) -> ValidationReport:
    """Run gates 1-5 end to end: split, history, walk-forward + sweep on IS, OOS once.

    ``cfg.start``/``cfg.end`` bound the whole run (IS + OOS); the split is made on the
    strategy calendar inside that range.

    ``holdout_start`` (A25, normally :data:`HOLDOUT_START`) drops every bar on or after it
    before anything else runs. ``protocol=True`` (A24/A26) also fills ``edge`` and ``overlay`` and
    keeps the daily returns for :func:`evaluate_batch` (the DSR is batch-level, A30; ``dsr`` stays
    None here). SPY comes from ``spy_bars`` or the ``SPY`` frame in ``bars``; a protocol run with
    neither raises ``ValueError`` (A30), instead of silently skipping the overlay.
    """
    canon = _canon(bars)
    if holdout_start is not None:
        lim = _ts(holdout_start)
        canon = {k: v.loc[v.index < lim] for k, v in canon.items()}
        if spy_bars is not None:
            spy_bars = spy_bars.loc[spy_bars.index < lim]
        if cfg.end >= holdout_start:
            cfg = cfg.model_copy(update={"end": holdout_start - timedelta(days=1)})
    if protocol:
        spy_check = spy_bars if spy_bars is not None else canon.get("SPY")
        if spy_check is None or not len(spy_check):
            raise ValueError("protocol=True needs SPY bars: pass spy_bars or include SPY in bars")
    target = _target_sleeve(cfg, sleeve_id)
    assert target.strategy is not None
    full_cal = strategy_calendar(cfg, canon)
    run_cal = full_cal[(full_cal >= _ts(cfg.start)) & (full_cal <= _ts(cfg.end))]
    is_idx, oos_idx = split_oos(run_cal, oos_frac)
    cut = oos_idx[0].date()
    split = Split(
        cut=cut,
        is_start=is_idx[0].date(),
        is_end=is_idx[-1].date(),
        oos_end=oos_idx[-1].date(),
        n_is=len(is_idx),
        n_oos=len(oos_idx),
    )
    g = gate or GateConfig()
    history = check_history(
        full_cal,
        target.strategy.asset_class,
        _warmup(cfg),
        min_years_equity=g.min_years_equity,
        min_years_crypto=g.min_years_crypto,
    )
    view = make_is_view(canon, cut)
    is_cfg = cfg.model_copy(update={"end": split.is_end})
    wfc = wf or WalkForwardConfig.defaults(target.strategy.asset_class)
    need = wfc.train_bars + g.wf_min_windows * wfc.test_bars
    wf_skip: str | None = None
    if split.n_is < need:
        wf_skip = (
            f"insufficient in-sample history: {split.n_is} bars < {need} required "
            f"(train {wfc.train_bars} + {g.wf_min_windows} x test {wfc.test_bars})"
        )
        wf_res = WalkForwardResult(windows=[], n_trials=0)
    else:
        wf_res = walk_forward(is_cfg, view, wfc, sleeve_id=target.sleeve_id, runners=runners)
    sens = sensitivity_sweep(is_cfg, view, sweep_pct, sleeve_id=target.sleeve_id, runners=runners)
    reg = registry if registry is not None else OosRegistry()
    reg.freeze(cfg)
    oos = run_oos(cfg, canon, cut=cut, registry=reg, runners=runners)
    full = runners.fund(cfg, canon)
    edge = overlay = None
    proto_rets: list[float] | None = None
    if protocol:
        rets = daily_returns(full.result.equity)
        ppy = full.result.periods_per_year
        edge = edge_vs_tbills(rets, periods_per_year=ppy)
        spy = spy_bars if spy_bars is not None else canon.get("SPY")
        assert spy is not None
        overlay = spy_overlay(rets, daily_returns(spy["close"]), periods_per_year=ppy)
        proto_rets = [float(x) for x in rets.to_numpy()]
    return ValidationReport(
        strategy_id=_strategy_id(cfg),
        history=history,
        split=split,
        walk_forward=wf_res,
        wf_skip_reason=wf_skip,
        oos=oos,
        full_trade_count=full.metrics.trade_count,
        oos_trade_count=oos.report.metrics.trade_count,
        sensitivity=sens,
        forward=forward,
        n_trials=wf_res.n_trials + 1 + len(sens.variants) + 1,
        edge=edge,
        protocol_returns=proto_rets,
        overlay=overlay,
    )


# ---------------------------------------------------------------------------- hold-out (A25)


def open_holdout(
    cfg: BacktestConfig,
    bars: Mapping[str, pd.DataFrame],
    strategy_id: str,
    registry_path: Path | None = None,
    *,
    batch: str,
    log_path: Path | None = None,
    runners: Runners = DEFAULT_RUNNERS,
) -> BacktestReport:
    """Run ``cfg`` once on ``HOLDOUT_START`` onward (earlier bars give warm-up only).

    ``strategy_id`` must be the config's strategy and pre-registered in the trial registry.
    The opening is written to the hold-out log *before* the run, so a crash still counts.
    Raises :class:`~tradebot.backtest.protocol.HoldoutError` on a second opening of the same
    id or a third finalist in ``batch``.
    """
    if _strategy_id(cfg) != strategy_id:
        raise ValueError(f"cfg is for {_strategy_id(cfg)!r}, not {strategy_id!r}")
    if strategy_id not in TrialRegistry.load(registry_path or DEFAULT_REGISTRY_PATH):
        raise ValueError(f"{strategy_id!r} is not in the trial registry")
    if cfg.end < HOLDOUT_START:
        raise ValueError(f"cfg.end {cfg.end} is before the hold-out start {HOLDOUT_START}")
    log = HoldoutLog(log_path or DEFAULT_HOLDOUT_LOG)
    entry: dict[str, object] = {
        "id": strategy_id,
        "batch": batch,
        "config_hash": config_hash(cfg),
        "start": HOLDOUT_START.isoformat(),
        "end": cfg.end.isoformat(),
    }
    log.record(entry)
    report = runners.benchmarked(cfg.model_copy(update={"start": HOLDOUT_START}), _canon(bars))
    m = report.metrics
    entry.update(
        {
            "sharpe": _num(m.sharpe),
            "total_return": _num(m.total_return),
            "max_drawdown": _num(m.max_drawdown),
            "trades": m.trade_count,
        }
    )
    log.save()
    return report
