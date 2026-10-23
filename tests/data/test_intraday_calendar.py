"""Intraday 15m regular-session calendar (ADR-0001 A21)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from tradebot.data.intraday import (
    bar_complete_time,
    bar_opens,
    complete_mask,
    is_bar_open,
    is_last_bar_of_session,
    last_complete_bar_open,
    session_bar_opens,
    session_date,
)


def test_normal_day_has_26_bars() -> None:
    opens = session_bar_opens(date(2024, 6, 12))
    assert len(opens) == 26
    assert opens[0] == pd.Timestamp("2024-06-12 13:30", tz="UTC")  # 09:30 EDT
    assert opens[-1] == pd.Timestamp("2024-06-12 19:45", tz="UTC")  # 15:45 EDT


def test_day_after_thanksgiving_early_close_has_14_bars() -> None:
    opens = session_bar_opens(date(2024, 11, 29))
    assert len(opens) == 14
    assert opens[0] == pd.Timestamp("2024-11-29 14:30", tz="UTC")  # 09:30 EST
    assert opens[-1] == pd.Timestamp("2024-11-29 17:45", tz="UTC")  # 12:45 EST


def test_dst_transitions_shift_utc_but_not_local_time() -> None:
    before = session_bar_opens(date(2016, 3, 11))  # EST
    after = session_bar_opens(date(2016, 3, 14))  # EDT, after the 13 Mar switch
    assert before[0] == pd.Timestamp("2016-03-11 14:30", tz="UTC")
    assert after[0] == pd.Timestamp("2016-03-14 13:30", tz="UTC")
    assert len(before) == len(after) == 26
    fall_before = session_bar_opens(date(2016, 11, 4))  # EDT
    fall_after = session_bar_opens(date(2016, 11, 7))  # EST, after the 6 Nov switch
    assert fall_before[0] == pd.Timestamp("2016-11-04 13:30", tz="UTC")
    assert fall_after[0] == pd.Timestamp("2016-11-07 14:30", tz="UTC")


def test_weekend_and_holiday_have_no_bars() -> None:
    assert len(session_bar_opens(date(2024, 6, 15))) == 0  # Saturday
    assert len(session_bar_opens(date(2024, 12, 25))) == 0  # Christmas


def test_range_counts_and_ordering() -> None:
    opens = bar_opens(date(2024, 11, 27), date(2024, 11, 29))  # Wed, Thu (closed), Fri early
    assert len(opens) == 26 + 14
    assert opens.is_monotonic_increasing and opens.is_unique
    assert str(opens.tz) == "UTC"
    assert len(bar_opens(date(2024, 6, 13), date(2024, 6, 12))) == 0


def test_bar_complete_time_and_last_bar() -> None:
    first = pd.Timestamp("2024-06-12 13:30", tz="UTC")
    assert bar_complete_time(first) == pd.Timestamp("2024-06-12 13:45", tz="UTC")
    assert not is_last_bar_of_session(first)
    assert is_last_bar_of_session(pd.Timestamp("2024-06-12 19:45", tz="UTC"))
    assert not is_last_bar_of_session(pd.Timestamp("2024-06-12 19:30", tz="UTC"))
    assert is_last_bar_of_session(pd.Timestamp("2024-11-29 17:45", tz="UTC"))  # early close
    assert not is_last_bar_of_session(pd.Timestamp("2024-11-29 19:45", tz="UTC"))  # after close


def test_is_bar_open_rejects_extended_hours_and_misaligned() -> None:
    assert is_bar_open(pd.Timestamp("2024-06-12 13:30", tz="UTC"))
    assert not is_bar_open(pd.Timestamp("2024-06-12 13:15", tz="UTC"))  # pre-market
    assert not is_bar_open(pd.Timestamp("2024-06-12 20:00", tz="UTC"))  # after close
    assert not is_bar_open(pd.Timestamp("2024-06-12 13:40", tz="UTC"))


def test_last_complete_bar_open() -> None:
    # 10:00 ET: bar 09:30 is complete at 09:45, bar 09:45 at 10:00 -> 09:45 is the last complete.
    now = datetime(2024, 6, 12, 14, 0, tzinfo=UTC)
    assert last_complete_bar_open(now) == pd.Timestamp("2024-06-12 13:45", tz="UTC")
    # Just before the first bar completes the previous session's last bar is returned.
    early = datetime(2024, 6, 12, 13, 44, tzinfo=UTC)
    assert last_complete_bar_open(early) == pd.Timestamp("2024-06-11 19:45", tz="UTC")
    # After the close: last bar of the day.
    late = datetime(2024, 6, 12, 22, 0, tzinfo=UTC)
    assert last_complete_bar_open(late) == pd.Timestamp("2024-06-12 19:45", tz="UTC")
    # Weekend rolls back to Friday's final bar.
    sat = datetime(2024, 6, 15, 15, 0, tzinfo=UTC)
    assert last_complete_bar_open(sat) == pd.Timestamp("2024-06-14 19:45", tz="UTC")


def test_complete_mask_and_session_date() -> None:
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2024-06-12 13:30", tz="UTC"), pd.Timestamp("2024-06-12 13:45", tz="UTC")]
    )
    mask = complete_mask(idx, datetime(2024, 6, 12, 13, 45, tzinfo=UTC))
    assert mask.tolist() == [True, False]
    assert session_date(pd.Timestamp("2024-06-12 19:45", tz="UTC")) == date(2024, 6, 12)
