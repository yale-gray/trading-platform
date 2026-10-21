"""Committed synthetic fixtures: FixtureProvider reads and manifest integrity (SPEC-001 AC 3, 14).

Reads go straight from the committed ``tests/fixtures/market`` directory. FixtureProvider
never writes, so no copy is needed. Hashes are compared with ``manifest.json``, not git.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tradebot.data.providers.fixture_provider import FixtureProvider

SIZE_BUDGET_BYTES = 2_500_000
ALL_TIME_START = date(2000, 1, 1)
ALL_TIME_END = date(2030, 1, 1)


def _load_manifest(market_root: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((market_root / "manifest.json").read_text("utf-8"))
    return manifest


def _assert_matches_manifest(market_root: Path) -> None:
    manifest = _load_manifest(market_root)
    for rel, meta in manifest["files"].items():
        digest = hashlib.sha256((market_root / rel).read_bytes()).hexdigest()
        assert digest == meta["sha256"], f"fixture file changed: {rel}"


@pytest.fixture(scope="session", autouse=True)
def _fixture_files_unchanged_at_session_end(fixtures_root: Path) -> Iterator[None]:
    """Every committed fixture file must still match manifest.json once the session ends."""
    yield
    _assert_matches_manifest(fixtures_root / "market")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symbol", "rows", "first"),
    [
        # Hand-checked: 2018-01-02..2024-12-31 inclusive has 2556 UTC days.
        ("BTC/USD", 2556, date(2018, 1, 2)),
        # Manifest: yfinance/SPY 1761 rows (XNYS sessions) from 2018-01-02.
        ("SPY", 1761, date(2018, 1, 2)),
        # Late listing: SOL/USD 1727 rows from 2020-04-10.
        ("SOL/USD", 1727, date(2020, 4, 10)),
    ],
)
def test_fixture_provider_serves_manifest_row_counts(
    fixtures_root: Path, symbol: str, rows: int, first: date
) -> None:
    provider = FixtureProvider(fixtures_root / "market")
    bars = provider.fetch_bars(symbol, ALL_TIME_START, ALL_TIME_END)
    assert len(bars) == rows
    assert bars.index[0].date() == first
    assert bars.index[-1].date() == date(2024, 12, 31)


@pytest.mark.integration
def test_fixture_provider_matches_manifest_for_every_bars_file(fixtures_root: Path) -> None:
    market = fixtures_root / "market"
    manifest = _load_manifest(market)
    provider = FixtureProvider(market)
    checked = 0
    for rel, meta in manifest["files"].items():
        if not rel.endswith("/gen-0001/bars.parquet"):
            continue
        _, fs_symbol, *_ = rel.split("/")
        symbol = fs_symbol.replace("_", "/")
        bars = provider.fetch_bars(symbol, ALL_TIME_START, ALL_TIME_END)
        assert len(bars) == meta["rows"], symbol
        assert bars.index[0].date().isoformat() == meta["first"], symbol
        assert bars.index[-1].date().isoformat() == meta["last"], symbol
        checked += 1
    assert checked == 20


@pytest.mark.unit
def test_fixture_provider_supports_only_symbols_with_a_cached_file(fixtures_root: Path) -> None:
    provider = FixtureProvider(fixtures_root / "market")
    assert provider.supports("SPY")
    assert provider.supports("ETH/USD")
    assert not provider.supports("AAPL")  # valid equity format, but not in the fixtures


@pytest.mark.unit
def test_manifest_hashes_match_committed_fixture_files(fixtures_root: Path) -> None:
    _assert_matches_manifest(fixtures_root / "market")


@pytest.mark.unit
def test_fixture_directory_is_within_size_budget(fixtures_root: Path) -> None:
    market = fixtures_root / "market"
    total = sum(p.stat().st_size for p in market.rglob("*") if p.is_file())
    assert total <= SIZE_BUDGET_BYTES
