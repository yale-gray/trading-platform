"""Offline provider that serves the committed synthetic fixtures (SPEC-001 Section 3).

Reads go through :class:`BarCache`, so the real cache layout and sha256 check
are exercised. This provider never touches the network and never writes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from tradebot.clock import FixedClock
from tradebot.data.cache import BarCache
from tradebot.data.errors import DataError, UnknownSymbolError
from tradebot.data.symbols import asset_class, normalize_symbol
from tradebot.data.types import AssetClass, Symbol, Timeframe

# Cache source keys, as used by the production adapters.
EQUITY_SOURCE = "yfinance"
CRYPTO_SOURCE = "coinbaseexchange"

# BarCache takes a clock, but this provider only reads, so the value is never used.
_UNUSED_CLOCK = FixedClock(datetime(1970, 1, 1, tzinfo=UTC))


class FixtureProvider:
    """Daily bars read from a cache-layout directory such as ``tests/fixtures/market``."""

    name = "fixture"

    def __init__(self, root: Path | str) -> None:
        self._cache = BarCache(root, _UNUSED_CLOCK)

    def _source_for(self, symbol: Symbol) -> str:
        if asset_class(symbol) is AssetClass.CRYPTO:
            return CRYPTO_SOURCE
        return EQUITY_SOURCE

    def supports(self, symbol: Symbol) -> bool:
        try:
            canonical = normalize_symbol(symbol)
        except UnknownSymbolError:
            return False
        return self._cache.exists(self._source_for(canonical), canonical)

    def fetch_bars(
        self,
        symbol: Symbol,
        start: date,
        end: date,
        timeframe: Timeframe = Timeframe.D1,
    ) -> pd.DataFrame:
        """Return fixture bars with label date in [start, end], inclusive."""
        if timeframe is not Timeframe.D1:
            raise DataError(f"FixtureProvider only serves {Timeframe.D1.value} bars")
        canonical = normalize_symbol(symbol)
        source = self._source_for(canonical)
        if not self._cache.exists(source, canonical):
            raise UnknownSymbolError(f"no fixture for {canonical!r} under {source!r}")
        lo = pd.Timestamp(start).tz_localize("UTC")
        hi = pd.Timestamp(end).tz_localize("UTC")
        return self._cache.read(source, canonical, lo, hi)
