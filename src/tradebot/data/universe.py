"""Symbol universe for the market data layer (SPEC-001 Section 6).

v1 uses a fixed list (``StaticUniverse``). The ``Universe`` protocol is the hook
for a later point-in-time membership table.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from tradebot.data.errors import DataError, UnknownSymbolError
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


# --------------------------------------------------------------------------- point-in-time S&P 500
# ADR-0001 A32. ``universes/sp500_history.csv`` has one row per (ticker, membership interval);
# ``universes/sp500_aliases.csv`` maps tickers as the source lists them to the ticker that has
# the price history (FB -> META). Membership on a day is the membership in force at the close of
# the PREVIOUS trading day, so a change dated d is first visible on the session after d.

SP500_HISTORY_FILE = "sp500_history.csv"
SP500_ALIAS_FILE = "sp500_aliases.csv"

_Interval = tuple[Symbol, date, date | None]  # ticker, first day in force, first day not (excl.)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


@lru_cache(maxsize=4)
def _sp500_alias_rows(path: Path | None = None) -> tuple[tuple[str, Symbol, date | None], ...]:
    """(source ticker, price ticker, until) rows. ``until`` (exclusive, optional) limits the alias
    to membership before that date: IR is Trane (TT) until 2020-03-02, then Ingersoll Rand."""
    rows = _csv_rows(path or UNIVERSE_DIR / SP500_ALIAS_FILE)
    return tuple(
        (
            r["member_ticker"].strip().upper(),
            normalize_symbol(r["price_ticker"]),
            date.fromisoformat(r["until"].strip()) if (r.get("until") or "").strip() else None,
        )
        for r in rows
    )


def _sp500_aliases() -> dict[str, Symbol]:
    """Source ticker -> price ticker (any date range)."""
    return {raw: price for raw, price, _ in _sp500_alias_rows()}


@lru_cache(maxsize=4)
def _sp500_intervals(path: Path | None = None) -> tuple[_Interval, ...]:
    aliases = {raw: (price, until) for raw, price, until in _sp500_alias_rows()}
    out: list[_Interval] = []
    for r in _csv_rows(path or UNIVERSE_DIR / SP500_HISTORY_FILE):
        raw = r["ticker"].strip().upper()
        start = date.fromisoformat(r["start"])
        end = date.fromisoformat(r["end"]) if r["end"].strip() else None
        price, until = aliases.get(raw, (None, None))
        if price is None:
            out.append((normalize_symbol(raw), start, end))
        elif until is None or start < until:
            cut = until if until is not None and (end is None or end > until) else end
            out.append((price, start, cut))
            if until is not None and (end is None or end > until):
                out.append((normalize_symbol(raw), until, end))
        else:
            out.append((normalize_symbol(raw), start, end))
    return tuple(out)


def sp500_history_start() -> date:
    """First date of the committed membership history (earliest interval start)."""
    return min(s for _, s, _ in _sp500_intervals())


def _prev_session(d: date) -> date:
    """The last XNYS session strictly before ``d``."""
    from tradebot.data.calendar import sessions

    prior = sessions("SPY", d - timedelta(days=14), d - timedelta(days=1))
    if len(prior) == 0:
        raise DataError(f"no trading day found before {d}")
    return prior[-1].date()


def sp500_members(asof: date) -> frozenset[Symbol]:
    """S&P 500 members for trading on ``asof``: the list in force at the previous session's close.

    Symbols are canonical price tickers (aliases applied). Raises ``DataError`` before the
    history starts (the previous close must be on or after ``sp500_history_start()``).
    """
    prev = _prev_session(asof)
    if prev < sp500_history_start():
        raise DataError(f"S&P 500 membership history starts {sp500_history_start()}; asked {asof}")
    return frozenset(t for t, s, e in _sp500_intervals() if s <= prev and (e is None or prev < e))


def sp500_ever(start: date, end: date) -> frozenset[Symbol]:
    """Every symbol that is a member (as of sp500_members) on any day in ``[start, end]``."""
    if end < start:
        return frozenset()
    lo, hi = _prev_session(start), _prev_session(end)
    return frozenset(t for t, s, e in _sp500_intervals() if s <= hi and (e is None or e > lo))


def sp500_member_flags(symbol: str, days: Sequence[date]) -> list[float]:
    """1.0 where ``symbol`` is an S&P 500 member on each day (as ``sp500_members``), else 0.0.

    Days before the history starts are 0.0 (never a member). Vectorised for the engine.
    """
    target = normalize_symbol(symbol)
    spans = [(s, e) for t, s, e in _sp500_intervals() if t == target]
    if not spans or not days:
        return [0.0] * len(days)
    from tradebot.data.calendar import sessions

    lo, hi = min(days), max(days)
    sess = [x.date() for x in sessions("SPY", lo - timedelta(days=14), hi)]
    out: list[float] = []
    k = 0
    for d in days:
        while k < len(sess) and sess[k] < d:
            k += 1
        prev = sess[k - 1] if k > 0 else None
        out.append(
            1.0
            if prev is not None and any(s <= prev and (e is None or prev < e) for s, e in spans)
            else 0.0
        )
    return out
