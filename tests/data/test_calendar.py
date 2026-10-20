from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradebot.data.calendar import (
    bar_close_time,
    is_session,
    last_closed_bar_date,
    new_bar_available,
    next_bar_date,
    sessions,
)

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_weekends_equity_false_crypto_true():
    sat = date(2024, 7, 6)
    sun = date(2024, 7, 7)
    assert not is_session("SPY", sat)
    assert not is_session("SPY", sun)
    assert is_session("BTC/USD", sat)
    assert is_session("BTC/USD", sun)


def test_holidays_not_sessions():
    assert not is_session("SPY", date(2024, 7, 4))
    assert not is_session("SPY", date(2024, 11, 28))
    assert is_session("SPY", date(2024, 7, 3))


def test_bar_close_time_early_close_and_normal():
    assert bar_close_time("SPY", date(2024, 11, 29)) == datetime(2024, 11, 29, 18, 0, tzinfo=UTC)
    assert bar_close_time("SPY", date(2024, 11, 27)) == datetime(2024, 11, 27, 21, 0, tzinfo=UTC)


def test_bar_close_time_rejects_non_session():
    with pytest.raises(ValueError):
        bar_close_time("SPY", date(2024, 7, 4))


def test_crypto_bar_close_is_next_utc_midnight():
    assert bar_close_time("BTC/USD", date(2024, 7, 4)) == datetime(2024, 7, 5, tzinfo=UTC)


def test_last_closed_bar_inside_buffer_is_previous_session():
    # 16:30 ET on 2024-07-02: 16:00 close + 60 min buffer not yet elapsed.
    now = _et(2024, 7, 2, 16, 30)
    assert last_closed_bar_date("SPY", now) == date(2024, 7, 1)
    assert not new_bar_available("SPY", date(2024, 7, 1), now)


def test_last_closed_bar_after_buffer_is_current_session():
    now = _et(2024, 7, 2, 17, 0)
    assert last_closed_bar_date("SPY", now) == date(2024, 7, 2)
    assert new_bar_available("SPY", date(2024, 7, 1), now)
    assert not new_bar_available("SPY", date(2024, 7, 2), now)


def test_monday_morning_points_at_friday():
    now = _et(2024, 7, 8, 8, 0)
    assert last_closed_bar_date("SPY", now) == date(2024, 7, 5)
    assert not new_bar_available("SPY", date(2024, 7, 5), now)


def test_holiday_points_at_last_session():
    # 2024-07-04 is a holiday; 2024-07-03 closed early at 13:00 ET.
    now = _et(2024, 7, 4, 12, 0)
    assert last_closed_bar_date("SPY", now) == date(2024, 7, 3)


def test_new_bar_available_when_never_seen():
    assert new_bar_available("SPY", None, _et(2024, 7, 2, 9, 0))


def test_crypto_last_closed_bar_boundary():
    assert last_closed_bar_date("BTC/USD", datetime(2024, 7, 5, 0, 0, tzinfo=UTC)) == date(
        2024, 7, 4
    )
    assert last_closed_bar_date("BTC/USD", datetime(2024, 7, 4, 23, 59, tzinfo=UTC)) == date(
        2024, 7, 3
    )


def test_naive_now_raises():
    with pytest.raises(ValueError):
        last_closed_bar_date("SPY", datetime(2024, 7, 2, 17, 0))
    with pytest.raises(ValueError):
        new_bar_available("SPY", None, datetime(2024, 7, 2, 17, 0))


def test_sessions_omits_holiday_and_weekend():
    idx = sessions("SPY", date(2024, 7, 1), date(2024, 7, 8))
    labels = [ts.date() for ts in idx]
    assert date(2024, 7, 4) not in labels
    assert date(2024, 7, 6) not in labels
    assert date(2024, 7, 7) not in labels
    assert labels == [
        date(2024, 7, 1),
        date(2024, 7, 2),
        date(2024, 7, 3),
        date(2024, 7, 5),
        date(2024, 7, 8),
    ]
    assert str(idx.tz) == "UTC"
    assert idx.is_monotonic_increasing
    assert idx[0] == pd.Timestamp("2024-07-01", tz="UTC")


def test_sessions_crypto_every_day():
    idx = sessions("BTC/USD", date(2024, 7, 1), date(2024, 7, 3))
    assert len(idx) == 3
    assert str(idx.tz) == "UTC"


def test_sessions_empty_range():
    assert len(sessions("SPY", date(2024, 7, 6), date(2024, 7, 7))) == 0
    assert len(sessions("BTC/USD", date(2024, 7, 3), date(2024, 7, 1))) == 0


def test_next_bar_date_skips_holiday_weekend():
    assert next_bar_date("SPY", date(2024, 7, 3)) == date(2024, 7, 5)
    assert next_bar_date("SPY", date(2024, 7, 6)) == date(2024, 7, 8)
    assert next_bar_date("BTC/USD", date(2024, 7, 6)) == date(2024, 7, 7)


def test_equity_before_xnys_start_raises_value_error():
    with pytest.raises(ValueError, match="XNYS calendar bounds"):
        is_session("SPY", date(1999, 12, 31))


def test_equity_after_xnys_end_raises_value_error():
    with pytest.raises(ValueError, match="XNYS calendar bounds"):
        is_session("SPY", date(2040, 1, 2))


def test_next_bar_date_at_xnys_end_raises_value_error():
    with pytest.raises(ValueError, match="XNYS calendar bounds"):
        next_bar_date("SPY", date(2035, 12, 31))


def test_last_closed_bar_date_early_close_buffer_boundary():
    # 2024-11-29 closes 13:00 ET = 18:00 UTC; with the 60 min buffer it is closed at 19:00 UTC.
    assert last_closed_bar_date("SPY", datetime(2024, 11, 29, 19, 0, tzinfo=UTC)) == date(
        2024, 11, 29
    )
    assert last_closed_bar_date("SPY", datetime(2024, 11, 29, 18, 59, tzinfo=UTC)) == date(
        2024, 11, 27
    )


def test_bar_close_time_winter_vs_summer():
    assert bar_close_time("SPY", date(2024, 1, 10)) == datetime(2024, 1, 10, 21, 0, tzinfo=UTC)
    assert bar_close_time("SPY", date(2024, 7, 8)) == datetime(2024, 7, 8, 20, 0, tzinfo=UTC)
