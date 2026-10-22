"""Bar clocks: which bar dates are complete and due (SPEC-004 "Bar clocks").

Pure functions of ``now`` (the injected clock's value) and the calendars in
``tradebot.data.calendar``; nothing here reads the wall clock.

* Equity bar D is complete at ``close(D) + EQUITY_CLOSE_DELAY`` (default 30 min, XNYS
  calendar, so early closes are handled); only calendar sessions are bars.
* Crypto bar D (UTC day) is complete at ``(D + 1 day) 00:00 UTC + 10 min``.
* A mixed portfolio follows the union clock (ADR D6): date D is due once every market
  that has a bar on D is complete.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

from tradebot.data.calendar import bar_close_time, is_session

CRYPTO_DELAY_MIN = 10

__all__ = [
    "CRYPTO_DELAY_MIN",
    "bar_complete_time",
    "due_dates",
    "has_bar",
    "market_of",
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
