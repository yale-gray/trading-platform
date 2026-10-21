"""Pins from audit 002 (F2, F3, F7, F8, F9 and the walk-forward history guard).

Walk-forward window arithmetic is checked against exact calendar dates; gate 2 and the
``GateConfig`` defaults at their exact edges; ``_bounds`` for float vs integer slots; the OOS
cache key; and the sensitivity collapse rules when base Sharpe <= 0.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from gate_fakes import (
    FIXTURE_WF_CRYPTO,
    FIXTURE_WF_EQUITY,
    Memorizer,
    cut_of,
    dummy_spec,
    fake_report,
    metrics,
    single_sleeve_config,
)
from gate_synth import random_walk
from test_validation_gate import _report, _wf

from tradebot.backtest.validation import (
    GateConfig,
    OosRegistry,
    Runners,
    WalkForwardConfig,
    _bounds,
    collapse_reasons,
    evaluate_gate,
    make_is_view,
    run_oos,
    run_validation,
    walk_forward,
)
from tradebot.strategy.schema import RulesSpec

# ---------------------------------------------------------------- F2: window disjointness


def _stub() -> Runners:
    def run(cfg, bars):  # type: ignore[no-untyped-def]
        return fake_report(metrics(), metrics())

    return Runners(fund=run, benchmarked=run)


def _wf_windows(wf: WalkForwardConfig):  # type: ignore[no-untyped-def]
    bars = {"SPY": random_walk(1, 4)}
    cfg = single_sleeve_config(dummy_spec(5.0), date(2012, 1, 3), date(2013, 11, 1))
    view = make_is_view(bars, date(2012, 3, 1))
    return walk_forward(cfg, view, wf, runners=_stub()).windows, view


def test_rolling_windows_have_exact_calendar_dates() -> None:
    # Weekday calendar from Tue 2012-01-03: index 0..9 = Jan 3..16, 10..14 = Jan 17..23, etc.
    wins, _ = _wf_windows(WalkForwardConfig(train_bars=10, test_bars=5, step_bars=5))
    w0, w1, w2 = wins[:3]
    assert (w0.train_start, w0.train_end) == (date(2012, 1, 3), date(2012, 1, 16))
    assert (w0.test_start, w0.test_end) == (date(2012, 1, 17), date(2012, 1, 23))
    assert (w1.train_start, w1.train_end) == (date(2012, 1, 10), date(2012, 1, 23))
    assert (w1.test_start, w1.test_end) == (date(2012, 1, 24), date(2012, 1, 30))
    assert (w2.train_start, w2.test_start) == (date(2012, 1, 17), date(2012, 1, 31))


def test_anchored_windows_keep_the_first_bar_as_train_start() -> None:
    wins, _ = _wf_windows(WalkForwardConfig(train_bars=10, test_bars=5, step_bars=5, anchored=True))
    assert all(w.train_start == date(2012, 1, 3) for w in wins)
    assert (wins[1].train_end, wins[1].test_start) == (date(2012, 1, 23), date(2012, 1, 24))
    assert (wins[1].test_end) == date(2012, 1, 30)


@pytest.mark.parametrize("anchored", [False, True])
def test_train_and_test_are_disjoint_contiguous_and_inside_in_sample(anchored: bool) -> None:
    wf = WalkForwardConfig(train_bars=10, test_bars=5, step_bars=5, anchored=anchored)
    wins, view = _wf_windows(wf)
    assert len(wins) >= 4
    cal = [d.date() for d in pd.bdate_range("2012-01-03", "2012-02-29")]
    for w in wins:
        assert w.train_start <= w.train_end < w.test_start <= w.test_end  # train ∩ test = ∅
        assert cal.index(w.test_start) == cal.index(w.train_end) + 1  # test starts right after
        assert cal.index(w.test_end) - cal.index(w.test_start) + 1 == 5
        assert w.test_end < view.cut and w.train_start >= date(2012, 1, 3)
    for a, b in zip(wins, wins[1:], strict=False):
        assert a.test_end < b.test_start  # test windows never overlap
        assert cal.index(b.test_start) == cal.index(a.test_end) + 1  # ... and are contiguous


# ---------------------------------------------------------------- F3: gate 2 + defaults


def test_gate_config_defaults_are_the_d10_values() -> None:
    c = GateConfig()
    assert (c.min_years_equity, c.min_years_crypto, c.wf_min_windows) == (10.0, 5.0, 4)
    assert (c.wf_sharpe_frac, c.wf_dd_frac, c.wf_dd_mult, c.oos_dd_mult) == (0.6, 0.6, 1.0, 1.0)
    assert (c.min_trades, c.sens_sharpe_floor, c.sens_dd_mult, c.forward_min_days) == (
        30,
        0.5,
        1.5,
        90,
    )


def test_production_walk_forward_defaults_are_not_the_test_shrinks() -> None:
    eq, cr = WalkForwardConfig.defaults("equity"), WalkForwardConfig.defaults("crypto")
    assert (eq.train_bars, eq.test_bars, eq.step_bars) == (756, 252, 252)
    assert (cr.train_bars, cr.test_bars, cr.step_bars) == (548, 182, 182)
    assert FIXTURE_WF_EQUITY != eq and FIXTURE_WF_CRYPTO != cr


def _g2(sharpes: list[float]) -> str:
    return evaluate_gate(_report(walk_forward=_wf(sharpes))).by_gate(2).status


def test_gate2_sharpe_fraction_exactly_60_percent_passes_50_percent_fails() -> None:
    assert _g2([1, 1, 1, -1, -1]) == "passed"  # 3/5 = 0.6 exactly
    assert _g2([1, 1, -1, -1]) == "failed"  # 2/4 = 0.5 < 0.6
    assert _g2([1, 1, 1, -1, -1, -1, -1]) == "failed"  # 3/7


def test_gate2_zero_sharpe_window_is_not_positive() -> None:
    assert _g2([1, 1, 0.0, 0.0, 0.0]) == "failed"  # 2/5, zeros do not count
    assert _g2([1, 1, 1, 0.0, 0.0]) == "passed"  # 3/5
    assert _g2([1, 1, 1, float("nan"), float("nan")]) == "passed"
    assert _g2([1, 1, float("nan"), float("nan"), float("nan")]) == "failed"


def test_gate2_drawdown_fraction_edges() -> None:
    from tradebot.backtest.validation import WalkForwardResult

    wins = _wf([1] * 5).windows
    mixed = [
        w.model_copy(update={"max_drawdown": -0.2}) if i >= 3 else w for i, w in enumerate(wins)
    ]
    # 3 of 5 windows within the benchmark drawdown = 0.6 passes; 2 of 5 fails
    ok = WalkForwardResult(windows=mixed, n_trials=5)
    assert evaluate_gate(_report(walk_forward=ok)).by_gate(2).status == "passed"
    bad = WalkForwardResult(
        windows=[
            w.model_copy(update={"max_drawdown": -0.2}) if i >= 2 else w for i, w in enumerate(wins)
        ],
        n_trials=5,
    )
    assert evaluate_gate(_report(walk_forward=bad)).by_gate(2).status == "failed"


# ---------------------------------------------------------------- history guard in run_validation


def test_run_validation_fails_gate2_with_a_reason_when_in_sample_is_too_short() -> None:
    bars = {"SPY": random_walk(seed=3, years=4.0)}
    cfg = single_sleeve_config(
        dummy_spec(1.0), bars["SPY"].index[1].date(), bars["SPY"].index[-1].date()
    )
    mem = Memorizer(cut=cut_of(cfg, bars))
    report = run_validation(cfg, bars, runners=mem.runners())  # production defaults 756/252/252
    need = 756 + 4 * 252
    assert report.split.n_is < need
    assert report.walk_forward is not None and report.walk_forward.n_windows == 0
    assert report.wf_skip_reason is not None and str(need) in report.wf_skip_reason
    g2 = evaluate_gate(report).by_gate(2)
    assert g2.status == "failed" and "insufficient in-sample history" in g2.detail


# ---------------------------------------------------------------- F7: _bounds


def test_bounds_float_slots_use_exact_20_percent_even_when_whole_valued() -> None:
    assert _bounds(3.0, 0.2, integer=False) == (pytest.approx(2.4), pytest.approx(3.6))
    assert _bounds(2.0, 0.2, integer=False) == (pytest.approx(1.6), pytest.approx(2.4))
    assert _bounds(2.5, 0.2, integer=False) == (pytest.approx(2.0), pytest.approx(3.0))


def test_bounds_integer_slots_floor_low_ceil_high_and_force_a_difference() -> None:
    assert _bounds(5.0, 0.2, integer=True) == (4.0, 6.0)
    assert _bounds(7.0, 0.2, integer=True) == (5.0, 9.0)  # low floor(5.6)=5, high ceil(8.4)=9
    assert _bounds(2.0, 0.2, integer=True) == (1.0, 3.0)  # 1.6 floors to 1; 2.4 ceils to 3
    assert _bounds(1.0, 0.2, integer=True) == (None, 2.0)  # below 1 is skipped


def test_sweep_uses_the_slot_type_not_the_value_to_pick_integer_bounds() -> None:
    from gate_fakes import metrics as m

    from tradebot.backtest.validation import sensitivity_sweep

    spec = RulesSpec.model_validate(
        {
            "id": "t",
            "name": "t",
            "asset_class": "equity",
            "universe": ["SPY"],
            "schedule": {"freq": "daily"},
            "parameters": {"len": 7, "mult": 3.0},
            "entry": {
                "all": [
                    {"gt": [{"price": "close"}, {"ind": "sma", "period": "$len"}]},
                    {"gt": [1, "$mult"]},
                ]
            },
            "position": {"weight": 1.0},
        }
    )
    cfg = single_sleeve_config(spec, date(2012, 1, 3), date(2013, 1, 3))
    view = make_is_view({"SPY": random_walk(1, 4)}, date(2013, 1, 4))

    def run(c, b):  # type: ignore[no-untyped-def]
        return fake_report(m(), m())

    res = sensitivity_sweep(cfg, view, runners=Runners(fund=run, benchmarked=run))
    got = {v.name: v.overrides for v in res.variants}
    assert got["len=low"] == {"len": 5.0} and got["len=high"] == {"len": 9.0}
    assert got["mult=low"] == {"mult": pytest.approx(2.4)}
    assert got["mult=high"] == {"mult": pytest.approx(3.6)}


# ---------------------------------------------------------------- F8: OOS cache key includes cut


def test_oos_cache_is_keyed_on_the_cut_too() -> None:
    bars = {"SPY": random_walk(seed=4, years=4.0)}
    cfg = single_sleeve_config(
        dummy_spec(1.0), bars["SPY"].index[1].date(), bars["SPY"].index[-1].date()
    )
    calls: list[date] = []

    def bench(c, b):  # type: ignore[no-untyped-def]
        calls.append(c.start)
        return fake_report(metrics(), metrics())

    rn = Runners(fund=bench, benchmarked=bench)
    reg = OosRegistry()
    reg.freeze(cfg)
    a = run_oos(cfg, bars, cut=date(2012, 6, 1), registry=reg, runners=rn)
    again = run_oos(cfg, bars, cut=date(2012, 6, 1), registry=reg, runners=rn)
    other = run_oos(cfg, bars, cut=date(2012, 9, 3), registry=reg, runners=rn)
    assert again.cached and not a.cached and len(calls) == 2
    assert not other.cached and other.cut == date(2012, 9, 3)


# ---------------------------------------------------------------- F9: informational sweep


def test_collapse_rules_do_not_apply_when_base_sharpe_is_not_positive() -> None:
    cfg = GateConfig()
    base = metrics(sharpe=-0.2, max_drawdown=-0.10, total_return=0.1)
    worse = metrics(sharpe=-0.9, max_drawdown=-0.50, total_return=-0.3)
    assert collapse_reasons(base, worse, cfg) == []
    zero = metrics(sharpe=0.0, max_drawdown=-0.10, total_return=0.1)
    assert collapse_reasons(zero, worse, cfg) == []
    good = metrics(sharpe=1.0, max_drawdown=-0.10, total_return=0.1)
    assert collapse_reasons(good, worse, cfg) == ["sharpe", "drawdown", "return_sign"]
