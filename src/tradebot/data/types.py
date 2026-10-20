"""Public value types for the market data layer."""

from enum import StrEnum
from typing import NewType

Symbol = NewType("Symbol", str)  # canonical: "SPY", "BTC/USD"


class AssetClass(StrEnum):
    """Asset class of a tradable symbol. Single source of truth."""

    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"


class Timeframe(StrEnum):
    D1 = "1d"  # v1 only; H1 etc. added later


BAR_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
