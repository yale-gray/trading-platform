"""Validation-gate tests (SPEC-005 section 7, SPEC-002 section 8, AC 10-12; T18).

Synthetic seeded data from ``gate_synth``. Criteria are tested at their exact edges with
hand-built reports; the end-to-end paths run the real engine (positive control, tuned walk-forward,
a built-in sleeve on the fixtures) or, for the overfit "memorizer", an injected backtester.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from gate_fakes import (
    FIXTURE_WF_EQUITY,
    SYNTH_WF,
    Memorizer,
    cut_of,
    dummy_spec,
    fake_report,
    metrics,
    single_sleeve_config,
)
from gate_synth import business_index, random_walk, regime_market
from golden import harness as h

from tradebot.backtest.metrics import Metrics
from tradebot.backtest.validation import (
    GateConfig,
    GridSpec,
    HistoryCheck,
    OosRegistry,
    OosRun,
    Runners,
    SensitivityResult,
    Split,
    ValidationReport,
    Variant,
    WalkForwardConfig,
    WalkForwardResult,
    check_history,
    evaluate_gate,
    forward_stats,
    make_is_view,
    run_validation,
    sensitivity_sweep,
    split_oos,
    walk_forward,
)
from tradebot.backtest.validation import WindowResult as WR
from tradebot.strategy.io import load_strategy
from tradebot.strategy.schema import RulesSpec

SPLIT = Split(
    cut=date(2020, 1, 1),
    is_start=date(2010, 1, 1),
    is_end=date(2019, 12, 31),
    oos_end=date(2022, 1, 1),
    n_is=100,
    n_oos=40,
)


def _oos(fund: Metrics, bench: Metrics) -> OosRun:
    return OosRun(
        strategy_id="x",
        config_hash="a" * 64,
        cut=SPLIT.cut,
        report=fake_report(fund, bench),
        peeking=False,
    )


def _wf(sharpes: list[float], dd: float = -0.05, bench_dd: float = -0.10) -> WalkForwardResult:
    wins = [
        WR(
            k=i,
            train_start=date(2010, 1, 1),
            train_end=date(2011, 1, 1),
            test_start=date(2011, 1, 2),
            test_end=date(2012, 1, 1),
            params=None,
            sharpe=s,
            total_return=0.1,
            max_drawdown=dd,
            bench_max_drawdown=bench_dd,
        )
        for i, s in enumerate(sharpes)
    ]
    return WalkForwardResult(windows=wins, n_trials=len(wins))


def _sens(base: Metrics, *variants: Metrics) -> SensitivityResult:
    return SensitivityResult(
        pct=0.2,
        base=base,
        variants=[Variant(name=f"v{i}", overrides={}, metrics=m) for i, m in enumerate(variants)],
        skipped=[],
    )


def _report(**kw: object) -> ValidationReport:
    parts: dict[str, object] = {
        "strategy_id": "x",
        "history": HistoryCheck(
            asset_class="equity", span_years=11.0, required_years=10.0, passed=True
        ),
        "split": SPLIT,
        "walk_forward": _wf([1, 1, 1, 1, -1]),
        "oos": _oos(metrics(), metrics(sharpe=0.9, sortino=1.4, max_drawdown=-0.12)),
        "full_trade_count": 40,
        "oos_trade_count": 12,
        "sensitivity": _sens(metrics(), metrics(sharpe=0.8)),
        "forward": None,
    }
    parts.update(kw)
    return ValidationReport(**parts)  # type: ignore[arg-type]


# ------------------------------------------------------------------ split + history


def test_split_oos_is_70_30_on_the_strategy_calendar() -> None:
    idx = business_index(12)
    is_idx, oos_idx = split_oos(idx)
    k = int(np.floor(len(idx) * 0.7))
    assert len(is_idx) == k and oos_idx[0] == idx[k]
    assert is_idx[-1] < oos_idx[0] and len(is_idx) + len(oos_idx) == len(idx)
    assert len(split_oos(pd.date_range("2020-01-01", periods=10, tz="UTC"))[0]) == 7


def test_history_gate_nine_years_of_stock_fails_ten_passes() -> None:
    assert not check_history(business_index(9.0), "equity").passed
    assert check_history(business_index(10.6), "equity").passed
    # warm-up bars are not counted toward the span
    assert not check_history(business_index(10.1), "equity", warmup_bars=200).passed


def test_history_gate_four_and_a_half_years_of_crypto_fails() -> None:
    day = lambda y: pd.date_range("2018-01-01", periods=int(365 * y), freq="D", tz="UTC")  # noqa: E731
    assert not check_history(day(4.5), "crypto").passed
    assert check_history(day(5.6), "crypto").passed


def test_gate1_fails_on_short_history_and_names_it() -> None:
    short = HistoryCheck(asset_class="equity", span_years=9.0, required_years=10.0, passed=False)
    res = evaluate_gate(_report(history=short))
    assert res.by_gate(1).status == "failed" and 1 in res.failed_gates()


# ------------------------------------------------------------------ per-criterion edges


def test_all_gates_pass_and_forward_pending_gives_hold() -> None:
    res = evaluate_gate(_report())
    assert [c.status for c in res.checks] == ["passed"] * 5 + ["pending"]
    assert res.verdict == "hold" and not res.passed


def test_trade_count_29_fails_30_passes() -> None:
    assert evaluate_gate(_report(full_trade_count=29)).by_gate(4).status == "failed"
    assert evaluate_gate(_report(full_trade_count=30)).by_gate(4).status == "passed"


def test_oos_sharpe_equal_to_benchmark_passes_just_below_fails() -> None:
    bench = metrics(sharpe=0.9, sortino=1.4, max_drawdown=-0.12)
    equal = _oos(metrics(sharpe=0.9, sortino=1.4, max_drawdown=-0.12), bench)
    below = _oos(metrics(sharpe=0.8999, sortino=1.4, max_drawdown=-0.12), bench)
    assert evaluate_gate(_report(oos=equal)).by_gate(3).status == "passed"
    assert evaluate_gate(_report(oos=below)).by_gate(3).status == "failed"
    sortino_below = _oos(metrics(sharpe=0.9, sortino=1.399, max_drawdown=-0.12), bench)
    assert evaluate_gate(_report(oos=sortino_below)).by_gate(3).status == "failed"


def test_oos_drawdown_101_percent_of_benchmark_fails() -> None:
    bench = metrics(sharpe=0.5, sortino=0.5, max_drawdown=-0.20)
    ok = _oos(metrics(sharpe=0.9, sortino=1.0, max_drawdown=-0.20), bench)
    bad = _oos(metrics(sharpe=0.9, sortino=1.0, max_drawdown=-0.202), bench)
    assert evaluate_gate(_report(oos=ok)).by_gate(3).status == "passed"
    assert evaluate_gate(_report(oos=bad)).by_gate(3).status == "failed"


def test_nan_oos_metrics_fail_rather_than_pass() -> None:
    bench = metrics()
    nan = _oos(metrics(sharpe=float("nan")), bench)
    assert evaluate_gate(_report(oos=nan)).by_gate(3).status == "failed"


def test_walk_forward_fractions_and_window_count() -> None:
    assert evaluate_gate(_report(walk_forward=_wf([1, 1, 1, -1, -1]))).by_gate(2).status == "passed"
    assert (
        evaluate_gate(_report(walk_forward=_wf([1, 1, -1, -1, -1]))).by_gate(2).status == "failed"
    )
    assert evaluate_gate(_report(walk_forward=_wf([1, 1, 1]))).by_gate(2).status == "failed"
    worse_dd = _wf([1, 1, 1, 1, 1], dd=-0.2, bench_dd=-0.1)
    assert evaluate_gate(_report(walk_forward=worse_dd)).by_gate(2).status == "failed"
    relaxed = GateConfig(wf_min_windows=3)
    assert (
        evaluate_gate(_report(walk_forward=_wf([1, 1, 1])), relaxed).by_gate(2).status == "passed"
    )


def test_sensitivity_collapse_rules() -> None:
    base = metrics(sharpe=1.0, max_drawdown=-0.10, total_return=0.5)
    ok = _sens(
        base, metrics(sharpe=0.5, max_drawdown=-0.15, total_return=0.2)
    )  # exactly on the edges
    assert evaluate_gate(_report(sensitivity=ok)).by_gate(5).status == "passed"
    for bad in (
        metrics(sharpe=0.49, max_drawdown=-0.10),
        metrics(sharpe=1.0, max_drawdown=-0.151),
        metrics(sharpe=1.0, max_drawdown=-0.10, total_return=-0.01),
    ):
        assert evaluate_gate(_report(sensitivity=_sens(base, bad))).by_gate(5).status == "failed"
    informational = _sens(
        metrics(sharpe=-0.2, total_return=0.1), metrics(sharpe=-0.9, total_return=0.1)
    )
    assert informational.informational and informational.passed()


def test_missing_parts_are_pending_not_passed() -> None:
    res = evaluate_gate(
        _report(walk_forward=None, oos=None, full_trade_count=None, sensitivity=None)
    )
    assert [c.status for c in res.checks] == [
        "passed",
        "pending",
        "pending",
        "pending",
        "pending",
        "pending",
    ]
    assert res.verdict == "hold"


def test_forward_gate_pending_until_90_days_then_advance() -> None:
    idx = pd.date_range("2025-01-01", periods=120, freq="D", tz="UTC")
    eq = pd.Series(np.linspace(100.0, 110.0, 120), index=idx)
    bm = pd.Series(np.linspace(100.0, 105.0, 120), index=idx)
    young = forward_stats(eq.iloc[:90], bm.iloc[:90], date(2025, 1, 1))  # 89 days span
    assert young.span_days == 89 and not young.passed
    assert evaluate_gate(_report(forward=young)).by_gate(6).status == "pending"
    old = forward_stats(eq.iloc[:91], bm.iloc[:91], date(2025, 1, 1))
    assert old.span_days == 90 and old.passed
    res = evaluate_gate(_report(forward=old))
    assert res.by_gate(6).status == "passed" and res.verdict == "advance" and res.passed
    assert forward_stats(eq, bm, date(2025, 3, 1)).span_days == 60


# ------------------------------------------------------------------ sensitivity sweep rules


def _stub_runners(
    by_override: dict[tuple[tuple[str, float], ...], Metrics], default: Metrics
) -> tuple[Runners, list]:
    seen: list[dict[str, float]] = []

    def fund(cfg, bars):  # type: ignore[no-untyped-def]
        spec = cfg.sleeves[0].strategy
        seen.append(dict(spec.parameters))
        key = tuple(sorted(spec.parameters.items()))
        return fake_report(by_override.get(key, default), default)

    return Runners(fund=fund, benchmarked=fund), seen


def test_sweep_variants_integer_float_constraints_and_atr_skip() -> None:
    spec = load_strategy(h.STRATEGIES / "crypto_trend.yaml")  # fast 20, slow 50, atr 14, mult 2.5
    cfg = h.golden_config("G3")
    view = make_is_view(h.load_bars(), date(2022, 1, 3))
    runners, seen = _stub_runners({}, metrics())
    res = sensitivity_sweep(cfg, view, runners=runners)
    names = {v.name: v.overrides for v in res.variants}
    assert names["fast=low"] == {"fast": 16.0} and names["fast=high"] == {"fast": 24.0}
    assert names["slow=low"] == {"slow": 40.0} and names["slow=high"] == {"slow": 60.0}
    assert names["stop_mult=low"] == {"stop_mult": 2.0} and names["stop_mult=high"] == {
        "stop_mult": 3.0
    }
    assert names["corner=low"] == {
        "fast": 16.0,
        "slow": 40.0,
        "stop_mult": 2.0,
    }  # atr_len stays at base
    assert names["corner=high"] == {"fast": 24.0, "slow": 60.0, "stop_mult": 3.0}
    assert {n for n, _ in res.skipped} == {"atr_len=low", "atr_len=high"}
    assert all("atr" in why for _, why in res.skipped)
    assert len(seen) == 1 + len(res.variants)  # base + variants, one at a time
    assert spec.parameters["fast"] == 20.0


def test_sweep_integer_rounding_and_constraint_skip() -> None:
    spec = RulesSpec.model_validate(
        {
            "id": "t",
            "name": "t",
            "asset_class": "equity",
            "universe": ["SPY"],
            "schedule": {"freq": "daily"},
            "parameters": {"rsi_len": 2, "max_hold": 5, "top": 3, "fast": 10, "slow": 11, "x": 0.5},
            "constraints": ["fast < slow"],
            "entry": {
                "all": [
                    {"lt": [{"ind": "rsi", "period": "$rsi_len"}, 10]},
                    {"gt": [{"ind": "sma", "period": "$fast"}, {"ind": "sma", "period": "$slow"}]},
                    {"gt": [1, "$top"]},
                    {"gt": [1, "$x"]},
                ]
            },
            "exit": {"ge": [{"pos": "bars_held"}, "$max_hold"]},
            "position": {"weight": 1.0},
        }
    )
    cfg = single_sleeve_config(spec, date(2012, 1, 3), date(2013, 1, 3))
    view = make_is_view({"SPY": random_walk(1, 4)}, date(2013, 1, 4))
    runners, _ = _stub_runners({}, metrics())
    res = sensitivity_sweep(cfg, view, runners=runners)
    got = {v.name: v.overrides for v in res.variants}
    assert got["rsi_len=low"] == {"rsi_len": 1.0} and got["rsi_len=high"] == {"rsi_len": 3.0}
    assert got["max_hold=low"] == {"max_hold": 4.0} and got["max_hold=high"] == {"max_hold": 6.0}
    # "top" sits in a numeric comparison here, not an integer slot: exact 0.8v / 1.2v
    assert got["top=low"] == {"top": pytest.approx(2.4)}
    assert got["top=high"] == {"top": pytest.approx(3.6)}
    assert got["x=low"] == {"x": pytest.approx(0.4)} and got["x=high"] == {"x": pytest.approx(0.6)}
    skipped = dict(res.skipped)
    assert (
        "slow=low" in skipped and "constraint" in skipped["slow=low"]
    )  # fast 10 < slow 8 is false
    assert "fast=high" in skipped  # fast 12 < slow 11 is false


def test_sweep_flags_a_deliberately_fragile_strategy() -> None:
    base = metrics(sharpe=1.5)
    cfg = single_sleeve_config(dummy_spec(5.0), date(2012, 1, 3), date(2013, 1, 3))
    view = make_is_view({"SPY": random_walk(1, 4)}, date(2013, 1, 4))
    fragile, _ = _stub_runners({(("offset", 6.0),): metrics(sharpe=0.2)}, base)
    res = sensitivity_sweep(cfg, view, runners=fragile)
    assert not res.passed() and res.collapses()[0][0] in {"offset=high", "corner=high"}
    robust, _ = _stub_runners({}, base)
    assert sensitivity_sweep(cfg, view, runners=robust).passed()


# ---- the memorizer (negative control)


def test_memorizer_is_rejected_on_oos_and_robustness() -> None:
    """Seed 2 random walk; the memorizer peeks at in-sample futures and is random out of sample."""
    bars = {"SPY": random_walk(seed=2, years=12.0, drift=0.0, vol=0.01)}
    cfg = single_sleeve_config(
        dummy_spec(1.0), bars["SPY"].index[1].date(), bars["SPY"].index[-1].date()
    )
    mem = Memorizer(cut=cut_of(cfg, bars), seed=2)
    report = run_validation(
        cfg,
        bars,
        wf=SYNTH_WF,
        runners=mem.runners(),
    )
    # precondition guards: the fixture really is overfit in sample (else the test is vacuous)
    is_cfg = cfg.model_copy(update={"end": report.split.is_end})
    is_rep = mem.backtest(is_cfg, bars, bench=True)
    gap = is_rep.metrics.sharpe - is_rep.benchmark_metrics["SPY"].sharpe
    assert gap > 2.0, "fixture no longer overfit"
    assert is_rep.metrics.trade_count >= 30 and report.history.span_years >= 10, (
        "fixture no longer overfit"
    )
    gate = evaluate_gate(report)
    assert gate.passed is False
    assert gate.by_gate(3).status == "failed", gate.by_gate(3).detail
    assert gate.by_gate(5).status == "failed", gate.by_gate(5).detail
    assert {3, 5} <= set(gate.failed_gates())
    assert gate.by_gate(3).name == "oos_vs_benchmark" and gate.by_gate(5).name == "sensitivity"
    assert "offset" in gate.by_gate(5).detail or "corner" in gate.by_gate(5).detail


# ------------------------------------------------------------------ real engine


def _trend_cfg(df: pd.DataFrame, sma: int = 50) -> tuple[RulesSpec, object]:
    spec = RulesSpec.model_validate(
        {
            "id": "trend",
            "name": "SMA trend",
            "asset_class": "equity",
            "universe": ["SPY"],
            "schedule": {"freq": "daily"},
            "parameters": {"trend_len": sma, "atr_len": 14, "stop_mult": 3.0},
            "entry": {"gt": [{"price": "close"}, {"ind": "sma", "period": "$trend_len"}]},
            "position": {"weight": 1.0},
            "stops": [
                {
                    "type": "atr_trailing",
                    "period": "$atr_len",
                    "mult": "$stop_mult",
                    "anchor": "highest_close_since_entry",
                }
            ],
        }
    )
    return spec, single_sleeve_config(
        spec, df.index[260].date(), df.index[-1].date(), sleeve_id="trend"
    )


@pytest.mark.slow
@pytest.mark.timeout(90)
def test_positive_control_trend_follower_passes_gates_1_to_5() -> None:
    """A trend follower on a persistent-regime market (seed 5) must pass the same code path."""
    df = regime_market(seed=5, years=12.0)
    _, cfg = _trend_cfg(df)
    report = run_validation(cfg, {"SPY": df}, wf=SYNTH_WF)
    gate = evaluate_gate(report)
    assert [gate.by_gate(g).status for g in range(1, 6)] == ["passed"] * 5, [
        c.detail for c in gate.checks
    ]
    assert gate.by_gate(6).status == "pending" and gate.verdict == "hold"
    assert report.n_trials > 5 and report.full_trade_count >= 30
    # ... and with 90 days of forward paper trading attached the same report advances
    idx = pd.date_range("2025-01-01", periods=91, freq="D", tz="UTC")
    fwd = forward_stats(
        pd.Series(np.linspace(1, 1.05, 91), index=idx),
        pd.Series(np.ones(91), index=idx),
        date(2025, 1, 1),
    )
    assert evaluate_gate(report.model_copy(update={"forward": fwd})).verdict == "advance"


@pytest.mark.slow
@pytest.mark.timeout(90)
def test_tuned_walk_forward_on_a_random_walk_shows_selection_bias() -> None:
    """Second overfit path: a grid-search tuner on a zero-drift walk looks good only in-sample."""
    df = random_walk(seed=18, years=12.0)
    spec, cfg = _trend_cfg(df)
    view = make_is_view({"SPY": df}, cut_of(cfg, {"SPY": df}))
    wf = WalkForwardConfig(
        train_bars=500,
        test_bars=250,
        step_bars=250,
        tune=GridSpec(grid={"trend_len": [20.0, 60.0], "stop_mult": [2.0, 4.0]}),
    )
    res = walk_forward(cfg, view, wf)
    assert res.n_windows >= 4 and res.n_trials == res.n_windows * 5
    train = np.array([w.train_sharpe for w in res.windows], dtype=float)
    test = np.array([w.sharpe for w in res.windows], dtype=float)
    assert train.mean() > test.mean()  # the best-of-grid in-sample number does not carry over
    assert all(
        w.params is not None and set(w.params) == {"trend_len", "stop_mult"} for w in res.windows
    )
    assert res.mean_efficiency() is not None
    assert max(w.test_end for w in res.windows) < view.cut


@pytest.mark.slow
@pytest.mark.timeout(90)
def test_builtin_sleeve_end_to_end_on_the_fixtures() -> None:
    """mean_reversion through split, history, walk-forward, sweep, OOS-once and the gate."""
    cfg = h.golden_config("G1")
    bars = h.load_bars()
    registry = OosRegistry()
    report = run_validation(
        cfg,
        bars,
        wf=FIXTURE_WF_EQUITY,
        registry=registry,
    )
    gate = evaluate_gate(report)
    assert gate.by_gate(1).status == "failed"  # fixtures hold about 6 years, 10 are required
    assert gate.by_gate(6).status == "pending" and gate.verdict == "hold"
    assert report.split.n_is + report.split.n_oos == len(make_calendar(cfg, bars))
    assert report.split.n_is == int(np.floor((report.split.n_is + report.split.n_oos) * 0.7))
    assert report.walk_forward is not None and report.walk_forward.n_windows >= 4
    assert report.sensitivity is not None
    assert {n for n, _ in report.sensitivity.skipped} == {"atr_len=low", "atr_len=high"}
    assert {v.name for v in report.sensitivity.variants} >= {"corner=low", "corner=high"}
    assert report.oos is not None and not report.oos.peeking
    oos_eq = report.oos.report.result.equity
    assert (
        oos_eq.index[0].date() == report.split.cut
        and oos_eq.index[-1].date() == report.split.oos_end
    )
    assert report.full_trade_count is not None and report.full_trade_count >= report.oos_trade_count
    assert len(registry.evaluated) == 1


def make_calendar(cfg, bars):  # type: ignore[no-untyped-def]
    from tradebot.backtest.validation import strategy_calendar

    cal = strategy_calendar(cfg, bars)
    return cal[
        (cal >= pd.Timestamp(cfg.start, tz="UTC")) & (cal <= pd.Timestamp(cfg.end, tz="UTC"))
    ]
