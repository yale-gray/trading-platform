"""Trading calendar helpers for equities (NYSE) and 24/7 crypto.

Bar labels are UTC-midnight dates. A bar labelled ``d`` closes at
``bar_close_time(symbol, d)``. The XNYS calendar has fixed bounds
(``XNYS_START`` to ``XNYS_END``, inclusive), so results never depend on the run
date. Equity dates outside those bounds raise ``ValueError``. Crypto is unbounded.
Every function that depends on the current time takes ``now`` as an argument and
never reads the wall clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

XNYS_START = pd.Timestamp("2000-01-03")
XNYS_END = pd.Timestamp("2035-12-31")

_EQUITY_BUFFER_MIN = 60
_MAX_LOOKBACK_DAYS = 30


def _is_crypto(symbol: str) -> bool:
    return "/" in symbol


@lru_cache(maxsize=1)
def _xnys() -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS", start=XNYS_START, end=XNYS_END)


def _check_equity_date(d: date) -> None:
    if not XNYS_START.date() <= d <= XNYS_END.date():
        raise ValueError(
            f"{d} is outside the XNYS calendar bounds [{XNYS_START.date()}, {XNYS_END.date()}]"
        )


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def is_session(symbol: str, d: date) -> bool:
    """True if ``d`` is a trading bar date for ``symbol``."""
    if _is_crypto(symbol):
        return True
    _check_equity_date(d)
    return bool(_xnys().is_session(pd.Timestamp(d)))


def bar_close_time(symbol: str, d: date) -> datetime:
    """UTC time at which the bar labelled ``d`` closes."""
    if _is_crypto(symbol):
        return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=1)
    if not is_session(symbol, d):
        raise ValueError(f"{d} is not an NYSE session")
    close = _xnys().session_close(pd.Timestamp(d))
    return close.tz_convert(UTC).to_pydatetime()


def last_closed_bar_date(symbol: str, now: datetime, buffer_min: int = _EQUITY_BUFFER_MIN) -> date:
    """Latest bar label whose close (plus ``buffer_min`` for equities) is at or before ``now``."""
    now_utc = _require_aware(now)
    buffer = timedelta(0) if _is_crypto(symbol) else timedelta(minutes=buffer_min)
    candidate = now_utc.date()
    for _ in range(_MAX_LOOKBACK_DAYS):
        if is_session(symbol, candidate):
            if bar_close_time(symbol, candidate) + buffer <= now_utc:
                return candidate
        candidate -= timedelta(days=1)
    raise RuntimeError(f"no closed bar for {symbol} within {_MAX_LOOKBACK_DAYS} days of {now}")


def new_bar_available(
    symbol: str, last_seen: date | None, now: datetime, buffer_min: int = _EQUITY_BUFFER_MIN
) -> bool:
    """True if a bar newer than ``last_seen`` has closed by ``now``."""
    _require_aware(now)
    if last_seen is None:
        return True
    return last_closed_bar_date(symbol, now, buffer_min) > last_seen


def sessions(symbol: str, start: date, end: date) -> pd.DatetimeIndex:
    """Expected bar labels in ``[start, end]``, UTC-midnight, ascending."""
    empty = pd.DatetimeIndex([], tz=UTC)
    if _is_crypto(symbol):
        if end < start:
            return empty
        return pd.date_range(pd.Timestamp(start, tz=UTC), pd.Timestamp(end, tz=UTC), freq="D")
    _check_equity_date(start)
    _check_equity_date(end)
    if end < start:
        return empty
    idx = _xnys().sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    if len(idx) == 0:
        return empty
    return pd.DatetimeIndex(idx).tz_localize(UTC)


def next_bar_date(symbol: str, d: date) -> date:
    """Next bar label strictly after ``d``."""
    if _is_crypto(symbol):
        return d + timedelta(days=1)
    probe = d + timedelta(days=1)
    _check_equity_date(d)
    _check_equity_date(probe)
    nxt = _xnys().date_to_session(pd.Timestamp(probe), direction="next")
    return nxt.date()
