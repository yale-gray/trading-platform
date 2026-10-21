"""Hand-built reports and stand-in backtesters for the gate tests (SPEC-005 section 7).

``fake_report`` builds a ``BacktestReport`` around given ``Metrics`` so criteria can be tested
at their exact edges. ``Memorizer`` is the test-only overfit strategy: an injected backtester
(``Runners``) that holds a position on every in-sample day whose *future* return is positive.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
from golden.harness import engine_for

from tradebot.backtest.engine import (
    BacktestConfig,
    BacktestReport,
    BacktestResult,
    SleeveSpec,
    config_hash,
)
from tradebot.backtest.metrics import Metrics, compute_metrics
from tradebot.backtest.trades import ClosedTrade
from tradebot.backtest.validation import Runners, WalkForwardConfig, strategy_calendar
from tradebot.strategy.schema import RulesSpec

# TEST-ONLY walk-forward shrinks. Production defaults (756/252/252 equity, 548/182/182 crypto)
# live in ``WalkForwardConfig.defaults`` and must never be replaced by these.
FIXTURE_WF_EQUITY = WalkForwardConfig(train_bars=350, test_bars=150, step_bars=150)
FIXTURE_WF_CRYPTO = WalkForwardConfig(train_bars=365, test_bars=180, step_bars=180)
SYNTH_WF = WalkForwardConfig(train_bars=500, test_bars=250, step_bars=250)  # 12y synthetic


def metrics(
    *,
    sharpe: float = 1.0,
    sortino: float = 1.5,
    max_drawdown: float = -0.10,
    total_return: float = 0.5,
    trade_count: int = 40,
) -> Metrics:
    return Metrics(
        total_return=total_return,
        cagr=0.05,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        sortino=sortino,
        win_rate=0.5,
        trade_count=trade_count,
        exposure_avg=0.5,
        time_in_market=0.5,
        periods_per_year=252,
    )


def _result(equity: pd.Series, trades: list[ClosedTrade] | None = None) -> BacktestResult:
    return BacktestResult(
        config_hash="0" * 64,
        data_fingerprint="0" * 64,
        periods_per_year=252,
        equity=equity,
        exposure=equity * 0.0,
        trades=trades or [],
        open_positions=[],
        killed_on=None,
        ledger=[],
        n_signals=0,
    )


def fake_report(fund: Metrics, bench: Metrics, key: str = "SPY") -> BacktestReport:
    eq = pd.Series([1.0, 1.0], index=pd.date_range("2020-01-01", periods=2, tz="UTC"))
    return BacktestReport(
        result=_result(eq),
        metrics=fund,
        benchmark_metrics={key: bench},
        benchmark_equity={key: eq},
        deltas={},
    )


def dummy_spec(offset: float = 1.0, sid: str = "memorizer") -> RulesSpec:
    """A schema-valid rules spec whose only parameter is the memorizer's look-ahead offset."""
    return RulesSpec.model_validate(
        {
            "id": sid,
            "name": "memorizer",
            "asset_class": "equity",
            "universe": ["SPY"],
            "schedule": {"freq": "daily"},
            "parameters": {"offset": offset},
            "entry": {"gt": [{"price": "close"}, "$offset"]},
            "position": {"weight": 1.0},
        }
    )


def single_sleeve_config(
    spec: RulesSpec, start: date, end: date, sleeve_id: str = "s"
) -> BacktestConfig:
    eng = engine_for("mean_reversion")
    eng = eng.model_copy(
        update={"sleeves": (eng.sleeves[0].model_copy(update={"sleeve_id": sleeve_id}),)}
    )
    return BacktestConfig(
        sleeves=[SleeveSpec(sleeve_id=sleeve_id, strategy=spec)], start=start, end=end, engine=eng
    )


