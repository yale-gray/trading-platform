"""Unit tests for devconsole/lib/sources.py: read-only market data loading (SPEC-006 AC 2-5)."""

from __future__ import annotations

import json
import os
import shutil
from datetime import date
from pathlib import Path

import pytest

from devconsole.lib.sources import (
    FIXTURES_DIR,
    FULL_END,
    FULL_START,
    SYNTHETIC_LABEL,
    DataSource,
    data_extent,
    load_bars,
    resolve_root,
    universe,
)
from tradebot.data.types import BAR_COLUMNS
from tradebot.data.universe import DEFAULT_UNIVERSE

pytestmark = pytest.mark.unit

_MANIFEST = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))


def _snapshot(root: Path) -> list[tuple[str, bool, int, int]]:
    """(relative path, is_dir, size, mtime_ns) for every entry under root, sorted."""
    rows: list[tuple[str, bool, int, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            full = Path(dirpath) / name
            st = full.stat()
            rows.append((str(full.relative_to(root)), full.is_dir(), st.st_size, st.st_mtime_ns))
    rows.append((".", True, root.stat().st_size, root.stat().st_mtime_ns))
    return sorted(rows)


# --------------------------------------------------------------------------- universe and label


def test_universe_lists_the_twenty_default_symbols() -> None:
    # 17 ETFs plus BTC/USD, ETH/USD, SOL/USD, per the universe module.
    symbols = universe()
    assert len(symbols) == 20
    assert symbols == list(DEFAULT_UNIVERSE)
    assert "BTC/USD" in symbols


def test_synthetic_label_text_is_exact() -> None:
    assert SYNTHETIC_LABEL == "synthetic practice data, not real prices"


# --------------------------------------------------------------------------- every symbol loads


@pytest.mark.parametrize("symbol", universe())
def test_every_universe_symbol_loads_from_fixtures(symbol: str) -> None:
    result = load_bars(DataSource.SYNTHETIC, symbol, FULL_START, FULL_END)

    assert result.message is None
    assert not result.bars.empty
    assert list(result.bars.columns) == list(BAR_COLUMNS)
    assert str(result.bars.index.tz) == "UTC"
    assert result.report is not None
    assert result.report.n_bars == len(result.bars)


def test_equity_fixture_row_count_matches_manifest() -> None:
    # manifest.json records 1761 rows for yfinance/SPY; the fixture has no bars the
    # validator would drop (all weekdays, closed, no duplicates).
    expected = _MANIFEST["files"]["yfinance/SPY/gen-0001/bars.parquet"]["rows"]
    result = load_bars(DataSource.SYNTHETIC, "SPY", FULL_START, FULL_END)
    assert len(result.bars) == expected == 1761


def test_synthetic_extent_matches_gen_json_first_and_last() -> None:
    # gen.json for SPY says first 2018-01-02, last 2024-12-31.
    assert data_extent(DataSource.SYNTHETIC, "SPY") == (date(2018, 1, 2), date(2024, 12, 31))


def test_resolve_root_synthetic_is_fixtures_dir_and_never_creates(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert resolve_root(DataSource.SYNTHETIC, fixtures_dir=missing) == missing
    assert not missing.exists()


def test_resolve_root_local_cache_uses_given_dir_without_creating(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    assert resolve_root(DataSource.LOCAL_CACHE, cache_dir=cache) == cache
    assert not cache.exists()


# --------------------------------------------------------------------------- bad symbols


@pytest.mark.parametrize(
    ("symbol", "fragment"),
    [
        ("nope!!", "Unrecognised symbol format"),
        ("BTC", "ambiguous"),
        ("", "Unrecognised symbol format"),
        ("SPY/USD", "Unsupported crypto base"),
        ("XYZ", "no fixture"),
    ],
)
def test_bad_symbol_gives_message_not_raise(symbol: str, fragment: str) -> None:
    result = load_bars(DataSource.SYNTHETIC, symbol, FULL_START, FULL_END)

    assert result.message is not None
    assert fragment in result.message
    assert result.bars.empty
    assert list(result.bars.columns) == list(BAR_COLUMNS)


def test_window_with_no_bars_gives_empty_state_message() -> None:
    result = load_bars(DataSource.SYNTHETIC, "SPY", date(1990, 1, 1), date(1991, 1, 1))

    assert result.bars.empty
    assert result.message == "No bars for SPY between 1990-01-01 and 1991-01-01."


def test_data_extent_none_for_unknown_symbol() -> None:
    assert data_extent(DataSource.SYNTHETIC, "XYZ") is None


# --------------------------------------------------------------------------- local cache


def test_local_cache_missing_dir_returns_message_and_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    before = sorted(p.name for p in tmp_path.iterdir())

    result = load_bars(DataSource.LOCAL_CACHE, "SPY", FULL_START, FULL_END, cache_dir=missing)

    assert result.bars.empty
    assert result.message is not None
    assert "No local cache yet" in result.message
    assert not missing.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_local_cache_populated_copy_returns_bars(market_cache: Path) -> None:
    result = load_bars(DataSource.LOCAL_CACHE, "SPY", FULL_START, FULL_END, cache_dir=market_cache)

    assert result.message is None
    assert len(result.bars) == 1761
    assert result.bars.index[0].date() == date(2018, 1, 2)
    assert result.bars.index[-1].date() == date(2024, 12, 31)


def test_local_cache_unknown_symbol_is_message_not_raise(market_cache: Path) -> None:
    result = load_bars(DataSource.LOCAL_CACHE, "XYZ", FULL_START, FULL_END, cache_dir=market_cache)

    assert result.bars.empty
    assert result.message is not None


def test_local_cache_load_leaves_tree_byte_identical(market_cache: Path) -> None:
    """AC 4/5: file list, sizes and mtimes unchanged by reads and quality checks."""
    before = _snapshot(market_cache)

    for symbol in universe():
        result = load_bars(
            DataSource.LOCAL_CACHE, symbol, FULL_START, FULL_END, cache_dir=market_cache
        )
        assert result.message is None, symbol

    assert _snapshot(market_cache) == before


def test_synthetic_load_leaves_fixture_tree_unchanged(tmp_path: Path) -> None:
    """AC 5 on a temp copy of the fixture tree, read through the synthetic source."""
    copy = tmp_path / "fixture_copy"
    shutil.copytree(FIXTURES_DIR, copy)
    before = _snapshot(copy)

    for symbol in universe():
        result = load_bars(DataSource.SYNTHETIC, symbol, FULL_START, FULL_END, fixtures_dir=copy)
        assert result.message is None, symbol

    assert _snapshot(copy) == before


def test_committed_fixture_tree_is_not_modified_by_loading() -> None:
    """Loading from the committed fixtures must not touch them (read-only source)."""
    before = _snapshot(FIXTURES_DIR)

    for symbol in ("SPY", "BTC/USD"):
        assert load_bars(DataSource.SYNTHETIC, symbol, FULL_START, FULL_END).message is None

    assert _snapshot(FIXTURES_DIR) == before
