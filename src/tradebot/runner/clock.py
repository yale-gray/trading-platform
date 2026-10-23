"""Bar clocks: which bar dates are complete and due (SPEC-004 "Bar clocks").

Pure functions of ``now`` (the injected clock's value) and the calendars in
``tradebot.data.calendar``; nothing here reads the wall clock.

* Equity bar D is complete at ``close(D) + EQUITY_CLOSE_DELAY`` (default 30 min, XNYS
  calendar, so early closes are handled); only calendar sessions are bars.
* Crypto bar D (UTC day) is complete at ``(D + 1 day) 00:00 UTC + 10 min``.
* A mixed portfolio follows the union clock (ADR D6): date D is due once every market
  that has a bar on D is complete.
* A 15-minute equity bar opened at ``t`` is complete at ``t + 15 min + INTRADAY_DELAY_MIN``
  (ADR-0001 A21; XNYS sessions, early closes respected): see :func:`due_bars_15m`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from tradebot.data.calendar import XNYS_END, XNYS_START, bar_close_time, is_session

CRYPTO_DELAY_MIN = 10
INTRADAY_DELAY_MIN = 1
"""Minutes after a 15m bar closes before it counts as due (data-vendor lag)."""
BAR_15M = timedelta(minutes=15)

__all__ = [
    "CRYPTO_DELAY_MIN",
    "INTRADAY_DELAY_MIN",
    "bar_complete_time",
    "due_bars_15m",
    "due_dates",
    "has_bar",
    "market_of",
    "next_equity_run_after",
    "pause_end_bar",
]


def market_of(symbol: str) -> str:
    """``"crypto"`` for pairs such as ``BTC/USD``, else ``"equity"``."""
    return "crypto" if "/" in symbol else "equity"


def has_bar(market: str, d: date) -> bool:
    """True if ``market`` has a bar labelled ``d`` (equities: an XNYS session; crypto: always)."""
    return True if market == "crypto" else is_session("SPY", d)


def bar_complete_time(market: str, d: date, equity_delay_min: int = 30) -> datetime | None:
    """UTC instant bar ``d`` becomes known, or ``None`` if the market has no bar that day."""
    if market == "crypto":
        return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(
            days=1, minutes=CRYPTO_DELAY_MIN
        )
    if not has_bar("equity", d):
        return None
    return bar_close_time("SPY", d) + timedelta(minutes=equity_delay_min)


def due_dates(
    markets: Iterable[str],
    *,
    start: date,
    after: date | None,
    now: datetime,
    equity_delay_min: int = 30,
) -> list[date]:
    """Bar dates ``>= start`` and ``> after`` that are complete at ``now``, ascending.

    Dates on which none of the portfolio's markets has a bar (holidays for an equity-only
    portfolio) are skipped. Because bar N+1 is never due while bar N is not, the result is
    always a gap-free prefix of the portfolio's own calendar.
    """
    mk = sorted(set(markets))
    now = now.astimezone(UTC)
    lo = start if after is None else max(start, after + timedelta(days=1))
    out: list[date] = []
    d = lo
    last = now.date()
    while d <= last:
        times = [t for m in mk if (t := bar_complete_time(m, d, equity_delay_min)) is not None]
        if times and max(times) <= now:
            out.append(d)
        elif times:
            break  # not complete yet; nothing later can be due either
        d += timedelta(days=1)
    return out


def session_bounds(d: date) -> tuple[datetime, datetime] | None:
    """UTC ``(open, close)`` of the XNYS session on ``d`` (early closes included), or ``None``."""
    if not is_session("SPY", d):
        return None
    xcal = _xnys_calendar()
    label = pd.Timestamp(d)
    return (
        xcal.session_open(label).to_pydatetime().astimezone(UTC),
        xcal.session_close(label).to_pydatetime().astimezone(UTC),
    )


def _xnys_calendar():  # type: ignore[no-untyped-def]
    from tradebot.data.calendar import _xnys

    return _xnys()


def due_bars_15m(
    *,
    start: date,
    after: pd.Timestamp | None,
    now: datetime,
    delay_min: int = INTRADAY_DELAY_MIN,
    limit: int | None = None,
) -> list[pd.Timestamp]:
    """Completed 15m bar opens (UTC) that are ``>= start`` (date) and ``> after``, ascending.

    A bar opened at ``t`` is due once ``t + 15 min + delay_min <= now``. Bars come only from
    XNYS sessions, so the result is a gap-free prefix of the regular-session calendar. ``limit``
    stops the walk early (callers use it to detect a catch-up that is too long).
    """
    now = now.astimezone(UTC)
    d = start if after is None else max(start, after.date())
    out: list[pd.Timestamp] = []
    while d <= now.date():
        if XNYS_START.date() <= d <= XNYS_END.date():
            bounds = session_bounds(d)
        else:
            bounds = None
        if bounds is not None:
            t, close = bounds
            while t < close:
                stamp = pd.Timestamp(t)
                if after is None or stamp > after:
                    if t + BAR_15M + timedelta(minutes=delay_min) > now:
                        return out  # not complete yet; nothing later can be due either
                    out.append(stamp)
                    if limit is not None and len(out) >= limit:
                        return out
                t += BAR_15M
        d += timedelta(days=1)
    return out


def pause_end_bar(markets: Iterable[str], tripped: date, pause_bars: int) -> date | None:
    """Bar date of the ``pause_bars``-th portfolio bar after trip bar ``tripped`` (the re-arm bar).

    Walks the portfolio's own calendar (a day counts if any of its markets has a bar).
    ``None`` if the walk leaves the calendar bounds.
    """
    mk = sorted(set(markets)) or ["equity"]
    d, left = tripped, pause_bars
    try:
        while left > 0:
            d += timedelta(days=1)
            if any(has_bar(m, d) for m in mk):
                left -= 1
    except ValueError:
        return None
    return d


def next_equity_run_after(now: datetime, equity_delay_min: int = 30) -> datetime | None:
    """UTC instant of the next equity bar completion strictly after ``now`` (close + delay)."""
    now = now.astimezone(UTC)
    d = now.date() - timedelta(days=1)
    try:
        for _ in range(15):
            t = bar_complete_time("equity", d, equity_delay_min)
            if t is not None and t > now:
                return t
            d += timedelta(days=1)
    except ValueError:
        return None
    return None
