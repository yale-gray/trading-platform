"""Read-only market data sources for the dev console (no streamlit import).

Synthetic practice data is read straight from the committed fixtures through
``FixtureProvider``. The optional local cache is read through
``MarketData(offline=True)`` with ``refresh=False``. Nothing here writes, creates
directories, or touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

import pandas as pd

from tradebot.clock import SystemClock
from tradebot.data.config import DataConfig
from tradebot.data.errors import DataError
from tradebot.data.providers.fixture_provider import FixtureProvider
from tradebot.data.quality import QualityReport, validate_bars
from tradebot.data.service import MarketData
from tradebot.data.symbols import normalize_symbol
from tradebot.data.types import BAR_COLUMNS
from tradebot.data.universe import DEFAULT_UNIVERSE


class DataSource(StrEnum):
    """Where bars come from."""

    SYNTHETIC = "synthetic"
    LOCAL_CACHE = "local_cache"


SYNTHETIC_LABEL = "synthetic practice data, not real prices"
FIXTURES_DIR: Path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "market"
FULL_START, FULL_END = date(2000, 1, 3), date(2035, 12, 31)

# Fixed "now" for validating synthetic data (no wall-clock reads for fixtures).
_SYNTHETIC_NOW = datetime(2035, 12, 28, 23, tzinfo=UTC)


@dataclass(frozen=True)
class BarsResult:
    """Bars for one symbol plus how they were validated."""

    source: DataSource
    root: Path
    symbol: str
    bars: pd.DataFrame  # BAR_COLUMNS, UTC DatetimeIndex; empty if none
    report: QualityReport | None  # None only when there is nothing to validate
    message: str | None  # human-readable problem/empty-state, else None


def _empty_bars() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS}, index=index)


def resolve_root(
    source: DataSource, *, fixtures_dir: Path = FIXTURES_DIR, cache_dir: Path | None = None
) -> Path:
    """Directory the source reads from. Pure: never creates anything."""
    if source is DataSource.SYNTHETIC:
        return Path(fixtures_dir)
    return Path(cache_dir) if cache_dir is not None else DataConfig().cache_dir


def universe() -> list[str]:
    """The default symbol universe (20 symbols)."""
    return list(DEFAULT_UNIVERSE)


def _fetch(source: DataSource, root: Path, symbol: str, start: date, end: date) -> pd.DataFrame:
    """Raw bars in ``[start, end]``; raises ``DataError`` subclasses."""
    if source is DataSource.SYNTHETIC:
        return FixtureProvider(root).fetch_bars(symbol, start, end)
    md = MarketData(DataConfig(cache_dir=root, offline=True))
    return md.get_bars(symbol, start, end, refresh=False)


def load_bars(
    source: DataSource,
    symbol: str,
    start: date,
    end: date,
    *,
    fixtures_dir: Path = FIXTURES_DIR,
    cache_dir: Path | None = None,
) -> BarsResult:
    """Load and validate bars read-only. Problems come back as ``message``, never raise."""
    root = resolve_root(source, fixtures_dir=fixtures_dir, cache_dir=cache_dir)

    def fail(sym: str, message: str) -> BarsResult:
        return BarsResult(source, root, sym, _empty_bars(), None, message)

    try:
        canon = str(normalize_symbol(symbol))
    except DataError as exc:
        return fail(symbol, str(exc))
    if source is DataSource.LOCAL_CACHE and not root.is_dir():
        return fail(canon, f"No local cache yet at {root}; run a data refresh later.")
    try:
        raw = _fetch(source, root, canon, start, end)
        if raw.empty:
            return fail(canon, f"No bars for {canon} between {start} and {end}.")
        now = _SYNTHETIC_NOW if source is DataSource.SYNTHETIC else SystemClock().now()
        bars, report = validate_bars(raw, canon, now)
    except DataError as exc:
        return fail(canon, str(exc))
    if bars.empty:
        return BarsResult(source, root, canon, bars, report, f"No closed bars for {canon}.")
    return BarsResult(source, root, canon, bars, report, None)


def data_extent(source: DataSource, symbol: str, **kw: object) -> tuple[date, date] | None:
    """First and last bar dates available for ``symbol``, or None if there are none."""
    result = load_bars(source, symbol, FULL_START, FULL_END, **kw)  # type: ignore[arg-type]
    if result.bars.empty:
        return None
    return result.bars.index[0].date(), result.bars.index[-1].date()
