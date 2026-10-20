"""Unit tests for tradebot.clock (SPEC-005 T1)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tradebot.clock import Clock, FixedClock, SystemClock

pytestmark = pytest.mark.unit


def test_system_clock_is_utc_and_current() -> None:
    before = datetime.now(UTC)
    value = SystemClock().now()
    after = datetime.now(UTC)
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)
    assert before - timedelta(seconds=5) <= value <= after + timedelta(seconds=5)


def test_system_clock_call_matches_now() -> None:
    clock = SystemClock()
    delta = abs(clock() - clock.now())
    assert delta < timedelta(seconds=5)


def test_fixed_clock_is_stable_until_advanced() -> None:
    start = datetime(2024, 12, 31, 23, 0, tzinfo=UTC)
    clock = FixedClock(start)
    assert clock.now() == start
    assert clock.now() == start


def test_fixed_clock_advance_one_day() -> None:
    start = datetime(2024, 12, 31, 23, 0, tzinfo=UTC)
    clock = FixedClock(start)
    clock.advance(timedelta(days=1))
    assert clock.now() == datetime(2025, 1, 1, 23, 0, tzinfo=UTC)


def test_fixed_clock_advance_zero_is_allowed() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    clock = FixedClock(start)
    clock.advance(timedelta(0))
    assert clock.now() == start


def test_fixed_clock_advance_negative_raises() -> None:
    clock = FixedClock(datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))


def test_fixed_clock_set_forward_works() -> None:
    clock = FixedClock(datetime(2024, 1, 1, tzinfo=UTC))
    target = datetime(2024, 6, 1, tzinfo=UTC)
    clock.set(target)
    assert clock.now() == target


def test_fixed_clock_set_backwards_raises() -> None:
    clock = FixedClock(datetime(2024, 6, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        clock.set(datetime(2024, 1, 1, tzinfo=UTC))


def test_fixed_clock_rejects_naive_start() -> None:
    with pytest.raises(ValueError):
        FixedClock(datetime(2024, 1, 1))


def test_fixed_clock_set_rejects_naive() -> None:
    clock = FixedClock(datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        clock.set(datetime(2024, 2, 1))


def test_fixed_clock_converts_non_utc_to_utc_same_instant() -> None:
    est = timezone(timedelta(hours=-5))
    local = datetime(2024, 1, 1, 12, 0, tzinfo=est)
    clock = FixedClock(local)
    value = clock.now()
    assert value == datetime(2024, 1, 1, 17, 0, tzinfo=UTC)
    assert value.utcoffset() == timedelta(0)


def test_fixed_clock_call_equals_now() -> None:
    clock = FixedClock(datetime(2024, 3, 4, 5, 6, tzinfo=UTC))
    assert clock() == clock.now()


def test_both_clocks_satisfy_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FixedClock(datetime(2024, 1, 1, tzinfo=UTC)), Clock)
