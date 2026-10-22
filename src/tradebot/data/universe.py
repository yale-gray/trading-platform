"""Symbol universe for the market data layer (SPEC-001 Section 6).

v1 uses a fixed list (``StaticUniverse``). The ``Universe`` protocol is the hook
for a later point-in-time membership table.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date
from pathlib import Path
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


UNIVERSE_DIR = Path(__file__).parent / "universes"

SP500_LABEL = "current S&P 500 constituents (survivorship-biased)"
"""Fixed caveat shown wherever S&P 500 stock results are displayed (ADR-0001 A13)."""

SP500_SPEC_PREFIX = "stocks_"


def is_sp500_spec(spec_id: str) -> bool:
    """True when a strategy runs on the S&P 500 snapshot.

    Explicit rule: the strategy id starts with ``stocks_`` (``stocks_momentum``,
    ``stocks_momentum_cash``). Universe size is never used to guess.
    """
    return spec_id.startswith(SP500_SPEC_PREFIX)


class DatedUniverse:
    """Universe with a per-symbol ``date_added`` (membership starts on that date).

    SURVIVORSHIP CAVEAT (ADR-0001 A13): this holds *current constituents only*
    (survivorship-biased). Names removed from the index are absent, and no historical
    removal dates are available from the free source. Honouring ``date_added`` only
    stops a name being used before it joined the index, which removes part, not all,
    of the bias. Label every result "current constituents (survivorship-biased)".
    """

    def __init__(self, added: dict[Symbol, date | None]) -> None:
        self._added = dict(added)

    def members(self, as_of: date) -> list[Symbol]:
        return [s for s, d in self._added.items() if d is None or d <= as_of]

    def symbols(self) -> list[Symbol]:
        """All symbols regardless of date."""
        return list(self._added)


def load_sp500(path: Path | None = None) -> DatedUniverse:
    """Load the committed S&P 500 snapshot (``universes/sp500.csv``)."""
    added: dict[Symbol, date | None] = {}
    with (path or UNIVERSE_DIR / "sp500.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("date_added") or "").strip()
            try:
                d: date | None = date.fromisoformat(raw[:10]) if raw else None
            except ValueError:
                d = None
            added[normalize_symbol(row["ticker"])] = d
    return DatedUniverse(added)


SP500_UNIVERSE: tuple[Symbol, ...] = tuple(load_sp500().symbols())
