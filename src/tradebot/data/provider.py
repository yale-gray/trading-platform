"""Protocol that every market data source implements."""

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

from tradebot.data.types import Symbol, Timeframe


@runtime_checkable
class DataProvider(Protocol):
    name: str  # cache source key

    def supports(self, symbol: Symbol) -> bool: ...

    def fetch_bars(
        self,
        symbol: Symbol,
        start: date,
        end: date,
        timeframe: Timeframe = Timeframe.D1,
    ) -> pd.DataFrame:
        """Closed bars with label date in [start, end], canonical schema (Section 1),
        already on the adjusted basis. No caching, no validation here."""
        ...
