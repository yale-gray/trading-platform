"""Symbol universe for the market data layer (SPEC-001 Section 6).

v1 uses a fixed list (``StaticUniverse``). The ``Universe`` protocol is the hook
for a later point-in-time membership table.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from tradebot.data.errors import UnknownSymbolError
from tradebot.data.symbols import normalize_symbol
from tradebot.data.types import Symbol

# The 20 symbols of docs/STRATEGY.md section 3: 17 ETFs plus BTC, ETH and SOL vs USD.
DEFAULT_UNIVERSE: tuple[Symbol, ...] = (
    Symbol("SPY"),
    Symbol("QQQ"),
    Symbol("IWM"),
    Symbol("EFA"),
    Symbol("EEM"),
    Symbol("TLT"),
    Symbol("IEF"),
    Symbol("GLD"),
    Symbol("XLK"),
    Symbol("XLF"),
    Symbol("XLE"),
    Symbol("XLV"),
    Symbol("XLI"),
    Symbol("XLY"),
    Symbol("XLP"),
    Symbol("XLU"),
    Symbol("XLB"),
    Symbol("BTC/USD"),
    Symbol("ETH/USD"),
    Symbol("SOL/USD"),
)


class Universe(Protocol):
    def members(self, as_of: date) -> list[Symbol]: ...


class StaticUniverse:
    """Returns the same symbols for every date. Membership is always true in v1."""

    def __init__(self, symbols: Sequence[str]) -> None:
        self._symbols: tuple[Symbol, ...] = tuple(normalize_symbol(s) for s in symbols)

    def members(self, as_of: date) -> list[Symbol]:
        return list(self._symbols)

    def is_member(self, symbol: str, asof: date) -> bool:
        """True if ``symbol`` is listed. Names that fail normalisation are not members."""
        try:
            return normalize_symbol(symbol) in self._symbols
        except UnknownSymbolError:
            return False
