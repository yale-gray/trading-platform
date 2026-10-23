"""Committed event calendars (public data, refreshed by hand; ADR-0001 A29)."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

__all__ = ["fomc_dates"]

_DIR = Path(__file__).parent


@lru_cache(maxsize=1)
def fomc_dates() -> tuple[date, ...]:
    """Scheduled FOMC statement days 2015-2027, ascending (``fomc.csv``).

    Unscheduled meetings and notation votes are excluded: they cannot be known in advance.
    """
    df = pd.read_csv(_DIR / "fomc.csv", parse_dates=["date"])
    return tuple(d.date() for d in df["date"].sort_values())
