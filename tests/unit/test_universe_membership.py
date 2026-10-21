"""Unit tests: static universe membership and survivorship by data availability.

SPEC-005 T8 / SPEC-001 Section 6. ``StaticUniverse`` is always-true in v1 (PM decision),
so these tests check only listed-vs-unlisted membership and make no date-filtering claim.
Survivorship is checked against the committed edge fixtures instead: ZZNEW lists on
2021-03-01 and ZZGONE's last bar is 2022-06-30, so neither has data outside its life.
"""

from __future__ import annotations

from datetime import UTC, date
from pathlib import Path

import pandas as pd
import pytest

from tradebot.clock import FixedClock
from tradebot.data.cache import BarCache
from tradebot.data.providers.fixture_provider import FixtureProvider
from tradebot.data.universe import DEFAULT_UNIVERSE, StaticUniverse

pytestmark = pytest.mark.unit

EQUITY_SOURCE = "yfinance"


# --- StaticUniverse membership ----------------------------------------------


@pytest.mark.parametrize("symbol", DEFAULT_UNIVERSE)
def test_listed_symbol_is_member(symbol: str) -> None:
    universe = StaticUniverse(DEFAULT_UNIVERSE)
    assert universe.is_member(symbol, date(2024, 12, 31)) is True


@pytest.mark.parametrize("symbol", ["AAPL", "ZZNEW", "XLRE"])
def test_unlisted_symbol_is_not_member(symbol: str) -> None:
    universe = StaticUniverse(DEFAULT_UNIVERSE)
    assert universe.is_member(symbol, date(2024, 12, 31)) is False


def test_membership_normalises_case_and_crypto_form() -> None:
    # "btc-usd" and "eth/usd" normalise to BTC/USD and ETH/USD, so they match the listing.
    universe = StaticUniverse(["SPY", "BTC/USD"])
    assert universe.is_member("spy", date(2024, 1, 2)) is True
    assert universe.is_member("btc-usd", date(2024, 1, 2)) is True
    assert universe.is_member("ETH/USD", date(2024, 1, 2)) is False


@pytest.mark.parametrize("symbol", ["ZZGONE", "BRK.B", "", "DOGE/USD", "BTC"])
def test_symbol_failing_normalisation_is_not_member_and_does_not_raise(symbol: str) -> None:
    universe = StaticUniverse(DEFAULT_UNIVERSE)
    assert universe.is_member(symbol, date(2020, 1, 2)) is False


def test_members_returns_the_listed_symbols_in_order() -> None:
    universe = StaticUniverse(DEFAULT_UNIVERSE)
    assert universe.members(date(2024, 12, 31)) == list(DEFAULT_UNIVERSE)
    assert len(DEFAULT_UNIVERSE) == 20  # 17 ETFs plus BTC, ETH, SOL vs USD (STRATEGY.md s.3)


# --- survivorship: data-availability helper ---------------------------------


def _bar_dates(root: Path, symbol: str) -> pd.DatetimeIndex:
    """UTC-midnight bar labels for an edge fixture, read directly from parquet."""
    fs_symbol = symbol.replace("/", "_")
    df = pd.read_parquet(root / EQUITY_SOURCE / fs_symbol / "gen-0001" / "bars.parquet")
    return df.index.tz_convert(UTC).normalize()


def _available(bar_dates: pd.DatetimeIndex, asof: date) -> bool:
    """A symbol is available at ``asof`` iff it has a bar on or before ``asof``, and
    ``asof`` is not after its last bar (no bars outside its life, e.g. ZZGONE)."""
    stamp = pd.Timestamp(asof, tz=UTC)
    has_bar_on_or_before = bool((bar_dates <= stamp).any())
    not_after_last_bar = stamp <= bar_dates.max()
    return has_bar_on_or_before and not_after_last_bar


def test_znew_first_bar_is_2021_03_01(edge_cache: Path) -> None:
    dates = _bar_dates(edge_cache, "ZZNEW")
    assert dates.min() == pd.Timestamp("2021-03-01", tz=UTC)


def test_zzgone_last_bar_is_2022_06_30(edge_cache: Path) -> None:
    dates = _bar_dates(edge_cache, "ZZGONE")
    assert dates.max() == pd.Timestamp("2022-06-30", tz=UTC)


@pytest.mark.parametrize("asof", [date(2018, 1, 2), date(2021, 2, 26), date(2021, 2, 27)])
def test_znew_unavailable_before_listing(edge_cache: Path, asof: date) -> None:
    assert _available(_bar_dates(edge_cache, "ZZNEW"), asof) is False


@pytest.mark.parametrize("asof", [date(2021, 3, 1), date(2021, 3, 2), date(2024, 12, 31)])
def test_znew_available_from_listing_onward(edge_cache: Path, asof: date) -> None:
    assert _available(_bar_dates(edge_cache, "ZZNEW"), asof) is True


@pytest.mark.parametrize("asof", [date(2018, 1, 1), date(2022, 7, 1), date(2024, 12, 31)])
def test_zzgone_unavailable_outside_its_life(edge_cache: Path, asof: date) -> None:
    assert _available(_bar_dates(edge_cache, "ZZGONE"), asof) is False


@pytest.mark.parametrize("asof", [date(2018, 1, 2), date(2020, 6, 15), date(2022, 6, 30)])
def test_zzgone_available_during_its_life(edge_cache: Path, asof: date) -> None:
    assert _available(_bar_dates(edge_cache, "ZZGONE"), asof) is True


def test_no_bars_served_outside_znew_and_zzgone_life(
    edge_cache: Path, fixed_clock: FixedClock
) -> None:
    # The helper's verdict matches what the cache actually serves.
    cache = BarCache(edge_cache, fixed_clock)
    before_listing = cache.read(EQUITY_SOURCE, "ZZNEW", end=pd.Timestamp("2021-02-28", tz=UTC))
    after_delisting = cache.read(EQUITY_SOURCE, "ZZGONE", start=pd.Timestamp("2022-07-01", tz=UTC))
    assert before_listing.empty
    assert after_delisting.empty


def test_fixture_provider_serves_znew_only_from_listing(edge_cache: Path) -> None:
    # ZZNEW is 5 letters, so FixtureProvider accepts it.
    provider = FixtureProvider(edge_cache)
    df = provider.fetch_bars("ZZNEW", date(2018, 1, 2), date(2024, 12, 31))
    assert df.index.min() == pd.Timestamp("2021-03-01", tz=UTC)


def test_production_reads_agree_with_the_availability_helper(
    edge_cache: Path, fixed_clock: FixedClock
) -> None:
    # Production path (BarCache + FixtureProvider), not the test-local helper: the data
    # served at the edges of each symbol's life matches the helper's verdict, and the
    # universe never lists the delisted name (it cannot even be normalised).
    cache = BarCache(edge_cache, fixed_clock)
    znew = cache.read(EQUITY_SOURCE, "ZZNEW")
    zzgone = cache.read(EQUITY_SOURCE, "ZZGONE")
    assert znew.index[0] == pd.Timestamp("2021-03-01", tz=UTC)
    assert zzgone.index[-1] == pd.Timestamp("2022-06-30", tz=UTC)
    provider = FixtureProvider(edge_cache)
    served = provider.fetch_bars("ZZNEW", date(2018, 1, 2), date(2021, 2, 28))
    assert served.empty
    assert StaticUniverse(DEFAULT_UNIVERSE).is_member("ZZGONE", date(2020, 1, 2)) is False
