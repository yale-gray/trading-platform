"""Rotation and rules signals on synthetic panels (SPEC-002 AC 4 and 5; T9).

Panels are deterministic (constant daily growth, hand-shaped dips), so every expected intent is
worked out from the construction in the comments.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.schema import RotationSpec, RulesSpec
from tradebot.strategy.signals import HeldPosition

pytestmark = pytest.mark.unit

IDX = pd.bdate_range("2020-01-01", periods=60, tz="UTC")


def _df(close: np.ndarray, idx: pd.DatetimeIndex = IDX) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": 1e6},
        index=idx,
    )


def _grow(g: float) -> pd.DataFrame:
    return _df(100 * (1 + g) ** np.arange(60))


def _rotation(mode: str = "pre_rank") -> RotationSpec:
    return RotationSpec.model_validate(
        {
            "id": "rot",
            "name": "rot",
            "asset_class": "equity",
            "parameters": {"mom": 10, "trend": 20, "top_n": 3},
            "universe": list("ABCDEF"),
            "schedule": {"freq": "monthly"},
            "eligibility": {"gt": [{"price": "close"}, {"ind": "sma", "period": "$trend"}]},
            "filter_mode": mode,
            "rank": {"by": {"ind": "ret", "period": "$mom"}, "order": "desc"},
            "select": {"top": "$top_n", "weighting": "equal"},
            "fallback": {"symbol": "IEF"},
        }
    )


def _eval(spec, bars, t: int, held=None, force: bool = True):  # type: ignore[no-untyped-def]
    comp = compile_strategy(spec, "s")
    sig = comp.evaluate(comp.prepare(bars), IDX[t], held or {}, force_decision=force)
    return sig, [(i.symbol, i.target, round(i.weight, 6)) for i in sig.intents]


GROWTH = {"A": 0.010, "B": 0.008, "C": 0.008, "D": 0.006, "E": -0.004, "F": 0.020, "IEF": 0.0}


def test_rotation_top3_by_return_among_trend_passers_ties_by_symbol() -> None:
    bars = {k: _grow(g) for k, g in GROWTH.items()}
    _, got = _eval(_rotation(), bars, 59)
    # E is below its SMA; ranks by 10-bar return: F > A > B = C > D, tie B/C goes to B
    assert sorted(got) == [
        ("A", "long", 1 / 3),
        ("B", "long", 1 / 3),
        ("F", "long", 1 / 3),
    ] or sorted(s for s, _, _ in got) == ["A", "B", "F"]
    assert all(abs(w - 1 / 3) < 1e-6 for _, _, w in got)


def test_rotation_fewer_than_three_eligible_gives_remainder_to_fallback() -> None:
    growth = {"A": 0.01, "B": -0.004, "C": -0.004, "D": -0.004, "E": -0.004, "F": 0.02, "IEF": 0.0}
    _, got = _eval(_rotation(), {k: _grow(g) for k, g in growth.items()}, 59)
    assert sorted(s for s, _, _ in got) == ["A", "F", "IEF"]
    assert all(t == "long" and abs(w - 1 / 3) < 1e-6 for _, t, w in got)


def test_rotation_post_rank_differs_from_pre_rank() -> None:
    e = np.concatenate([np.full(45, 300.0), np.full(5, 50.0), np.full(10, 90.0)])
    bars = {k: _grow(g) for k, g in GROWTH.items()}
    bars["E"] = _df(e)  # best 10-bar return (50 -> 90) but below its 20-bar SMA (about 132)
    pre = sorted(s for s, _, _ in _eval(_rotation("pre_rank"), bars, 59)[1])
    post = sorted(s for s, _, _ in _eval(_rotation("post_rank"), bars, 59)[1])
    assert pre == ["A", "B", "F"]  # filter first, then rank: E never enters
    assert post == ["A", "F", "IEF"]  # rank first (E, F, A), then E fails the filter: slot to IEF


def test_rotation_non_decision_bar_is_empty_and_held_unselected_goes_flat() -> None:
    bars = {k: _grow(g) for k, g in GROWTH.items()}
    sig, got = _eval(_rotation(), bars, 58, force=False)
    assert not sig.decision and got == []  # monthly schedule: mid-month bar emits nothing
    held = {"E": HeldPosition(symbol="E", entry_ts=IDX[30])}
    _, got = _eval(_rotation(), bars, 59, held)
    assert ("E", "flat", 0.0) in got and sum(1 for _, t, _ in got if t == "long") == 3


def _rules(exit_: dict) -> RulesSpec:
    return RulesSpec.model_validate(
        {
            "id": "r",
            "name": "r",
            "asset_class": "equity",
            "universe": ["SPY"],
            "schedule": {"freq": "daily"},
            "parameters": {"max_hold": 5},
            "entry": {
                "all": [
                    {"lt": [{"ind": "rsi", "period": 2}, 10]},
                    {"gt": [{"price": "close"}, {"ind": "sma", "period": 20}]},
                ]
            },
            "exit": exit_,
            "position": {"weight": 1.0},
        }
    )


def _dip_panel(
    extra: list[float] | None = None,
) -> tuple[dict[str, pd.DataFrame], pd.DatetimeIndex]:
    c = [100 * 1.005**i for i in range(40)]
    for _ in range(3):  # three 1% down days at bars 40, 41, 42: RSI(2) < 10 on bar 42
        c.append(c[-1] * 0.99)
    for r in extra or []:
        c.append(c[-1] * (1 + r))
    idx = pd.bdate_range("2020-01-01", periods=len(c), tz="UTC")
    return {"SPY": _df(np.array(c), idx)}, idx


def _rules_eval(spec, bars, idx, t, held=None):  # type: ignore[no-untyped-def]
    comp = compile_strategy(spec, "s")
    sig = comp.evaluate(comp.prepare(bars), idx[t], held or {}, force_decision=False)
    return [(i.symbol, i.target) for i in sig.intents]


def test_rules_enter_on_rsi_pullback_above_trend_and_only_then() -> None:
    bars, idx = _dip_panel()
    spec = _rules({"gt": [{"ind": "rsi", "period": 2}, 70]})
    assert _rules_eval(spec, bars, idx, 42) == [("SPY", "long")]
    assert _rules_eval(spec, bars, idx, 41) == []  # RSI not yet below 10 (or no entry): flat
    assert _rules_eval(spec, bars, idx, 30) == []


def test_rules_exit_on_rsi_and_on_time_and_exit_beats_entry() -> None:
    bars, idx = _dip_panel([0.02, 0.02, 0.02])  # bounce: RSI(2) > 70 by bar 44
    held = {"SPY": HeldPosition(symbol="SPY", entry_ts=idx[43])}
    spec = _rules({"gt": [{"ind": "rsi", "period": 2}, 70]})
    assert _rules_eval(spec, bars, idx, 45, held) == [("SPY", "flat")]
    # time exit: bars_held >= 5 signals at the close of the 5th bar after the fill bar
    timed = _rules({"ge": [{"pos": "bars_held"}, "$max_hold"]})
    flat_bars, fidx = _dip_panel([0.0005] * 6)
    h = {"SPY": HeldPosition(symbol="SPY", entry_ts=fidx[43])}
    assert _rules_eval(timed, flat_bars, fidx, 47, h) == [("SPY", "long")]  # bars_held 4: hold
    assert _rules_eval(timed, flat_bars, fidx, 48, h) == [("SPY", "flat")]  # bars_held 5: exit
    # exit wins when entry and exit are both true on the same bar
    both = _rules({"always": True})
    h42 = {"SPY": HeldPosition(symbol="SPY", entry_ts=idx[40])}
    assert _rules_eval(both, bars, idx, 42, h42) == [("SPY", "flat")]
