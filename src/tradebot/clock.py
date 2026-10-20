"""Injectable clock.

This is the only module under ``src/tradebot/`` allowed to call ``datetime.now(``.
Code under test takes a ``Clock`` and never reads the wall clock directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of the current instant. Always tz-aware, UTC."""

    def now(self) -> datetime: ...

    def __call__(self) -> datetime: ...


def _require_aware(when: datetime) -> datetime:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("clock values must be timezone-aware")
    return when.astimezone(UTC)


class SystemClock:
    """Real wall clock, UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now()


class FixedClock:
    """Clock that only moves when told to. Used by tests."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start)

    def now(self) -> datetime:
        return self._now

    def __call__(self) -> datetime:
        return self.now()

    def set(self, when: datetime) -> None:
        new = _require_aware(when)
        if new < self._now:
            raise ValueError("FixedClock cannot move backwards")
        self._now = new

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("FixedClock.advance requires a non-negative delta")
        self._now = self._now + delta
