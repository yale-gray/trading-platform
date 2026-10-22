"""Bar clocks: holidays, early closes, crypto and the union clock (SPEC-004 AC 5).

Named ``test_bar_clock`` (the spec's ``test_clock.py`` would collide with ``tests/unit``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tradebot.runner.clock import bar_complete_time, due_dates, has_bar, market_of

pytestmark = pytest.mark.unit


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def test_market_of() -> None:
    assert market_of("SPY") == "equity"
    assert market_of("BTC/USD") == "crypto"


def test_equity_skips_weekends_and_holidays() -> None:
    # Thanksgiving 2025-11-27 (Thu) and the weekend are not bars.
    got = due_dates(["equity"], start=date(2025, 11, 24), after=None, now=utc(2025, 12, 2, 2))
    assert got == [
        date(2025, 11, 24),
        date(2025, 11, 25),
        date(2025, 11, 26),
        date(2025, 11, 28),  # early close Friday
        date(2025, 12, 1),
    ]
    xmas = due_dates(["equity"], start=date(2025, 12, 22), after=None, now=utc(2025, 12, 30))
    assert date(2025, 12, 25) not in xmas
    assert date(2025, 12, 24) in xmas and date(2025, 12, 26) in xmas
    assert not has_bar("equity", date(2025, 12, 25))
    assert bar_complete_time("equity", date(2025, 12, 25)) is None


def test_early_close_day_is_due_at_1330_et() -> None:
    d = date(2025, 11, 28)  # Black Friday, close 13:00 ET = 18:00 UTC
    assert bar_complete_time("equity", d) == utc(2025, 11, 28, 18, 30)
    assert due_dates(["equity"], start=d, after=None, now=utc(2025, 11, 28, 18, 29)) == []
    assert due_dates(["equity"], start=d, after=None, now=utc(2025, 11, 28, 18, 30)) == [d]
    # a normal winter day closes 16:00 ET = 21:00 UTC, due 21:30
    n = date(2025, 11, 26)
    assert bar_complete_time("equity", n) == utc(2025, 11, 26, 21, 30)


def test_equity_delay_follows_dst() -> None:
    summer = date(2025, 7, 1)  # EDT: close 20:00 UTC
    assert bar_complete_time("equity", summer, 30) == utc(2025, 7, 1, 20, 30)
    assert bar_complete_time("equity", summer, 0) == utc(2025, 7, 1, 20, 0)


def test_crypto_daily_including_weekends_with_10_minute_delay() -> None:
    sat = date(2025, 11, 29)
    assert has_bar("crypto", sat)
    assert bar_complete_time("crypto", sat) == utc(2025, 11, 30, 0, 10)
    assert due_dates(["crypto"], start=sat, after=None, now=utc(2025, 11, 30, 0, 9)) == []
    assert due_dates(["crypto"], start=sat, after=None, now=utc(2025, 11, 30, 0, 10)) == [sat]
    got = due_dates(["crypto"], start=date(2025, 11, 28), after=None, now=utc(2025, 12, 2, 0, 10))
    assert got == [date(2025, 11, 28) + timedelta(days=i) for i in range(4)]


def test_mixed_follows_the_union_clock() -> None:
    mixed = ["crypto", "equity"]
    # Friday 2025-11-21 (a session): needs the crypto close (Sat 00:10), not the equity one
    assert due_dates(mixed, start=date(2025, 11, 21), after=None, now=utc(2025, 11, 21, 23)) == []
    assert due_dates(mixed, start=date(2025, 11, 21), after=None, now=utc(2025, 11, 22, 0, 10)) == [
        date(2025, 11, 21)
    ]
    # a weekend or holiday day is a bar through the crypto side alone
    got = due_dates(mixed, start=date(2025, 11, 22), after=None, now=utc(2025, 11, 24, 0, 10))
    assert got == [date(2025, 11, 22), date(2025, 11, 23)]
    assert date(2025, 12, 25) in due_dates(
        mixed, start=date(2025, 12, 24), after=None, now=utc(2025, 12, 27)
    )


def test_after_and_start_bounds() -> None:
    now = utc(2025, 12, 10)
    all_ = due_dates(["equity"], start=date(2025, 12, 1), after=None, now=now)
    assert due_dates(["equity"], start=date(2025, 12, 1), after=all_[2], now=now) == all_[3:]
    # nothing before `start` and nothing after `now`
    assert all_[0] == date(2025, 12, 1) and all_[-1] <= now.date()


def test_prefix_is_gap_free_when_the_latest_bar_is_incomplete() -> None:
    got = due_dates(["equity"], start=date(2025, 12, 1), after=None, now=utc(2025, 12, 3, 21, 0))
    assert got == [date(2025, 12, 1), date(2025, 12, 2)]  # Dec 3 closes 21:00, due 21:30
