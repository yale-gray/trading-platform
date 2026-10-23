"""Public value types for the market data layer."""

from enum import StrEnum
from typing import NewType

Symbol = NewType("Symbol", str)  # canonical: "SPY", "BTC/USD"


class AssetClass(StrEnum):
    """Asset class of a tradable symbol. Single source of truth."""

    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"


class Timeframe(StrEnum):
    D1 = "1d"
    M15 = "15m"  # US equity regular-session bars, labelled at bar open (ADR-0001 A21)


BAR_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
