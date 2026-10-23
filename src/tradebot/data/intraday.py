"""Intraday (15-minute) regular-session calendar for US equities (ADR-0001 A21).

Conventions:

* A bar is labelled by its **open** time (UTC). The bar is complete at ``open + 15 min``.
* Regular session only: opens run from the session open (09:30 ET) up to, but not
  including, the session close (16:00 ET, or 13:00 ET on an early close), taken from
  ``exchange_calendars`` XNYS. A normal day has 26 bars; an early close has 14.
* DST needs no special handling: opens are derived from the calendar's UTC session
  open/close, so 09:30 ET is 13:30 UTC in summer and 14:30 UTC in winter.
* "Session date" is the New York calendar date of the session (``date``).

Functions that depend on the current time take ``now``; none reads the wall clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from tradebot.data.calendar import XNYS_END, XNYS_START, _check_equity_date, _require_aware, _xnys

BAR_MINUTES = 15
BAR_DELTA = timedelta(minutes=BAR_MINUTES)
NY_TZ = "America/New_York"
_BAR_NS = BAR_MINUTES * 60 * 1_000_000_000


def _empty() -> pd.DatetimeIndex:
    return pd.DatetimeIndex([], tz=UTC, name="ts")


@lru_cache(maxsize=64)
def _opens_in_range(start: date, end: date) -> pd.DatetimeIndex:
    cal = _xnys()
    sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    if len(sessions) == 0:
        return _empty()
    opens = cal.opens.loc[sessions].dt.tz_convert(UTC)
    closes = cal.closes.loc[sessions].dt.tz_convert(UTC)
    o = opens.to_numpy(dtype="datetime64[ns]").astype("int64")
    c = closes.to_numpy(dtype="datetime64[ns]").astype("int64")
    counts = -((o - c) // _BAR_NS)  # ceil((c - o) / bar)
    total = int(counts.sum())
    starts = np.repeat(o, counts)
    within = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
    ns = starts + within * _BAR_NS
    return pd.DatetimeIndex(ns.astype("datetime64[ns]"), tz=UTC, name="ts")


def bar_opens(start: date, end: date) -> pd.DatetimeIndex:
    """Expected regular-session 15m bar opens (UTC) for session dates in ``[start, end]``."""
    if end < start:
        return _empty()
    _check_equity_date(start)
    _check_equity_date(end)
    return _opens_in_range(start, end)


def session_bar_opens(d: date) -> pd.DatetimeIndex:
    """Bar opens (UTC) of one session; empty if ``d`` is not an NYSE session."""
    return bar_opens(d, d)


def session_date(open_ts: pd.Timestamp | datetime) -> date:
    """New York calendar date of a bar open (the session it belongs to)."""
    ts = pd.Timestamp(open_ts)
    if ts.tzinfo is None:
        raise ValueError("bar open must be timezone-aware")
    return ts.tz_convert(NY_TZ).date()


def bar_complete_time(open_ts: pd.Timestamp | datetime) -> pd.Timestamp:
    """Instant at which the bar opened at ``open_ts`` is complete (open + 15 min)."""
    ts = pd.Timestamp(open_ts)
    if ts.tzinfo is None:
        raise ValueError("bar open must be timezone-aware")
    return ts.tz_convert(UTC) + BAR_DELTA


def is_bar_open(open_ts: pd.Timestamp | datetime) -> bool:
    """True if ``open_ts`` is a regular-session 15m bar open."""
    ts = pd.Timestamp(open_ts).tz_convert(UTC)
    d = session_date(ts)
    if not XNYS_START.date() <= d <= XNYS_END.date():
        return False
    return ts in session_bar_opens(d)


def is_last_bar_of_session(open_ts: pd.Timestamp | datetime) -> bool:
    """True if the bar opened at ``open_ts`` is the final bar of its session
    (15:45 ET on a normal day, 12:45 ET on a 13:00 early close)."""
    ts = pd.Timestamp(open_ts).tz_convert(UTC)
    opens = session_bar_opens(session_date(ts))
    return len(opens) > 0 and ts == opens[-1]


def session_close_time(d: date) -> pd.Timestamp:
    """UTC session close for session date ``d`` (16:00 ET, or 13:00 ET early close)."""
    _check_equity_date(d)
    if not _xnys().is_session(pd.Timestamp(d)):
        raise ValueError(f"{d} is not an NYSE session")
    return _xnys().session_close(pd.Timestamp(d)).tz_convert(UTC)


def last_complete_bar_open(now: datetime) -> pd.Timestamp | None:
    """Open of the latest bar complete at ``now`` (``open + 15 min <= now``), or None."""
    now_utc = pd.Timestamp(_require_aware(now))
    day = now_utc.tz_convert(NY_TZ).date()
    for back in range(0, 12):
        d = day - timedelta(days=back)
        if d < XNYS_START.date():
            break
        if d > XNYS_END.date():
            continue
        opens = session_bar_opens(d)
        done = opens[opens + BAR_DELTA <= now_utc]
        if len(done):
            return done[-1]
    return None


def complete_mask(index: pd.DatetimeIndex, now: datetime) -> np.ndarray:
    """Boolean mask of bars in ``index`` that are complete at ``now``."""
    cutoff = pd.Timestamp(_require_aware(now))
    return np.asarray(index + BAR_DELTA <= cutoff)
