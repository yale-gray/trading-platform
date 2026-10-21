"""Synthetic fixture bars for backtest tests, read through the real fixture provider.

Read-only: nothing is written to the repo, the cache or the network. Bars are fetched from
2018-01-02 so the strategies have warmup history before the run start (2019-01-02).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from tradebot.data.providers.fixture_provider import FixtureProvider

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "market"
FETCH_START = date(2018, 1, 2)
FETCH_END = date(2024, 12, 31)
RUN_START = date(2019, 1, 2)
RUN_END = date(2024, 12, 31)


_FRAMES: dict[str, pd.DataFrame] = {}


def load_fixture_bars(symbols: set[str]) -> dict[str, pd.DataFrame]:
    """Bars for each symbol over the fetch window, keyed by canonical symbol.

    Frames are read once per process and shared between callers (treat them as read-only);
    the returned dict is fresh each call.
    """
    provider = FixtureProvider(FIXTURES_ROOT)
    for s in sorted(symbols):
        if s not in _FRAMES:
            _FRAMES[s] = provider.fetch_bars(s, FETCH_START, FETCH_END)
    return {s: _FRAMES[s] for s in sorted(symbols)}
