"""Protocol wiring in validation.py: GateConfig/WF presets, hold-out truncation, batch verdicts."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from golden.harness import engine_for

from tradebot.backtest.engine import (
    BacktestConfig,
    SleeveSpec,
    run_with_benchmark,
)
from tradebot.backtest.protocol import (
    DsrResult,
    EdgeResult,
    HoldoutError,
    OverlayResult,
    null_sharpe_variance,
)
from tradebot.backtest.registry import TrialRecord, TrialRegistry
from tradebot.backtest.validation import (
    HOLDOUT_START,
    GateConfig,
    Runners,
    ValidationReport,
    WalkForwardConfig,
    evaluate_batch,
    evaluate_gate,
    open_holdout,
    run_fund_report,
    run_validation,
)
from tradebot.strategy.schema import RulesSpec


def _spec() -> RulesSpec:
    return RulesSpec.model_validate(
        {
            "id": "trend_p",
            "name": "SMA trend",
            "asset_class": "equity",
            "universe": ["SPY"],
            "schedule": {"freq": "daily"},
            "parameters": {"trend_len": 20},
            "entry": {"gt": [{"price": "close"}, {"ind": "sma", "period": "$trend_len"}]},
            "position": {"weight": 1.0},
        }
    )


def _market() -> dict[str, pd.DataFrame]:
    idx = pd.bdate_range("2022-01-03", "2025-06-30", tz="UTC")
    rng = np.random.default_rng(5)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, len(idx)))
    open_ = np.concatenate([[100.0], close[:-1]])
    df = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.003,
            "low": np.minimum(open_, close) * 0.997,
            "close": close,
            "volume": 1e6,
        },
        index=idx,
    )
    return {"SPY": df}


def _cfg(end: date = date(2025, 6, 30)) -> BacktestConfig:
    eng = engine_for("mean_reversion")
    eng = eng.model_copy(
        update={"sleeves": (eng.sleeves[0].model_copy(update={"sleeve_id": "s"}),)}
    )
    return BacktestConfig(
        sleeves=[SleeveSpec(sleeve_id="s", strategy=_spec())],
        start=date(2022, 3, 1),
        end=end,
        engine=eng,
    )


class Spy:
    def __init__(self) -> None:
        self.last: list[pd.Timestamp] = []

    def runners(self) -> Runners:
        def fund(cfg, bars):  # type: ignore[no-untyped-def]
            self.last.append(max(df.index[-1] for df in bars.values()))
            return run_fund_report(cfg, bars)

        def bench(cfg, bars):  # type: ignore[no-untyped-def]
            self.last.append(max(df.index[-1] for df in bars.values()))
            return run_with_benchmark(cfg, bars)

        return Runners(fund=fund, benchmarked=bench)


def test_presets_and_defaults() -> None:
    g = GateConfig.protocol_2026()
    assert (g.min_trades, g.min_years_equity, g.min_years_crypto, g.wf_min_windows) == (
        60, 7.0, 5.0, 3,
    )  # fmt: skip
    assert g.protocol and g.dsr_min == 0.95
    d = GateConfig()
    assert (d.min_trades, d.min_years_equity, d.wf_min_windows, d.protocol) == (30, 10.0, 4, False)
    wf = WalkForwardConfig.protocol_2026("equity", 26)
    assert (wf.train_bars, wf.test_bars, wf.step_bars) == (504 * 26, 252 * 26, 252 * 26)
    assert WalkForwardConfig.protocol_2026("crypto").train_bars == 504
    assert WalkForwardConfig.defaults("equity").train_bars == 756  # unchanged


def test_holdout_start_constant() -> None:
    assert HOLDOUT_START == date(2024, 1, 1)


def test_no_bar_from_the_holdout_reaches_the_strategy() -> None:
    market = _market()
    assert market["SPY"].index[-1] >= pd.Timestamp(HOLDOUT_START, tz="UTC")
    spy = Spy()
    rep = run_validation(
        _cfg(),
        market,
        wf=WalkForwardConfig(train_bars=200, test_bars=100, step_bars=100),
        runners=spy.runners(),
        holdout_start=HOLDOUT_START,
    )
    assert spy.last and max(spy.last) < pd.Timestamp(HOLDOUT_START, tz="UTC")
    assert rep.split.oos_end < HOLDOUT_START
    # default (no holdout_start) is untouched: the full history is used
    spy2 = Spy()
    run_validation(
        _cfg(),
        market,
        wf=WalkForwardConfig(train_bars=200, test_bars=100, step_bars=100),
        runners=spy2.runners(),
    )
    assert max(spy2.last) > pd.Timestamp(HOLDOUT_START, tz="UTC")


def test_protocol_run_fills_edge_overlay_and_returns_then_batch_fills_gate_seven() -> None:
    market = _market()
    trials = TrialRegistry(
        [TrialRecord(id=f"t{i}", date="2026-10-23", sharpe_period=0.01 * i) for i in range(4)]
    )
    rep = run_validation(
        _cfg(),
        market,
        wf=WalkForwardConfig(train_bars=200, test_bars=100, step_bars=100),
        holdout_start=HOLDOUT_START,
        protocol=True,
    )
    assert rep.edge is not None and rep.overlay is not None and rep.protocol_returns
    assert rep.dsr is None  # the DSR is batch-level (A30)
    out = evaluate_batch([rep], trials=trials)
    d = out[rep.strategy_id].dsr
    assert d is not None and d.n_trials == 5  # 4 registered + the candidate
    assert out[rep.strategy_id].gate.by_gate(7).status in ("passed", "failed")
    # default gate config adds no gate 7
    assert all(c.gate != 7 for c in evaluate_gate(rep).checks)


def test_protocol_run_without_spy_raises() -> None:
    market = {k: v for k, v in _market().items() if k != "SPY"}
    with pytest.raises(ValueError, match="SPY"):
        run_validation(_cfg(), market, protocol=True, holdout_start=HOLDOUT_START)


def _rets_report(name: str, seed: int, drift: float) -> ValidationReport:
    rng = np.random.default_rng(seed)
    rets = (rng.normal(drift, 0.01, 1500)).tolist()
    base = ValidationReport.model_construct(strategy_id=name, protocol_returns=rets)
    return base


def test_batch_dsr_is_order_independent_floored_and_ignores_seeded_rows() -> None:
    seeded = [
        TrialRecord(id=f"seed{i}", date="2026-10-23", status="failed", sharpe_period=-0.2 - i / 10)
        for i in range(4)
    ]
    reps = [_rets_report("a", 1, 0.0006), _rets_report("b", 2, 0.0003), _rets_report("c", 3, 0.0)]

    def run(order: list[int]):
        reg = TrialRegistry(list(seeded))
        out = _batch_dsr_only([reps[i] for i in order], reg)
        return out, reg

    o1, _ = run([0, 1, 2])
    o2, _ = run([2, 0, 1])
    for k in "abc":
        assert o1[k].dsr == pytest.approx(o2[k].dsr, abs=1e-15)
        assert o1[k].n_trials == 7  # 4 seeded + 3 batch
    # seeded failures (Sharpe -0.2..-0.5) would blow V up to ~0.02; the floor is ~1/T = 7e-4
    for d in o1.values():
        assert d.var_trial_sharpes < 0.002
        assert d.var_trial_sharpes >= null_sharpe_variance(d.sharpe_period, d.n_obs) - 1e-15


def _batch_dsr_only(items, reg):  # type: ignore[no-untyped-def]
    from tradebot.backtest.validation import _batch_dsr

    return _batch_dsr(items, reg)


def test_batch_variance_floor_applies_when_batch_sharpes_are_identical() -> None:
    r = _rets_report("a", 1, 0.0005)
    twin = ValidationReport.model_construct(strategy_id="b", protocol_returns=r.protocol_returns)
    out = _batch_dsr_only([r, twin], TrialRegistry([]))
    d = out["a"]
    assert d.var_trial_sharpes == pytest.approx(null_sharpe_variance(d.sharpe_period, d.n_obs))


def test_batch_record_appends_protocol_trials_and_counts_toward_later_v(tmp_path: Path) -> None:
    reg = TrialRegistry([], tmp_path / "r.json")
    base = run_validation(
        _cfg(),
        _market(),
        wf=WalkForwardConfig(train_bars=200, test_bars=100, step_bars=100),
        holdout_start=HOLDOUT_START,
    )
    reps = [
        base.model_copy(
            update={"strategy_id": n, "protocol_returns": _rets_report(n, i, d).protocol_returns}
        )
        for n, i, d in (("a", 1, 0.0006), ("b", 2, 0.0003))
    ]
    out = evaluate_batch(
        reps, GateConfig.protocol_2026(), trials=reg, batch="b1", record=True, as_of="2026-10-24"
    )
    assert set(out) == {"a", "b"}
    again = TrialRegistry.load(tmp_path / "r.json")
    assert [r.id for r in again.records] == ["a", "b"]
    assert all(r.protocol and r.batch == "b1" and r.sharpe_period for r in again.records)
    assert len(again.protocol_sharpes()) == 2
    assert len(again.protocol_sharpes({"a"})) == 1


def test_evaluate_batch_applies_holm_across_the_batch() -> None:
    market = _market()
    base = run_validation(
        _cfg(),
        market,
        wf=WalkForwardConfig(train_bars=200, test_bars=100, step_bars=100),
        holdout_start=HOLDOUT_START,
    )

    def variant(name: str, p: float) -> ValidationReport:
        return base.model_copy(
            update={
                "strategy_id": name,
                "edge": EdgeResult(n=500, mean_daily=1e-3, mean_annual=0.25, t_stat=3.0, p_value=p),
                "dsr": DsrResult(
                    dsr=0.99,
                    sharpe_period=0.1,
                    sr0=0.05,
                    n_trials=5,
                    n_obs=500,
                    skew=0.0,
                    kurtosis=3.0,
                    var_trial_sharpes=0.001,
                ),  # fmt: skip
                "overlay": OverlayResult(
                    n=500, alpha_annual=0.05, alpha_t=3.0, beta=0.2, sharpe_spy=0.5, sharpe_mix=0.7
                ),
            }
        )

    out = evaluate_batch([variant("a", 0.02), variant("b", 0.03), variant("c", 0.30)])
    assert out["a"].holm_p == pytest.approx(0.06)  # 3 * 0.02: raw 0.02 is no longer < 0.05
    assert out["b"].holm_p == pytest.approx(0.06) and out["c"].holm_p == pytest.approx(0.30)
    assert not out["a"].verdict.advance
    assert any("Holm" in r for r in out["a"].verdict.reasons)
    solo = evaluate_batch({"a": variant("a", 0.02)})
    assert solo["a"].holm_p == pytest.approx(0.02)
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_batch([variant("a", 0.1), variant("a", 0.2)])


def test_open_holdout_runs_from_2024_and_refuses_repeats(tmp_path: Path) -> None:
    market = _market()
    cfg = _cfg()
    reg_path, log_path = tmp_path / "reg.json", tmp_path / "log.json"
    reg = TrialRegistry.load(reg_path)
    for i in ("trend_p", "other"):
        reg.append(TrialRecord(id=i, date="2026-10-23"))
    rn = Spy().runners()
    rep = open_holdout(cfg, market, "trend_p", reg_path, batch="b1", log_path=log_path, runners=rn)
    assert rep.result.equity.index[0] >= pd.Timestamp(HOLDOUT_START, tz="UTC")
    with pytest.raises(HoldoutError, match="already opened"):
        open_holdout(cfg, market, "trend_p", reg_path, batch="b1", log_path=log_path, runners=rn)
    with pytest.raises(ValueError, match="not in the trial registry"):
        open_holdout(cfg, market, "trend_p", tmp_path / "empty.json", batch="b1", log_path=log_path)
    with pytest.raises(ValueError, match="cfg is for"):
        open_holdout(cfg, market, "other", reg_path, batch="b1", log_path=log_path)


def test_open_holdout_refuses_a_third_finalist_in_a_batch(tmp_path: Path) -> None:
    market = _market()
    cfg = _cfg()
    reg_path, log_path = tmp_path / "reg.json", tmp_path / "log.json"
    TrialRegistry.load(reg_path).append(TrialRecord(id="trend_p", date="2026-10-23"))
    log_path.write_text(
        '[{"id": "x", "batch": "b1"}, {"id": "y", "batch": "b1"}, {"id": "z", "batch": "b0"}]'
    )
    with pytest.raises(HoldoutError, match="batch"):
        open_holdout(cfg, market, "trend_p", reg_path, batch="b1", log_path=log_path)
    # nothing was recorded by the refused attempt, and another batch is still open
    open_holdout(cfg, market, "trend_p", reg_path, batch="b2", log_path=log_path)
