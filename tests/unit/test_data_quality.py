"""Unit tests: validators over every market_edge fixture, provider end-bounds, bar immutability.

Scope (SPEC-005 T8, SPEC-001 Section 5). The broad per-rule tests live in
``tests/data/test_quality.py``; this module checks each committed edge fixture
against its stated defect (SPEC-005 section 2), asserting on the error message or
report field, and checks that ``FixtureProvider`` never serves bars after ``end``
and that ``BarCache`` refuses to rewrite history.

Calendar facts used: 2019-07-04 is an NYSE holiday (Thursday); 2024-07-04 is a
Thursday holiday; 2024-07-06 is a Saturday; 2021-02-27 is a Saturday.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tradebot.clock import FixedClock
from tradebot.data.cache import BarCache
from tradebot.data.calendar import sessions
from tradebot.data.errors import DataQualityError, ImmutabilityError
from tradebot.data.providers.fixture_provider import FixtureProvider
from tradebot.data.quality import validate_bars

pytestmark = pytest.mark.unit

# Well after every fixture bar, so no bar is in progress.
NOW = datetime(2025, 1, 6, 12, 0, tzinfo=UTC)
EQUITY_SOURCE = "yfinance"
CRYPTO_SOURCE = "coinbaseexchange"


def _load(root: Path, source: str, symbol: str) -> pd.DataFrame:
    fs_symbol = symbol.replace("/", "_")
    return pd.read_parquet(root / source / fs_symbol / "gen-0001" / "bars.parquet")


def _edge(edge_cache: Path, symbol: str) -> pd.DataFrame:
    return _load(edge_cache, EQUITY_SOURCE, symbol)


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- every edge fixture through validate_bars -----------------------------

# Expected outcome per SPEC-005 section 2. A string means validate_bars must raise
# DataQualityError whose message contains it; None means it must return cleanly.
EDGE_OUTCOMES: dict[str, str | None] = {
    "ZZGAP": "6 interior gap(s)",
    "ZZDUP": None,  # flagged via report.duplicates_dropped, not an error
    "ZZTZ": "must be UTC",
    "ZZBAD": "non-positive or NaN price",  # plus an OHLC violation; see detail test
    "ZZFLAT": None,
    "ZZUP": None,
    "ZZNEW": None,
    "ZZGONE": None,
}


@pytest.mark.parametrize("symbol", sorted(EDGE_OUTCOMES))
def test_every_edge_fixture_has_its_stated_outcome(edge_cache: Path, symbol: str) -> None:
    df = _edge(edge_cache, symbol)
    expected = EDGE_OUTCOMES[symbol]
    if expected is None:
        clean, report = validate_bars(df, symbol, NOW)
        assert report.gaps == []
        assert report.outliers == []
        assert len(clean) == len(df) - report.duplicates_dropped
    else:
        with pytest.raises(DataQualityError, match=re.escape(expected)):
            validate_bars(df, symbol, NOW)


def test_zzgap_missing_sessions_are_exactly_the_six_stated(edge_cache: Path) -> None:
    # Expected: 503 XNYS sessions 2018-01-02..2019-12-31 minus the 497 present = 6 missing.
    # Missing: 2019-07-08..2019-07-12 (Mon-Fri) and 2019-07-15 (Mon).
    df = _edge(edge_cache, "ZZGAP")
    expected = sessions("ZZGAP", date(2018, 1, 2), date(2019, 12, 31))
    present = df.index.tz_convert(UTC).normalize()
    missing = expected.difference(present)
    assert len(expected) == 503
    assert [ts.date().isoformat() for ts in missing] == [
        "2019-07-08",
        "2019-07-09",
        "2019-07-10",
        "2019-07-11",
        "2019-07-12",
        "2019-07-15",
    ]
    with pytest.raises(DataQualityError) as excinfo:
        validate_bars(df, "ZZGAP", NOW)
    # The message lists the first five and counts the rest: 2019-07-15 is the "+1 more".
    assert "2019-07-08, 2019-07-09, 2019-07-10, 2019-07-11, 2019-07-12 (+1 more)" in str(
        excinfo.value
    )


def test_zzdup_duplicate_is_flagged_and_last_row_kept(edge_cache: Path) -> None:
    # 2019-03-12 appears twice; the second row is the original close x 1.01 (SPEC-005 s.2).
    # Hand check: 504 rows - 1 duplicate = 503 bars; 503 sessions in the range.
    df = _edge(edge_cache, "ZZDUP")
    dup_rows = df.loc[pd.Timestamp("2019-03-12", tz="UTC")]
    assert len(dup_rows) == 2
    first_close, second_close = dup_rows["close"].iloc[0], dup_rows["close"].iloc[1]
    assert second_close == pytest.approx(first_close * 1.01, rel=1e-4)

    clean, report = validate_bars(df, "ZZDUP", NOW)
    assert report.duplicates_dropped == 1
    assert report.warnings == ["dropped 1 duplicate timestamp(s)"]
    assert clean.index.is_unique
    assert len(clean) == 503
    assert clean.loc[pd.Timestamp("2019-03-12", tz="UTC"), "close"] == pytest.approx(second_close)


def test_zztz_non_utc_index_is_rejected_with_zone_named(edge_cache: Path) -> None:
    df = _edge(edge_cache, "ZZTZ")
    assert str(df.index.tz) == "America/New_York"
    with pytest.raises(DataQualityError, match=r"ZZTZ: index must be UTC, got America/New_York"):
        validate_bars(df, "ZZTZ", NOW)


def test_zzbad_reports_both_stated_defects(edge_cache: Path) -> None:
    # Two defects in one fixture: 2019-05-15 has high < low (OHLC violation) and
    # 2019-05-16 has close = 0.0 (non-positive price). validate_bars reports both.
    df = _edge(edge_cache, "ZZBAD")
    bad_day = df.loc[pd.Timestamp("2019-05-15", tz="UTC")]
    assert bad_day["high"] < bad_day["low"]
    zero_day = df.loc[pd.Timestamp("2019-05-16", tz="UTC")]
    assert zero_day["close"] == 0.0

    with pytest.raises(DataQualityError) as excinfo:
        validate_bars(df, "ZZBAD", NOW)
    message = str(excinfo.value)
    assert "1 row(s) with non-positive or NaN price: 2019-05-16" in message
    assert "1 row(s) violating low<=min(open,close) or high>=max(open,close): 2019-05-15" in (
        message
    )


@pytest.mark.parametrize(
    "symbol",
    ["ZZFLAT", "ZZUP", "ZZNEW", "ZZGONE"],
)
def test_clean_edge_fixture_passes_with_no_warnings(edge_cache: Path, symbol: str) -> None:
    df = _edge(edge_cache, symbol)
    clean, report = validate_bars(df, symbol, NOW)
    assert len(clean) == len(df)
    assert report.duplicates_dropped == 0
    assert report.gaps == []
    assert report.outliers == []
    assert report.warnings == []


def test_zzflat_closes_are_constant_100(edge_cache: Path) -> None:
    # Hand check: every bar is open = high = low = close = 100.0 (SPEC-005 s.2).
    clean, _ = validate_bars(_edge(edge_cache, "ZZFLAT"), "ZZFLAT", NOW)
    assert (clean["close"] == 100.0).all()
    assert (clean["high"] == clean["low"]).all()


def test_zzup_closes_step_up_by_half_a_point(edge_cache: Path) -> None:
    # Hand check: close_t = 100 + 0.5 t, so each step is +0.5 and the first close is 100.0.
    clean, _ = validate_bars(_edge(edge_cache, "ZZUP"), "ZZUP", NOW)
    assert clean["close"].iloc[0] == pytest.approx(100.0)
    assert clean["close"].diff().dropna().to_numpy() == pytest.approx(0.5)


# --- FixtureProvider never returns bars after end ---------------------------

END_CASES: list[tuple[str, str, date]] = [
    (EQUITY_SOURCE, "SPY", date(2019, 3, 12)),  # ordinary session
    (EQUITY_SOURCE, "SPY", date(2019, 7, 4)),  # NYSE holiday (Thursday)
    (EQUITY_SOURCE, "SPY", date(2024, 7, 6)),  # Saturday
    (EQUITY_SOURCE, "SPY", date(2024, 12, 31)),  # last cached session
    (CRYPTO_SOURCE, "BTC/USD", date(2021, 2, 27)),  # Saturday, crypto trades daily
    (CRYPTO_SOURCE, "BTC/USD", date(2022, 6, 30)),
    (CRYPTO_SOURCE, "BTC/USD", date(2024, 12, 31)),
]


@pytest.mark.parametrize(("source", "symbol", "end"), END_CASES)
def test_fixture_provider_never_returns_bars_after_end(
    market_cache: Path, source: str, symbol: str, end: date
) -> None:
    provider = FixtureProvider(market_cache)
    df = provider.fetch_bars(symbol, date(2018, 1, 2), end)

    end_ts = pd.Timestamp(end, tz="UTC")
    assert len(df) > 0
    assert df.index.max() <= end_ts
    # The result is exactly the cached bars up to and including end, nothing dropped.
    raw = _load(market_cache, source, symbol)
    expected = raw.loc[raw.index <= end_ts]
    assert df.index.max() == expected.index.max()
    assert len(df) == len(expected)


def test_fixture_provider_window_before_first_bar_is_empty(edge_cache: Path) -> None:
    # ZZNEW first bar is 2021-03-01, so a window ending 2021-02-26 has nothing to serve.
    provider = FixtureProvider(edge_cache)
    df = provider.fetch_bars("ZZNEW", date(2018, 1, 2), date(2021, 2, 26))
    assert df.empty


# --- bar immutability -------------------------------------------------------


@pytest.fixture
def writable_cache(market_cache: Path, fixed_clock: FixedClock) -> BarCache:
    # market_cache is already a writable tmp_path copy of the committed fixtures.
    return BarCache(market_cache, fixed_clock)


@pytest.mark.parametrize(("source", "symbol"), [(EQUITY_SOURCE, "SPY"), (CRYPTO_SOURCE, "BTC/USD")])
def test_appending_changed_historical_row_raises_immutability_error(
    market_cache: Path, writable_cache: BarCache, source: str, symbol: str
) -> None:
    hist = writable_cache.read(source, symbol).iloc[:50].copy()
    hist.index.name = "ts"
    before = _tree_hashes(market_cache)

    # Control: re-appending unchanged history is a no-op that adds nothing.
    assert writable_cache.append(source, symbol, hist) == 0

    changed = hist.copy()
    changed.iloc[10, changed.columns.get_loc("close")] += 0.01  # one historical close moved
    with pytest.raises(ImmutabilityError):
        writable_cache.append(source, symbol, changed)
    assert _tree_hashes(market_cache) == before  # the refused write left no trace
