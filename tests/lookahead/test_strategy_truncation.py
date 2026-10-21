"""Strategy signals are causal: truncation invariance and future poisoning (SPEC-005 s3).

For every built-in strategy the ``SignalSet`` at bar t computed from bars truncated at t must
equal the one computed from the full history, and must not change when every later bar is
replaced by garbage. Checked with and without held positions, on strided bars plus every
month-end/month-start boundary of a sample year and the 2024-12 year-end boundary. A leaky
canary proves the checkers bind.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from helpers import repair_ohlc, select_ts

from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.schema import STRATEGIES, StrategySpec
from tradebot.strategy.signals import HeldPosition

STRATEGY_NAMES: list[str] = sorted(STRATEGIES)

Bars = Mapping[str, pd.DataFrame]
SignalFn = Callable[[StrategySpec, Bars, pd.Timestamp, dict[str, pd.Timestamp], bool], Any]

_COLS = ["open", "high", "low", "close", "volume"]
_POISON = ["open", "high", "low", "close", "volume"]
_BOUNDARY_YEAR = 2022


@pytest.fixture(scope="module")
def bars() -> dict[str, pd.DataFrame]:
    """All committed fixture bars keyed by canonical symbol (read-only parquet reads)."""
    dest = Path(__file__).resolve().parents[1] / "fixtures" / "market"
    out: dict[str, pd.DataFrame] = {}
    for path in sorted(dest.glob("*/*/gen-*/bars.parquet")):
        sym = path.parents[1].name.replace("_", "/")
        out[sym] = pd.read_parquet(path)[_COLS]
    return out


def _signal(
    spec: StrategySpec,
    bars: Bars,
    ts: pd.Timestamp,
    held: dict[str, pd.Timestamp],
    force: bool,
) -> dict[str, Any]:
    """Reference signal function: fresh compile, prepare, evaluate at ts."""
    strat = compile_strategy(spec)
    feats = strat.prepare({s: bars[s] for s in strat.required})
    positions = {s: HeldPosition(symbol=s, entry_ts=e) for s, e in held.items()}
    out = strat.evaluate(feats, ts, positions, force_decision=force).model_dump()
    out["is_decision_bar"] = strat.is_decision_bar(ts)
    return out


def _leaky_signal(
    spec: StrategySpec,
    bars: Bars,
    ts: pd.Timestamp,
    held: dict[str, pd.Timestamp],
    force: bool,
) -> dict[str, Any]:
    """Deliberately leaky: evaluates at ts on features shifted by -1 (reads bar t+1)."""
    strat = compile_strategy(spec)
    feats = strat.prepare({s: bars[s] for s in strat.required})
    for key, arr in feats.arrays.items():
        shifted = np.full_like(arr, np.nan)
        shifted[:-1] = arr[1:]
        feats.arrays[key] = shifted
    positions = {s: HeldPosition(symbol=s, entry_ts=e) for s, e in held.items()}
    out = strat.evaluate(feats, ts, positions, force_decision=force).model_dump()
    out["is_decision_bar"] = strat.is_decision_bar(ts)
    return out


def _truncate(bars: Bars, ts: pd.Timestamp) -> dict[str, pd.DataFrame]:
    return {s: df.loc[df.index <= ts] for s, df in bars.items()}


def _poison(bars: Bars, ts: pd.Timestamp, rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for s, df in bars.items():
        df = df.copy()
        mask = np.asarray(df.index > ts)
        if mask.any():
            df.loc[mask, _POISON] = rng.uniform(0.01, 1e6, size=(int(mask.sum()), len(_POISON)))
            df = repair_ohlc(df)
        out[s] = df
    return out


def _calendar(spec: StrategySpec, bars: Bars) -> pd.DatetimeIndex:
    strat = compile_strategy(spec)
    return strat.prepare({s: bars[s] for s in strat.required}).calendar


def _check_points(cal: pd.DatetimeIndex, warmup: int) -> list[pd.Timestamp]:
    """Strided bars + month boundaries of a sample year + the 2024-12 year-end boundary."""
    picks = set(select_ts(len(cal), warmup, stride=150))
    months = cal.to_series().groupby([cal.year, cal.month])
    for (year, month), grp in months:
        if year == _BOUNDARY_YEAR:
            picks.add(cal.get_loc(grp.index[0]))
            picks.add(cal.get_loc(grp.index[-1]))
        elif (year, month) in {(2024, 12), (2025, 1)}:
            picks.add(cal.get_loc(grp.index[0]))
            picks.add(cal.get_loc(grp.index[-1]))
    picks.update(i for i in range(max(len(cal) - 5, 0), len(cal)))
    return [cal[i] for i in sorted(picks) if i >= 3]


def _held_for(spec: StrategySpec, cal: pd.DatetimeIndex, ts: pd.Timestamp) -> dict[str, Any]:
    """A symbol held since 3 bars earlier (plus one outside the universe-ish second holding)."""
    i = int(cal.get_loc(ts))
    strat = compile_strategy(spec)
    return {s: cal[i - 3] for s in strat.universe[:2]}


def check_signal_invariance(
    fn: SignalFn,
    spec: StrategySpec,
    bars: Bars,
    points: list[pd.Timestamp],
    cal: pd.DatetimeIndex,
    *,
    with_held: bool,
    poison: bool,
    seed: int = 20250101,
) -> int:
    """Signal at t from truncated (or poisoned-future) bars must equal the full-bars signal.

    Returns the number of decision-bar signals with intents compared (non-vacuity measure).
    """
    rng = np.random.default_rng(seed)
    nonempty = 0
    for n, ts in enumerate(points):
        held = _held_for(spec, cal, ts) if with_held else {}
        for force in (n % 2 == 1,):
            full = fn(spec, bars, ts, held, force)
            alt_bars = _poison(bars, ts, rng) if poison else _truncate(bars, ts)
            alt = fn(spec, alt_bars, ts, held, force)
            kind = "poisoning" if poison else "truncation"
            assert alt == full, f"{kind} changed signal at {ts} (force={force}, held={bool(held)})"
            nonempty += bool(full["intents"])
    return nonempty


@pytest.fixture(scope="module", params=STRATEGY_NAMES)
def case(request: pytest.FixtureRequest, bars: dict[str, pd.DataFrame]):
    spec = STRATEGIES[request.param]
    cal = _calendar(spec, bars)
    return spec, cal, _check_points(cal, spec.warmup_bars)


@pytest.mark.property
@pytest.mark.parametrize("with_held", [False, True], ids=["flat", "held"])
def test_truncation_invariance(case, bars, with_held: bool) -> None:
    spec, cal, points = case
    n = check_signal_invariance(_signal, spec, bars, points, cal, with_held=with_held, poison=False)
    assert n > 0, "check was vacuous: no signal ever produced intents"


@pytest.mark.property
@pytest.mark.parametrize("with_held", [False, True], ids=["flat", "held"])
def test_future_poisoning_invariance(case, bars, with_held: bool) -> None:
    spec, cal, points = case
    n = check_signal_invariance(_signal, spec, bars, points, cal, with_held=with_held, poison=True)
    assert n > 0, "check was vacuous: no signal ever produced intents"


@pytest.mark.property
def test_decision_bar_invariant_under_truncation(case, bars) -> None:
    spec, cal, points = case
    strat_full = compile_strategy(spec)
    strat_full.prepare({s: bars[s] for s in strat_full.required})
    for ts in points:
        strat = compile_strategy(spec)
        trunc = _truncate(bars, ts)
        strat.prepare({s: trunc[s] for s in strat.required})
        assert strat.is_decision_bar(ts) == strat_full.is_decision_bar(ts), f"at {ts}"


@pytest.mark.unit
@pytest.mark.parametrize("poison", [False, True], ids=["truncation", "poisoning"])
@pytest.mark.parametrize("with_held", [False, True], ids=["flat", "held"])
def test_checker_catches_leaky_strategy(case, bars, with_held: bool, poison: bool) -> None:
    """The canary: features shifted by -1 must be flagged, or the checks above prove nothing."""
    spec, cal, points = case
    with pytest.raises(AssertionError, match="changed signal"):
        check_signal_invariance(
            _leaky_signal, spec, bars, points, cal, with_held=with_held, poison=poison
        )


@pytest.mark.unit
def test_strategy_names_match_registry() -> None:
    assert STRATEGY_NAMES == sorted(STRATEGIES)
    assert len(STRATEGY_NAMES) >= 3
