"""The engine's ``asof`` type: a ``date`` for daily bars, a UTC ``datetime`` for intraday bars.

ADR-0001 A21 design note (the least invasive way to run 15-minute bars): ``datetime`` is a
subclass of ``date``, so every ``asof`` the engine, ledger and broker already carry may be a
timezone-aware UTC ``datetime`` (the bar *open* timestamp) instead of a ``date``. A daily
engine never sees a ``datetime`` and its serialised form is unchanged, so daily goldens and old
``EngineState`` checkpoints are untouched. One engine runs in one mode; mixing the two in one
engine is a bug and raises ``TypeError`` on the first comparison.

Helpers here turn either flavour into the things the engine needs: the exchange trading day
(settlement), a pandas ``Timestamp`` (indexing) and a totally ordered key (mixed
``date.min`` / ``datetime`` comparisons such as the initial-deposit stamp).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

__all__ = [
    "AsOf",
    "asof_from_iso",
    "asof_key",
    "asof_timestamp",
    "is_intraday",
    "trade_day",
]

AsOf = date | datetime
"""``date`` (daily bar label) or tz-aware UTC ``datetime`` (intraday bar open)."""

_NY = ZoneInfo("America/New_York")


def is_intraday(asof: date) -> bool:
    """True for a ``datetime`` asof (an intraday bar)."""
    return isinstance(asof, datetime)


def trade_day(asof: date) -> date:
    """Exchange trading day of ``asof``: itself for a ``date``, the New York date for a bar open."""
    if isinstance(asof, datetime):
        aware = asof if asof.tzinfo is not None else asof.replace(tzinfo=UTC)
        return aware.astimezone(_NY).date()
    return asof


def asof_key(asof: date) -> datetime:
    """Totally ordered UTC key (a ``date`` maps to its UTC midnight) for mixed comparisons."""
    if isinstance(asof, datetime):
        return asof if asof.tzinfo is not None else asof.replace(tzinfo=UTC)
    return datetime.combine(asof, time.min, tzinfo=UTC)


def asof_timestamp(asof: date) -> pd.Timestamp:
    """UTC ``pd.Timestamp`` of ``asof`` (a ``date`` is its UTC midnight)."""
    return pd.Timestamp(asof_key(asof))


def asof_from_iso(text: str) -> AsOf:
    """Inverse of ``asof.isoformat()`` for either flavour."""
    return datetime.fromisoformat(text) if "T" in text else date.fromisoformat(text)
