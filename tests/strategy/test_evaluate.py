"""Strategy evaluator semantics (SPEC-002 sections 2 and 2a; SPEC-003 inputs).

Each case builds a fresh ``compile_strategy`` and ``prepare`` on tiny hand-built bar frames and
calls ``evaluate`` directly. Expected intents are worked out by hand in comments.
Audit 003-wave3 G5 (mutants SE01-SE24).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd
import pytest
import yaml

from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.io import load_strategy
from tradebot.strategy.signals import HeldPosition, Intent, SignalSet

pytestmark = pytest.mark.unit

IDX = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")  # daily bars, Jan 1..5
MIDX = pd.DatetimeIndex(
    ["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02", "2024-02-05"], tz="UTC"
)  # spans a month boundary: decision bars are index 0 (Jan 30) and 2 (Feb 1)
NAN = float("nan")
CLOSE: dict[str, Any] = {"price": "close"}
STOP = {"type": "atr_trailing", "period": 14, "mult": 3.0, "anchor": "highest_close_since_entry"}


# --------------------------------------------------------------------------- helpers


def _frames(
    index: pd.DatetimeIndex,
    closes: Mapping[str, Sequence[float]],
    volumes: Mapping[str, Sequence[float]] | None = None,
) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym, c in closes.items():
        close = pd.Series(list(c), index=index, dtype="float64")
        vols = (volumes or {}).get(sym, [1.0] * len(index))
        vol = pd.Series(list(vols), index=index, dtype="float64")
        out[sym] = pd.DataFrame(
            {"open": close, "high": close, "low": close, "close": close, "volume": vol},
            index=index,
        )
    return out


def _yaml(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False)


def _rules(
    entry: dict[str, Any],
    exit_: dict[str, Any] | None = None,
    *,
    universe: Sequence[str] = ("SPY",),
    position: dict[str, Any] | None = None,
    stops: list[dict[str, Any]] | None = None,
    freq: str = "daily",
) -> str:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "id": "t_rules",
        "name": "t_rules",
        "kind": "rules",
        "asset_class": "equity",
        "universe": list(universe),
        "schedule": {"freq": freq},
        "entry": entry,
    }
    if exit_ is not None:
        doc["exit"] = exit_
    if position is not None:
        doc["position"] = position
    if stops is not None:
        doc["stops"] = stops
    return _yaml(doc)


def _rot(
    *,
    universe: Sequence[str] = ("SPY", "QQQ", "IWM", "EFA"),
    top: int = 2,
    order: str = "desc",
    by: dict[str, Any] = CLOSE,
    eligibility: dict[str, Any] | None = None,
    filter_mode: str | None = None,
    fallback: dict[str, Any] | None = None,
    freq: str = "daily",
) -> str:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "id": "t_rot",
        "name": "t_rot",
        "kind": "rotation",
        "asset_class": "equity",
        "universe": list(universe),
        "schedule": {"freq": freq},
        "rank": {"by": by, "order": order},
        "select": {"top": top},
    }
    if eligibility is not None:
        doc["eligibility"] = eligibility
    if filter_mode is not None:
        doc["filter_mode"] = filter_mode
    if fallback is not None:
        doc["fallback"] = fallback
    return _yaml(doc)


def _run(
    text: str,
    frames: Mapping[str, pd.DataFrame],
    index: pd.DatetimeIndex,
    at: int,
    held: Mapping[str, HeldPosition] | None = None,
    force: bool = False,
) -> SignalSet:
    cs = compile_strategy(load_strategy(text))  # fresh per case
    feats = cs.prepare(frames)  # fresh per case
    return cs.evaluate(feats, index[at], dict(held or {}), force_decision=force)


def _held(sym: str, index: pd.DatetimeIndex, fill_at: int) -> dict[str, HeldPosition]:
    return {sym: HeldPosition(symbol=sym, entry_ts=index[fill_at])}


def _by(ss: SignalSet) -> dict[str, Intent]:
    return {i.symbol: i for i in ss.intents}


# Condition building blocks. UNKNOWN is a comparison whose operand is NaN; FALSE and TRUE are
# known truth values that do not depend on the bar data.
UNKNOWN: dict[str, Any] = {"gt": [CLOSE, 100]}
FALSE: dict[str, Any] = {"gt": [1, 2]}
TRUE: dict[str, Any] = {"always": True}


# --------------------------------------------------------------------------- comparisons


@pytest.mark.parametrize(
    ("op", "close", "expect"),
    [
        ("gt", 100.0, False),
        ("gt", 101.0, True),
        ("ge", 100.0, True),
        ("ge", 99.0, False),
        ("lt", 100.0, False),
        ("lt", 99.0, True),
        ("le", 100.0, True),
        ("le", 101.0, False),
        ("eq", 100.0, True),
        ("eq", 101.0, False),
    ],
)
def test_comparison_operators_against_literal_100(op: str, close: float, expect: bool) -> None:
    # Single bar, close vs literal 100. gt/lt are strict, ge/le inclusive, eq exact.
    text = _rules({op: [CLOSE, 100]})
    ss = _run(text, _frames(IDX, {"SPY": [close] * 5}), IDX, 0)
    assert ("SPY" in _by(ss)) is expect


def test_crosses_above_fires_only_on_the_crossing_bar() -> None:
    # close 90, 95, 105, 110, 99 vs 100.
    # bar1: prev 90 <= 100 but now 95 > 100 is false -> no.
    # bar2: prev 95 <= 100 and now 105 > 100 -> crossing, entry.
    # bar3: prev 105 <= 100 is false -> no.
    text = _rules({"crosses_above": [CLOSE, 100]})
    frames = _frames(IDX, {"SPY": [90, 95, 105, 110, 99]})
    assert _by(_run(text, frames, IDX, 1)) == {}
    assert "SPY" in _by(_run(text, frames, IDX, 2))
    assert _by(_run(text, frames, IDX, 3)) == {}


def test_crosses_above_is_strict_on_the_current_bar() -> None:
    # close 90, 100, 101. bar1: now 100 is not > 100, so no cross. bar2: prev 100 <= 100
    # (inclusive on the prior bar) and now 101 > 100, so cross.
    text = _rules({"crosses_above": [CLOSE, 100]})
    frames = _frames(IDX, {"SPY": [90, 100, 101, 101, 101]})
    assert _by(_run(text, frames, IDX, 1)) == {}
    assert "SPY" in _by(_run(text, frames, IDX, 2))


def test_crosses_below_fires_only_on_the_crossing_bar() -> None:
    # close 110, 100, 99 vs 100.
    # bar1: prev 110 >= 100, now 100 < 100 is false -> no.
    # bar2: prev 100 >= 100 and now 99 < 100 -> cross, entry.
    text = _rules({"crosses_below": [CLOSE, 100]})
    frames = _frames(IDX, {"SPY": [110, 100, 99, 99, 99]})
    assert _by(_run(text, frames, IDX, 1)) == {}
    assert "SPY" in _by(_run(text, frames, IDX, 2))


def test_crossing_with_a_nan_bar_is_not_a_cross() -> None:
    # close 90, NaN, 105. bar2 would be a cross against the valid bar 90, but the prior bar is
    # NaN (unknown), so no entry.
    text = _rules({"crosses_above": [CLOSE, 100]})
    frames = _frames(IDX, {"SPY": [90, NAN, 105, 105, 105]})
    assert _by(_run(text, frames, IDX, 2)) == {}


# --------------------------------------------------------------------------- rule combinators


def test_all_and_any_are_three_valued_with_nan_operands() -> None:
    # Every case uses a NaN close, so UNKNOWN is unknown on every bar.
    # all: any known False wins (False); otherwise unknown -> no entry.
    # any: any known True wins (True); otherwise unknown -> no entry.
    nan_frames = _frames(IDX, {"SPY": [NAN] * 5})
    cases = [
        ({"all": [UNKNOWN, TRUE]}, False),  # unknown and true -> unknown -> no entry
        ({"all": [UNKNOWN, FALSE]}, False),  # unknown and false -> false
        ({"any": [UNKNOWN, TRUE]}, True),  # unknown or true -> true
        ({"any": [UNKNOWN, FALSE]}, False),  # unknown or false -> unknown -> no entry
    ]
    for entry, expect in cases:
        ss = _run(_rules(entry), nan_frames, IDX, 0)
        assert ("SPY" in _by(ss)) is expect, entry


def test_not_inverts_a_known_comparison() -> None:
    # not (close > 100): close 99 -> not false = true (entry); close 101 -> false (no entry).
    text = _rules({"not": {"gt": [CLOSE, 100]}})
    assert "SPY" in _by(_run(text, _frames(IDX, {"SPY": [99] * 5}), IDX, 0))
    assert _by(_run(text, _frames(IDX, {"SPY": [101] * 5}), IDX, 0)) == {}


def test_not_of_a_nan_comparison_is_not_an_entry() -> None:
    # not (NaN > 100) is unknown, not true, so no entry. Same comparison with close 99 does enter.
    text = _rules({"not": {"gt": [CLOSE, 100]}})
    assert _by(_run(text, _frames(IDX, {"SPY": [NAN] * 5}), IDX, 0)) == {}
    assert "SPY" in _by(_run(text, _frames(IDX, {"SPY": [99] * 5}), IDX, 0))


def test_nan_comparison_is_false_at_entry() -> None:
    # Entry close > 100 on a NaN close is unknown, so no entry.
    text = _rules({"gt": [CLOSE, 100]})
    assert _by(_run(text, _frames(IDX, {"SPY": [NAN] * 5}), IDX, 0)) == {}


# --------------------------------------------------------------------------- rules: entry/exit


def test_entry_only_when_flat_held_symbol_gets_hold_not_entry() -> None:
    # Entry is true and the symbol is held. It must be a hold, never a fresh "entry" intent.
    text = _rules({"gt": [CLOSE, 100]}, {"gt": [CLOSE, 1000]})  # exit false
    frames = _frames(IDX, {"SPY": [150] * 5})
    ss = _run(text, frames, IDX, 1, held=_held("SPY", IDX, 0))
    intent = _by(ss)["SPY"]
    assert intent.target == "long"
    assert intent.reason.startswith("hold")
    assert intent.reason != "entry"


def test_exit_only_when_held_flat_symbol_with_true_exit_gets_nothing() -> None:
    # Entry false, exit true, not held: the exit is never consulted, so no intent.
    text = _rules({"gt": [CLOSE, 1000]}, {"lt": [CLOSE, 200]})
    assert _by(_run(text, _frames(IDX, {"SPY": [150] * 5}), IDX, 0)) == {}


def test_exit_takes_priority_over_entry_when_held() -> None:
    # Held, entry true (150 > 100) and exit true (150 < 200). Exit wins: flat.
    text = _rules({"gt": [CLOSE, 100]}, {"lt": [CLOSE, 200]})
    ss = _run(text, _frames(IDX, {"SPY": [150] * 5}), IDX, 1, held=_held("SPY", IDX, 0))
    intent = _by(ss)["SPY"]
    assert intent.target == "flat"
    assert intent.reason.startswith("exit condition")


def test_hold_while_exits_when_entry_turns_false() -> None:
    # No exit block: exit = not entry. close 150, 150, 90: held from bar 0.
    # bar1 entry true, so hold. bar2 entry false, so flat.
    text = _rules({"gt": [CLOSE, 100]})
    frames = _frames(IDX, {"SPY": [150, 150, 90, 150, 150]})
    held = _held("SPY", IDX, 0)
    assert _by(_run(text, frames, IDX, 1, held=held))["SPY"].target == "long"
    flat = _by(_run(text, frames, IDX, 2, held=held))["SPY"]
    assert flat.target == "flat"
    assert flat.reason.startswith("exit: entry no longer true")


def test_explicit_exit_holds_while_entry_is_false() -> None:
    # Explicit exit (close < 80) is false at close 90, so the position is held even though the
    # entry (close > 100) is false.
    text = _rules({"gt": [CLOSE, 100]}, {"lt": [CLOSE, 80]})
    ss = _run(text, _frames(IDX, {"SPY": [90] * 5}), IDX, 1, held=_held("SPY", IDX, 0))
    assert _by(ss)["SPY"].target == "long"
    assert _by(ss)["SPY"].reason.startswith("hold")


def test_nan_in_explicit_exit_is_unknown_and_holds() -> None:
    # Exit (close < 80) with a NaN close is unknown, which is not confirmed True, so it holds.
    text = _rules({"gt": [CLOSE, 1000]}, {"lt": [CLOSE, 80]})
    ss = _run(text, _frames(IDX, {"SPY": [NAN] * 5}), IDX, 2, held=_held("SPY", IDX, 0))
    assert _by(ss)["SPY"].target == "long"


def test_hold_while_nan_entry_exits_pins_audit_f3() -> None:
    # Audit F3, judged the safe direction: with no explicit exit, a NaN entry is not confirmed
    # True, so exit = not entry fires and the held position is flattened. Pinned as it stands.
    text = _rules({"gt": [CLOSE, 100]})
    ss = _run(text, _frames(IDX, {"SPY": [NAN] * 5}), IDX, 2, held=_held("SPY", IDX, 0))
    intent = _by(ss)["SPY"]
    assert intent.target == "flat"
    assert intent.reason.startswith("exit: entry no longer true")


def test_time_exit_fires_at_exactly_n_bars_held() -> None:
    # Fill at bar 0. bars_held at bar t is t - 0. Exit at bars_held >= 3.
    # bar2: bars_held 2 -> 2 >= 3 is false, hold. bar3: bars_held 3 -> exit.
    text = _rules({"gt": [CLOSE, 1000]}, {"ge": [{"pos": "bars_held"}, 3]})
    frames = _frames(IDX, {"SPY": [150] * 5})
    held = _held("SPY", IDX, 0)
    hold = _by(_run(text, frames, IDX, 2, held=held))["SPY"]
    assert hold.target == "long"
    assert hold.reason == "hold (bars_held=2)"
    exit_ = _by(_run(text, frames, IDX, 3, held=held))["SPY"]
    assert exit_.target == "flat"
    assert exit_.reason == "exit condition (bars_held=3)"


def test_bars_held_counts_from_the_fill_bar() -> None:
    # Fill at bar 1, not bar 0. Exit at bars_held >= 2.
    # bar2: bars_held 1, hold. bar3: bars_held 2, exit. A count from bar 0 would exit at bar 2.
    text = _rules({"gt": [CLOSE, 1000]}, {"ge": [{"pos": "bars_held"}, 2]})
    frames = _frames(IDX, {"SPY": [150] * 5})
    held = _held("SPY", IDX, 1)
    assert _by(_run(text, frames, IDX, 2, held=held))["SPY"].target == "long"
    assert _by(_run(text, frames, IDX, 3, held=held))["SPY"].target == "flat"


def test_equal_weight_is_one_over_universe_size() -> None:
    # Three symbols, all entering: weight 1/3 each.
    universe = ("SPY", "QQQ", "IWM")
    text = _rules({"gt": [CLOSE, 100]}, universe=universe)
    frames = _frames(IDX, {s: [150] * 5 for s in universe})
    by = _by(_run(text, frames, IDX, 0))
    assert set(by) == set(universe)
    for sym in universe:
        assert by[sym].weight == pytest.approx(1 / 3)
        assert by[sym].reason == "entry"


def test_explicit_position_weight_is_used_as_given() -> None:
    text = _rules({"gt": [CLOSE, 100]}, position={"weight": 0.4})
    ss = _run(text, _frames(IDX, {"SPY": [150] * 5}), IDX, 0)
    assert _by(ss)["SPY"].weight == pytest.approx(0.4)


def test_stop_request_is_copied_onto_long_intents_only() -> None:
    # SPY enters (long, gets the stop). QQQ is held at close 50, so entry is false and hold-while
    # exits it: a flat intent, which must carry no stop.
    text = _rules({"gt": [CLOSE, 100]}, universe=("SPY", "QQQ"), stops=[STOP])
    frames = _frames(IDX, {"SPY": [150] * 5, "QQQ": [50] * 5})
    by = _by(_run(text, frames, IDX, 0, held=_held("QQQ", IDX, 0)))
    assert by["SPY"].target == "long"
    assert by["SPY"].stop is not None
    assert by["SPY"].stop.type == "atr_trailing"
    assert by["SPY"].stop.mult == pytest.approx(3.0)
    assert by["QQQ"].target == "flat"
    assert by["QQQ"].stop is None


def test_held_symbol_outside_universe_is_flattened() -> None:
    # TLT is held but not in the universe [SPY], so it is flattened.
    text = _rules({"gt": [CLOSE, 100]})
    frames = _frames(IDX, {"SPY": [90] * 5})
    ss = _run(text, frames, IDX, 0, held=_held("TLT", IDX, 0))
    intent = _by(ss)["TLT"]
    assert intent.target == "flat"
    assert intent.reason == "not in universe"


# --------------------------------------------------------------------------- schedule


def test_monthly_schedule_decides_only_on_first_bar_of_month() -> None:
    # MIDX: Jan 30 (decision, index 0), Jan 31 (no), Feb 1 (decision, index 2), Feb 2, Feb 5.
    text = _rules({"gt": [CLOSE, 100]}, freq="monthly")
    frames = _frames(MIDX, {"SPY": [150] * 5})
    cs = compile_strategy(load_strategy(text))
    feats = cs.prepare(frames)

    assert cs.is_decision_bar(MIDX[0], feats) is True
    assert cs.is_decision_bar(MIDX[1], feats) is False
    assert cs.is_decision_bar(MIDX[2], feats) is True

    skipped = cs.evaluate(feats, MIDX[1], {})
    assert skipped.decision is False
    assert skipped.intents == []

    decided = cs.evaluate(feats, MIDX[2], {})
    assert decided.decision is True
    assert "SPY" in _by(decided)


def test_force_decision_evaluates_a_non_decision_bar() -> None:
    # Jan 31 is not a decision bar, but force_decision=True evaluates it anyway.
    text = _rules({"gt": [CLOSE, 100]}, freq="monthly")
    frames = _frames(MIDX, {"SPY": [150] * 5})
    forced = _run(text, frames, MIDX, 1, force=True)
    assert forced.decision is True
    assert "SPY" in _by(forced)


# --------------------------------------------------------------------------- rotation


def test_rank_desc_selects_top_k_with_equal_weight() -> None:
    # Closes: SPY 10, QQQ 40, IWM 30, EFA 20. Top 2 by close, desc: QQQ (40), IWM (30).
    # Weight 1/k = 0.5 each. Unselected SPY and EFA get no intent (not held).
    closes = {"SPY": [10] * 5, "QQQ": [40] * 5, "IWM": [30] * 5, "EFA": [20] * 5}
    ss = _run(_rot(), _frames(IDX, closes), IDX, 0)
    by = _by(ss)
    assert set(by) == {"QQQ", "IWM"}
    assert by["QQQ"].weight == pytest.approx(0.5)
    assert by["IWM"].weight == pytest.approx(0.5)
    assert by["QQQ"].reason.startswith("rank 1/2")
    assert by["IWM"].reason.startswith("rank 2/2")


def test_rank_asc_selects_the_lowest_scores() -> None:
    # order asc: lowest closes first. SPY 10 and EFA 20.
    closes = {"SPY": [10] * 5, "QQQ": [40] * 5, "IWM": [30] * 5, "EFA": [20] * 5}
    ss = _run(_rot(order="asc"), _frames(IDX, closes), IDX, 0)
    assert set(_by(ss)) == {"SPY", "EFA"}


def test_tie_break_on_equal_scores_is_symbol_ascending() -> None:
    # SPY and QQQ both score 30. Top 1 desc: the tie goes to the symbol that sorts first, QQQ.
    closes = {"SPY": [30] * 5, "QQQ": [30] * 5, "IWM": [10] * 5, "EFA": [20] * 5}
    ss = _run(_rot(top=1), _frames(IDX, closes), IDX, 0)
    by = _by(ss)
    assert set(by) == {"QQQ"}
    assert by["QQQ"].weight == pytest.approx(1.0)


def test_pre_rank_filters_before_ranking() -> None:
    # Eligible: close > 15, so SPY (10) is out. Volume ranks SPY 900 > IWM 300 > EFA 200 > QQQ 100.
    # pre_rank drops SPY before ranking, so the top 2 eligible by volume are IWM and EFA.
    closes = {"SPY": [10] * 5, "QQQ": [20] * 5, "IWM": [30] * 5, "EFA": [40] * 5}
    vols = {"SPY": [900] * 5, "QQQ": [100] * 5, "IWM": [300] * 5, "EFA": [200] * 5}
    text = _rot(by={"price": "volume"}, eligibility={"gt": [CLOSE, 15]}, filter_mode="pre_rank")
    ss = _run(text, _frames(IDX, closes, vols), IDX, 0)
    assert set(_by(ss)) == {"IWM", "EFA"}


def test_post_rank_filters_after_top_k_and_leaves_slot_empty() -> None:
    # Same data, post_rank. Top 2 by volume over all: SPY 900, IWM 300. SPY is then ineligible,
    # so only IWM survives. Its weight stays 1/k = 0.5, with no fallback to fill the other slot.
    closes = {"SPY": [10] * 5, "QQQ": [20] * 5, "IWM": [30] * 5, "EFA": [40] * 5}
    vols = {"SPY": [900] * 5, "QQQ": [100] * 5, "IWM": [300] * 5, "EFA": [200] * 5}
    text = _rot(by={"price": "volume"}, eligibility={"gt": [CLOSE, 15]}, filter_mode="post_rank")
    ss = _run(text, _frames(IDX, closes, vols), IDX, 0)
    by = _by(ss)
    assert set(by) == {"IWM"}
    assert by["IWM"].weight == pytest.approx(0.5)


def test_nan_score_is_not_ranked() -> None:
    # QQQ's volume is NaN, so it has no score and cannot be selected. Top 2 by volume: SPY, IWM.
    closes = {"SPY": [10] * 5, "QQQ": [40] * 5, "IWM": [30] * 5, "EFA": [20] * 5}
    vols = {"SPY": [900] * 5, "QQQ": [NAN] * 5, "IWM": [300] * 5, "EFA": [200] * 5}
    ss = _run(_rot(by={"price": "volume"}), _frames(IDX, closes, vols), IDX, 0)
    assert set(_by(ss)) == {"SPY", "IWM"}


def test_fallback_takes_empty_slots_with_weight_k_minus_n_over_k() -> None:
    # Eligible (close > 15): only QQQ. k = 3, n = 1 selected, so 2 slots are empty.
    # QQQ gets 1/3. Fallback IEF gets (k - n)/k = 2/3.
    closes = {"SPY": [10] * 5, "QQQ": [20] * 5, "IWM": [10] * 5, "EFA": [10] * 5, "IEF": [50] * 5}
    text = _rot(top=3, eligibility={"gt": [CLOSE, 15]}, fallback={"symbol": "IEF"})
    by = _by(_run(text, _frames(IDX, closes), IDX, 0))
    assert by["QQQ"].weight == pytest.approx(1 / 3)
    assert by["IEF"].weight == pytest.approx(2 / 3)
    assert sum(i.weight for i in by.values()) == pytest.approx(1.0)


@pytest.mark.parametrize(("ief_close", "expect_fallback"), [(90, False), (110, True)])
def test_fallback_require_gates_the_fallback(ief_close: float, expect_fallback: bool) -> None:
    # Fallback IEF requires close > 100. At IEF 90 it is not allowed, so the slots stay empty.
    # At IEF 110 it is allowed, and gets 2/3.
    closes = {"SPY": [10] * 5, "QQQ": [20] * 5, "IWM": [10] * 5, "EFA": [10] * 5}
    closes["IEF"] = [ief_close] * 5
    text = _rot(
        top=3,
        eligibility={"gt": [CLOSE, 15]},
        fallback={"symbol": "IEF", "require": {"gt": [CLOSE, 100]}},
    )
    by = _by(_run(text, _frames(IDX, closes), IDX, 0))
    assert ("IEF" in by) is expect_fallback
    assert by["QQQ"].weight == pytest.approx(1 / 3)
    if expect_fallback:
        assert by["IEF"].weight == pytest.approx(2 / 3)


def test_held_but_unselected_is_flat_and_held_selected_stays_long() -> None:
    # Top 2 desc: QQQ (40) and IWM (30). Held SPY (10) is not selected, so flat.
    # Held QQQ is selected, so it stays long via its rank intent.
    closes = {"SPY": [10] * 5, "QQQ": [40] * 5, "IWM": [30] * 5, "EFA": [20] * 5}
    held = {**_held("SPY", IDX, 0), **_held("QQQ", IDX, 0)}
    by = _by(_run(_rot(), _frames(IDX, closes), IDX, 0, held=held))
    assert by["SPY"].target == "flat"
    assert by["SPY"].reason == "not selected"
    assert by["SPY"].stop is None
    assert by["QQQ"].target == "long"
    assert by["QQQ"].reason.startswith("rank 1/2")


def test_rotation_monthly_schedule_skips_non_decision_bars() -> None:
    # Monthly: Jan 31 is not a decision bar. A held SPY must NOT be flattened there (no opinion).
    # On Feb 1 (decision bar) it is flattened because it is not in the top 2 by close.
    closes = {"SPY": [10] * 5, "QQQ": [40] * 5, "IWM": [30] * 5, "EFA": [20] * 5}
    text = _rot(freq="monthly")
    frames = _frames(MIDX, closes)
    held = _held("SPY", MIDX, 0)

    skipped = _run(text, frames, MIDX, 1, held=held)
    assert skipped.decision is False
    assert skipped.intents == []

    decided = _run(text, frames, MIDX, 2, held=held)
    assert decided.decision is True
    assert _by(decided)["SPY"].target == "flat"
