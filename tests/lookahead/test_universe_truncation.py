"""``{universe: sp500_member}`` is causal (ADR-0001 A32): same truncation/poisoning pattern as
``test_strategy_truncation`` on synthetic bars, using the committed point-in-time history."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from test_strategy_truncation import (
    _calendar,
    _check_points,
    _leaky_signal,
    _signal,
    check_signal_invariance,
)

from tradebot.data.calendar import sessions
from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.schema import RulesSpec

# AAPL: always a member; SPLS: removed 2017-09; TWX: removed 2018-06; META (ex-FB): always
SYMS = ["AAPL", "SPLS", "TWX", "META"]


def _spec() -> RulesSpec:
    return RulesSpec.model_validate(
        {
            "id": "pit",
            "name": "pit",
            "asset_class": "equity",
            "universe": SYMS,
            "schedule": {"freq": "daily"},
            "entry": {
                "all": [
                    {"eq": [{"universe": "sp500_member"}, 1]},
                    {"lt": [{"ind": "ret", "period": 5}, 0.0]},
                ]
            },
            "exit": {"ge": [{"pos": "bars_held"}, 4]},
            "position": {
                "weight": "equal",
                "max_positions": 3,
                "rank": {"by": {"ind": "ret", "period": 5}, "order": "asc"},
            },
        }
    )


@pytest.fixture(scope="module")
def bars() -> dict[str, pd.DataFrame]:
    idx = sessions("SPY", date(2016, 6, 1), date(2018, 12, 31))
    rng = np.random.default_rng(7)
    out = {}
    for sym in SYMS:
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, len(idx))))
        out[sym] = pd.DataFrame(
            {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx
        )
    return out


@pytest.fixture(scope="module")
def case(bars):
    spec = _spec()
    cal = _calendar(spec, bars)
    pts = [t for t in _check_points(cal, spec.warmup_bars) if t.year >= 2017]
    mid = [t for t in cal if date(2017, 9, 1) <= t.date() <= date(2017, 9, 25)]
    return spec, cal, sorted({*pts, *mid[::2]})


@pytest.mark.property
@pytest.mark.parametrize("poison", [False, True], ids=["truncation", "poisoning"])
@pytest.mark.parametrize("with_held", [False, True], ids=["flat", "held"])
def test_membership_signal_is_causal(case, bars, with_held: bool, poison: bool) -> None:
    spec, cal, points = case
    n = check_signal_invariance(
        _signal, spec, bars, points, cal, with_held=with_held, poison=poison
    )
    assert n > 0, "check was vacuous: no signal ever produced intents"


@pytest.mark.property
def test_membership_feature_ignores_every_bar_value(bars) -> None:
    """The feature depends only on dates: rewriting all prices leaves it unchanged."""
    spec = _spec()
    base = compile_strategy(spec)
    f1 = base.prepare({s: bars[s] for s in base.required})
    other = {s: df.assign(close=df["close"] * 3 + 1.0) for s, df in bars.items()}
    alt = compile_strategy(spec)
    f2 = alt.prepare({s: other[s] for s in alt.required})
    for key, arr in f1.arrays.items():
        if key[1] == "universe":
            assert np.array_equal(arr, f2.arrays[key])


@pytest.mark.unit
@pytest.mark.parametrize("poison", [False, True], ids=["truncation", "poisoning"])
def test_checker_catches_a_leaky_membership_feature(case, bars, poison: bool) -> None:
    spec, cal, points = case
    with pytest.raises(AssertionError, match="changed signal"):
        check_signal_invariance(
            _leaky_signal, spec, bars, points, cal, with_held=False, poison=poison
        )
