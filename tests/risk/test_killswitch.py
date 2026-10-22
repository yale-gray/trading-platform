"""Drawdown kill switch (SPEC-003 section 7, I11/I12, AC5; ADR-0001 D8).

Trip rule: ``1 - equity/peak >= threshold``, equivalently ``equity <= peak * (1 - threshold)``.
Every expected number below is worked out by hand in a comment.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tradebot.risk.killswitch import DEFAULT_THRESHOLD, KillSwitch

pytestmark = pytest.mark.unit

D = Decimal
D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
CAP = D("100000.00")


def test_default_threshold_is_twenty_percent() -> None:
    assert DEFAULT_THRESHOLD == D("0.20")


def test_trips_at_exactly_twenty_percent_drawdown() -> None:
    # peak 100000. equity 80000: 1 - 80000/100000 = 0.20, and 0.20 >= 0.20, so it trips.
    ks = KillSwitch(CAP)
    obs = ks.observe(D("80000.00"), D1)
    assert obs.tripped_now is True
    assert obs.killed is True
    assert ks.tripped_asof == D1


def test_does_not_trip_at_nineteen_point_nine_nine_percent() -> None:
    # equity 80010: 1 - 80010/100000 = 0.1999 < 0.20, so no trip.
    ks = KillSwitch(CAP)
    obs = ks.observe(D("80010.00"), D1)
    assert obs.tripped_now is False
    assert obs.killed is False
    assert ks.tripped_asof is None


def test_reported_drawdown_is_one_minus_equity_over_peak() -> None:
    # peak 100000, equity 90000: drawdown = 1 - 0.9 = 0.10, not tripped (0.10 < 0.20).
    ks = KillSwitch(CAP)
    obs = ks.observe(D("90000.00"), D1)
    assert obs.drawdown == D("0.10")
    assert obs.killed is False


def test_trip_is_sticky_for_n_bars_even_after_recovery_and_blocks_buys() -> None:
    # Trip at 79000 (drawdown 21%). With a 3-bar pause, recovery to the old peak must not
    # clear it on bars 1 and 2 after the trip; the 3rd bar after the trip re-arms.
    ks = KillSwitch(CAP, pause_bars=3)
    assert ks.observe(D("79000.00"), D1).tripped_now is True

    recovered = ks.observe(D("100000.00"), D2)
    assert recovered.killed is True and recovered.tripped_now is False
    assert ks.allows_buy() is False
    assert ks.rebalance_frozen is True

    still = ks.observe(D("99000.00"), D3)
    assert still.killed is True and still.rearmed_now is False
    assert ks.peak_equity == CAP  # peak is frozen during the pause


def test_rearms_after_pause_bars_with_peak_at_current_equity() -> None:
    ks = KillSwitch(CAP, pause_bars=3)
    ks.observe(D("79000.00"), D1)
    ks.observe(D("80000.00"), D2)
    ks.observe(D("81000.00"), D3)
    obs = ks.observe(D("82000.00"), date(2024, 1, 5))  # 3rd bar after the trip
    assert obs.rearmed_now is True and obs.killed is False and obs.tripped_now is False
    assert obs.peak_equity == D("82000.00") == ks.peak_equity
    assert ks.allows_buy() is True and ks.tripped_asof is None
    # Next trip is measured from the new peak: 82000 * 0.8 = 65600.
    assert ks.observe(D("65601.00"), date(2024, 1, 6)).killed is False
    assert ks.observe(D("65600.00"), date(2024, 1, 7)).tripped_now is True


def test_pause_of_twenty_bars_by_default() -> None:
    ks = KillSwitch(CAP)
    assert ks.pause_bars == 20
    ks.observe(D("70000.00"), D1)
    for i in range(19):
        assert ks.observe(D("70000.00"), D2 + timedelta(days=i)).killed is True
    assert ks.observe(D("70000.00"), D2 + timedelta(days=19)).rearmed_now is True


def test_pause_bars_must_be_positive() -> None:
    with pytest.raises(ValueError, match="pause_bars"):
        KillSwitch(CAP, pause_bars=0)


def test_manual_reset_ends_the_pause_early() -> None:
    ks = KillSwitch(CAP, pause_bars=20)
    ks.observe(D("70000.00"), D1)
    ks.observe(D("71000.00"), D2)
    ks.reset("op", "early", D3)
    assert ks.killed is False and ks.bars_paused == 0
    assert ks.peak_equity == D("71000.00")
    # a later re-trip starts a fresh pause count
    ks.observe(D("56800.00"), date(2024, 1, 5))
    assert ks.killed is True and ks.bars_paused == 0


def test_running_peak_rises_above_initial_capital() -> None:
    # Peak rises to 120000. From that peak, 20% is 96000. From the initial 100000, 96000 would
    # only be a 4% drawdown, so the trip at 96000 can only come from the raised peak.
    ks = KillSwitch(CAP)
    up = ks.observe(D("120000.00"), D1)
    assert up.peak_equity == D("120000.00")
    assert up.killed is False

    just_above = ks.observe(D("96001.00"), D2)  # 1 - 96001/120000 = 0.19999... < 0.20
    assert just_above.killed is False

    at_line = ks.observe(D("96000.00"), D3)  # 1 - 96000/120000 = 0.20 exactly
    assert at_line.tripped_now is True
    assert at_line.peak_equity == D("120000.00")


def test_trip_fires_on_the_transition_only_not_every_bar() -> None:
    # tripped_now is True only on the bar that crosses the line.
    ks = KillSwitch(CAP)
    t1 = ks.observe(D("79000.00"), D1)
    t2 = ks.observe(D("78000.00"), D2)
    t3 = ks.observe(D("50000.00"), D3)
    assert t1.tripped_now is True
    assert t2.tripped_now is False
    assert t3.tripped_now is False
    assert t2.killed is True and t3.killed is True
    assert ks.tripped_asof == D1  # not moved by later bars


def test_custom_threshold_is_honoured() -> None:
    # threshold 0.10: trip iff equity <= 100000 * 0.90 = 90000.
    ks = KillSwitch(CAP, threshold=D("0.10"))
    assert ks.threshold == D("0.10")
    assert ks.observe(D("90001.00"), D1).killed is False
    assert ks.observe(D("90000.00"), D2).tripped_now is True


def test_default_threshold_does_not_trip_at_ten_percent() -> None:
    # Same 10% drawdown, default 20% threshold: 0.10 < 0.20, so no trip.
    ks = KillSwitch(CAP)
    assert ks.observe(D("90000.00"), D1).killed is False


def test_reset_requires_tripped() -> None:
    ks = KillSwitch(CAP)
    with pytest.raises(ValueError, match="not tripped"):
        ks.reset("op", "note", D1)


def test_reset_clears_trip_and_rebases_peak_to_last_equity() -> None:
    # Trip at 70000 (30% down). Reset sets peak = last observed equity = 70000.00.
    ks = KillSwitch(CAP)
    ks.observe(D("70000.00"), D1)
    assert ks.killed is True

    ks.reset("operator", "manual review", D2)
    assert ks.killed is False
    assert ks.tripped_asof is None
    assert ks.peak_equity == D("70000.00")
    assert len(ks.resets) == 1
    assert D(ks.resets[0]["peak_equity"]) == D("70000.00")

    # Peak re-based to 70000. 69000 is 1.43% below it, so no trip.
    # Measured against the old 100000 peak it would be 31% down and would trip, so this
    # distinguishes a re-based peak from a stale one.
    after = ks.observe(D("69000.00"), D3)
    assert after.killed is False


def test_state_round_trip_preserves_killed_and_peak() -> None:
    # threshold 0.15: trip iff equity <= 120000 * 0.85 = 102000. Equity 100000 trips.
    ks = KillSwitch(CAP, threshold=D("0.15"), pause_bars=5)
    ks.observe(D("120000.00"), D1)
    ks.observe(D("100000.00"), D2)
    assert ks.killed is True

    state = ks.to_state()
    restored = KillSwitch.from_state(json.loads(json.dumps(state)))  # must be JSON-serialisable

    assert restored.killed is True
    assert restored.peak_equity == D("120000.00")
    assert restored.threshold == D("0.15")
    assert restored.tripped_asof == D2
    assert restored.last_equity == D("100000.00")
    assert restored.pause_bars == 5 and restored.bars_paused == 0

    # Restored switch stays tripped and does not re-trip on the next bar.
    again = restored.observe(D("130000.00"), D3)
    assert again.killed is True
    assert again.tripped_now is False
    assert again.peak_equity == D("120000.00")  # peak is frozen while paused
    assert restored.bars_paused == 1


def test_state_saved_before_a18_restores_with_defaults() -> None:
    old = {
        "threshold": "0.20",
        "peak_equity": "100000.00",
        "last_equity": "79000.00",
        "killed": True,
        "tripped_asof": "2024-01-02",
        "resets": [],
    }
    ks = KillSwitch.from_state(old, default_pause_bars=7)
    assert ks.killed is True and ks.pause_bars == 7 and ks.bars_paused == 0
    assert KillSwitch.from_state(old).pause_bars == 20