@dataclass(frozen=True)
class Memorizer:
    """Overfit lookup strategy: in sample hold where ``ret[t + offset] > 0`` (peeks at the future).

    Out of sample (from ``cut``) it follows a fixed pseudo-random schedule, as the spec describes.
    ``offset`` is the one parameter the sensitivity sweep perturbs: any offset other than the
    memorised one destroys the in-sample edge.
    """

    cut: date
    seed: int = 7

    def _positions(self, close: pd.Series, offset: int) -> np.ndarray:
        ret = close.pct_change().to_numpy()
        n = len(close)
        pos = np.zeros(n)
        for t in range(n):
            if t + offset < n:
                pos[t] = 1.0 if ret[t + offset] > 0 else 0.0
        rng = np.random.default_rng(self.seed)
        sched = (rng.random(n) < 0.5).astype(float)
        is_oos = np.asarray(close.index >= pd.Timestamp(self.cut, tz="UTC"))
        return np.where(is_oos, sched, pos)

    def backtest(
        self, cfg: BacktestConfig, bars: Mapping[str, pd.DataFrame], bench: bool
    ) -> BacktestReport:
        """Equity of holding SPY per ``_positions`` over [cfg.start, cfg.end]; no engine."""
        spec = cfg.sleeves[0].strategy
        assert spec is not None
        offset = int(spec.resolve().parameters["offset"])
        full = bars["SPY"]["close"]
        pos = pd.Series(self._positions(full, offset), index=full.index)
        lo, hi = pd.Timestamp(cfg.start, tz="UTC"), pd.Timestamp(cfg.end, tz="UTC")
        sel = (full.index >= lo) & (full.index <= hi)
        close = full[sel]
        p = pos[sel]
        held = p.shift(1).fillna(0.0)  # decided at the prior close, earns today's return
        rets = close.pct_change().fillna(0.0) * held
        eq = 100000.0 * (1.0 + rets).cumprod()
        trades: list[ClosedTrade] = []
        entry: int | None = None
        arr = held.to_numpy()
        for i, flag in enumerate(arr):
            if flag and entry is None:
                entry = i
            if (not flag or i == len(arr) - 1) and entry is not None:
                a, b = close.iloc[entry], close.iloc[i]
                trades.append(
                    ClosedTrade(
                        sleeve_id="s",
                        symbol="SPY",
                        entry_asof=close.index[entry].date(),
                        exit_asof=close.index[i].date(),
                        qty=Decimal(1),
                        entry_price=Decimal(str(round(a, 4))),
                        exit_price=Decimal(str(round(b, 4))),
                        fees=Decimal(0),
                        net_pnl=Decimal(str(round(b - a, 4))),
                        exit_reason="signal",
                    )
                )
                entry = None
        expo = held.astype(float)
        m = compute_metrics(eq, trades, expo, 252, cfg.rf_annual)
        res = _result(eq, trades).model_copy(update={"config_hash": config_hash(cfg)})
        if not bench:
            return BacktestReport(
                result=res, metrics=m, benchmark_metrics={}, benchmark_equity={}, deltas={}
            )
        beq = 100000.0 * close / close.iloc[0]
        bm = compute_metrics(beq, [], beq * 0.0 + 1.0, 252, cfg.rf_annual)
        return BacktestReport(
            result=res,
            metrics=m,
            benchmark_metrics={"SPY": bm},
            benchmark_equity={"SPY": beq},
            deltas={},
        )

    def runners(self) -> Runners:
        return Runners(
            fund=lambda c, b: self.backtest(c, b, False),
            benchmarked=lambda c, b: self.backtest(c, b, True),
        )


def cut_of(cfg: BacktestConfig, bars: Mapping[str, pd.DataFrame], oos_frac: float = 0.30) -> date:
    """The 70/30 cut date on the strategy calendar inside ``cfg``'s window."""
    from tradebot.backtest.validation import split_oos

    cal = strategy_calendar(cfg, bars)
    cal = cal[(cal >= pd.Timestamp(cfg.start, tz="UTC")) & (cal <= pd.Timestamp(cfg.end, tz="UTC"))]
    return split_oos(cal, oos_frac)[1][0].date()
