"""Hold-out discipline (SPEC-005 section 7, SPEC-002 section 8 gate 1; T18).

The tuner sees the data through a spy. Poisoning the last 30% must change nothing, no index past
the split date may reach tuning, and the OOS window is read once per config hash.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from gate_fakes import SYNTH_WF, Memorizer, cut_of, dummy_spec, single_sleeve_config
from gate_synth import regime_market

from tradebot.backtest.engine import run_with_benchmark
from tradebot.backtest.validation import (
    DataView,
    GridSpec,
    OosRegistry,
    Runners,
    WalkForwardConfig,
    evaluate_gate,
    make_is_view,
    run_fund_report,
    run_oos,
    run_validation,
    sensitivity_sweep,
    split_oos,
    walk_forward,
)


def _trend(sma: int = 50, mult: float = 3.0):  # type: ignore[no-untyped-def]
    from tradebot.strategy.schema import RulesSpec

    return RulesSpec.model_validate(
        {
            "id": "trend",
            "name": "SMA trend",
            "asset_class": "equity",
            "universe": ["SPY"],
            "schedule": {"freq": "daily"},
            "parameters": {"trend_len": sma, "atr_len": 14, "stop_mult": mult},
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


class Spy:
    """Runners that record the last bar every backtest was given."""

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


@pytest.fixture(scope="module")
def market() -> dict[str, pd.DataFrame]:
    return {"SPY": regime_market(seed=3, years=12.0)}


@pytest.fixture(scope="module")
def cfg(market):  # type: ignore[no-untyped-def]
    df = market["SPY"]
    return single_sleeve_config(
        _trend(), df.index[260].date(), df.index[-1].date(), sleeve_id="trend"
    )


WF = WalkForwardConfig(
    train_bars=500,
    test_bars=250,
    step_bars=250,
    tune=GridSpec(grid={"trend_len": [30.0, 60.0]}),
)


@pytest.mark.slow
@pytest.mark.timeout(90)
def test_tuner_never_receives_a_bar_past_the_split_and_poison_changes_nothing(market, cfg) -> None:  # type: ignore[no-untyped-def]
    cut = cut_of(cfg, market)
    ts_cut = pd.Timestamp(cut, tz="UTC")
    clean_view = make_is_view(market, cut)
    spy = Spy()
    wf_clean = walk_forward(cfg, clean_view, WF, runners=spy.runners())
    sens_clean = sensitivity_sweep(cfg, clean_view, runners=spy.runners())
    assert spy.last and max(spy.last) < ts_cut  # not one bar at or past the split date

    poisoned = market["SPY"].copy()
    tail = poisoned.index >= ts_cut
    assert tail.sum() > 800
    poisoned.loc[tail, ["open", "high", "low", "close"]] = np.random.default_rng(0).uniform(
        1e3, 1e6, size=(int(tail.sum()), 4)
    )
    poisoned.loc[tail, "high"] = poisoned.loc[tail, ["open", "high", "low", "close"]].max(axis=1)
    poisoned.loc[tail, "low"] = poisoned.loc[tail, ["open", "high", "low", "close"]].min(axis=1)
    pview = make_is_view({"SPY": poisoned}, cut)
    wf_poison = walk_forward(cfg, pview, WF)
    sens_poison = sensitivity_sweep(cfg, pview)
    assert wf_poison == wf_clean  # chosen params and in-sample metrics identical
    assert sens_poison == sens_clean
    assert all(w.test_end < cut for w in wf_clean.windows)
    assert wf_clean.n_windows >= 4


def test_view_rejects_out_of_sample_bars(market) -> None:  # type: ignore[no-untyped-def]
    df = market["SPY"]
    cut = df.index[2000].date()
    with pytest.raises(ValueError, match="out-of-sample"):
        DataView({"SPY": df}, cut)
    view = make_is_view(market, cut)
    assert view.bars["SPY"].index[-1] < pd.Timestamp(cut, tz="UTC") and view.oos_locked
    unlocked = DataView.__new__(DataView)
    object.__setattr__(unlocked, "bars", view.bars)
    object.__setattr__(unlocked, "cut", cut)
    object.__setattr__(unlocked, "oos_locked", False)
    spec_cfg = single_sleeve_config(dummy_spec(), df.index[1].date(), df.index[-1].date())
    with pytest.raises(ValueError, match="locked"):
        walk_forward(spec_cfg, unlocked, WalkForwardConfig(train_bars=10, test_bars=5, step_bars=5))
    with pytest.raises(ValueError, match="locked"):
        sensitivity_sweep(spec_cfg, unlocked)


def test_split_is_by_date_and_fixed_before_tuning(market, cfg) -> None:  # type: ignore[no-untyped-def]
    cal = market["SPY"].index[260:]
    is_idx, oos_idx = split_oos(cal)
    assert cut_of(cfg, market) == oos_idx[0].date()
    # appending bars after the fact would move the cut: the cut is a function of the calendar only
    assert split_oos(cal[:-100])[1][0] != oos_idx[0]


def test_oos_needs_a_frozen_config_and_is_read_once_per_hash(market, cfg) -> None:  # type: ignore[no-untyped-def]
    cut = cut_of(cfg, market)
    reg = OosRegistry()
    calls: list[int] = []

    def counting(c, b):  # type: ignore[no-untyped-def]
        calls.append(1)
        return run_with_benchmark(c, b)

    rn = Runners(benchmarked=counting)
    with pytest.raises(PermissionError):
        run_oos(cfg, market, cut=cut, registry=reg, runners=rn)
    h1 = reg.freeze(cfg)
    first = run_oos(cfg, market, cut=cut, registry=reg, runners=rn)
    again = run_oos(cfg, market, cut=cut, registry=reg, runners=rn)
    assert len(calls) == 1 and again.cached and not first.peeking and first.config_hash == h1
    assert first.report.result.equity.index[0].date() >= cut  # trades only from the cut

    # a second, different hash for the same strategy id is flagged as peeking
    other = cfg.model_copy(
        update={"sleeves": [cfg.sleeves[0].model_copy(update={"strategy": _trend(sma=40)})]}
    )
    reg.freeze(other)
    second = run_oos(other, market, cut=cut, registry=reg, runners=rn)
    assert second.peeking and "trend" in reg.peeking and len(calls) == 2


def test_peeking_fails_gate_one_in_the_report(market) -> None:  # type: ignore[no-untyped-def]
    df = market["SPY"]
    cfg = single_sleeve_config(dummy_spec(1.0), df.index[1].date(), df.index[-1].date())
    mem = Memorizer(cut=cut_of(cfg, market))
    reg = OosRegistry()
    wf = SYNTH_WF
    clean = run_validation(cfg, market, wf=wf, registry=reg, runners=mem.runners())
    assert not clean.oos.peeking  # type: ignore[union-attr]
    # re-validating with a different parameter on the same registry/strategy id = a second look
    cfg2 = single_sleeve_config(dummy_spec(2.0), df.index[1].date(), df.index[-1].date())
    again = run_validation(cfg2, market, wf=wf, registry=reg, runners=mem.runners())
    assert again.oos.peeking  # type: ignore[union-attr]
    gate = evaluate_gate(again)
    assert gate.by_gate(1).status == "failed" and "peeking=True" in gate.by_gate(1).detail
    assert date(2000, 1, 1) < again.split.cut
