"""Manifest and content checks for the committed synthetic market fixtures (SPEC-005 section 2).

Reads the committed parquet and JSON directly. It never calls scripts/make_fixtures.py, so a
silent edit to a fixture, or a manifest that drifts from the files, fails here.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.unit

SEED = 20180102
START = "2018-01-02"
END = "2024-12-31"
SOL_START = "2020-04-10"
BUDGET_TOTAL = 2_500_000
BUDGET_FILE = 200_000
TREES = ("market", "market_edge")

ETFS = [
    "SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "XLK",
    "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
]  # fmt: skip
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD"]
CLEAN_SYMBOLS = ETFS + CRYPTO
EDGE_SYMBOLS = ["ZZNEW", "ZZGONE", "ZZGAP", "ZZDUP", "ZZTZ", "ZZBAD", "ZZFLAT", "ZZUP"]
OHLCV = ["open", "high", "low", "close", "volume"]
AUDIT = ["fetched_at", "source_rev"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _tree_files(root: Path) -> set[str]:
    """Every file under the tree except the manifest itself, as posix paths relative to root."""
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }


def _bars_path(root: Path, symbol: str) -> Path:
    """Path of a symbol's bars.parquet; BTC/USD lives in BTC_USD/ on disk."""
    matches = sorted(root.glob(f"*/{symbol.replace('/', '_')}/gen-0001/bars.parquet"))
    assert len(matches) == 1, f"{symbol}: expected one bars.parquet under {root}, got {matches}"
    return matches[0]


def _read(root: Path, symbol: str) -> pd.DataFrame:
    return pd.read_parquet(_bars_path(root, symbol))


def _utc(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Index as UTC with nanosecond resolution, so it compares with calendar output."""
    out = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return out.as_unit("ns")


@cache
def _xnys(start: str, end: str) -> pd.DatetimeIndex:
    """Real XNYS sessions (exchange_calendars is offline) as tz-aware UTC midnight."""
    sessions = xcals.get_calendar("XNYS").sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return _utc(pd.DatetimeIndex(sessions))


def _every_day(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="D", tz="UTC").as_unit("ns")


def _ts(day: str) -> pd.Timestamp:
    return pd.Timestamp(day, tz="UTC")


def _assert_same_index(got: pd.DatetimeIndex, want: pd.DatetimeIndex, label: str) -> None:
    if got.equals(want):
        return
    missing = want.difference(got)
    extra = got.difference(want)
    raise AssertionError(
        f"{label}: index differs; {len(missing)} expected missing "
        f"(first {list(missing[:5])}), {len(extra)} unexpected "
        f"(first {list(extra[:5])}); got {len(got)} rows, want {len(want)}"
    )


# ---------------------------------------------------------------- manifests and files
@pytest.mark.parametrize("tree", TREES)
def test_manifest_header_fields(fixtures_root: Path, tree: str) -> None:
    m = _manifest(fixtures_root / tree)
    assert m["synthetic"] is True
    assert m["seed"] == SEED
    assert m["schema"] == 1
    assert m["generator"] == "scripts/make_fixtures.py"
    assert m["generator_version"] == 1


@pytest.mark.parametrize("tree", TREES)
def test_manifest_lists_exactly_the_files_on_disk(fixtures_root: Path, tree: str) -> None:
    root = fixtures_root / tree
    listed = set(_manifest(root)["files"])
    on_disk = _tree_files(root)
    assert listed == on_disk, (
        f"not in manifest: {sorted(on_disk - listed)}; "
        f"listed but missing on disk: {sorted(listed - on_disk)}"
    )


@pytest.mark.parametrize("tree", TREES)
def test_file_sha256_and_bytes_match_manifest(fixtures_root: Path, tree: str) -> None:
    root = fixtures_root / tree
    for rel, entry in _manifest(root)["files"].items():
        path = root / rel
        assert path.stat().st_size == entry["bytes"], f"{rel}: size differs from manifest"
        assert _sha256(path) == entry["sha256"], f"{rel}: sha256 differs from manifest"


@pytest.mark.parametrize("tree", TREES)
def test_manifest_total_bytes_is_real_sum(fixtures_root: Path, tree: str) -> None:
    root = fixtures_root / tree
    m = _manifest(root)
    real = sum((root / rel).stat().st_size for rel in m["files"])
    assert m["total_bytes"] == real
    assert sum(e["bytes"] for e in m["files"].values()) == real


@pytest.mark.parametrize("tree", TREES)
def test_parquet_rows_and_date_range_match_manifest(fixtures_root: Path, tree: str) -> None:
    root = fixtures_root / tree
    checked = 0
    for rel, entry in _manifest(root)["files"].items():
        if not rel.endswith("bars.parquet"):
            continue
        checked += 1
        df = pd.read_parquet(root / rel)
        idx = _utc(df.index)
        assert entry["rows"] == len(df), f"{rel}: row count"
        assert entry["first"] == idx.min().strftime("%Y-%m-%d"), f"{rel}: first date"
        assert entry["last"] == idx.max().strftime("%Y-%m-%d"), f"{rel}: last date"
    assert checked > 0


@pytest.mark.parametrize("tree", TREES)
def test_gen_json_sha256_equals_bars_sha256(fixtures_root: Path, tree: str) -> None:
    root = fixtures_root / tree
    files = _manifest(root)["files"]
    gen_files = [rel for rel in files if rel.endswith("gen-0001/gen.json")]
    assert gen_files, "no gen.json entries in manifest"
    for gen_rel in gen_files:
        bars_rel = gen_rel.replace("gen.json", "bars.parquet")
        assert bars_rel in files, f"{gen_rel}: sibling bars.parquet not in manifest"
        bars_sha = _sha256(root / bars_rel)
        assert json.loads((root / gen_rel).read_text(encoding="utf-8"))["sha256"] == bars_sha
        assert files[bars_rel]["sha256"] == bars_sha


def test_fixture_budget(fixtures_root: Path) -> None:
    every_file = [p for p in fixtures_root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in every_file)
    assert total <= BUDGET_TOTAL, f"tests/fixtures is {total} bytes, budget {BUDGET_TOTAL}"
    for p in every_file:
        if p.suffix == ".parquet":
            size = p.stat().st_size
            assert size <= BUDGET_FILE, f"{p.name} is {size} bytes, budget {BUDGET_FILE}"


# ---------------------------------------------------------------- clean universe
def test_clean_universe_has_the_20_expected_symbols(fixtures_root: Path) -> None:
    root = fixtures_root / "market"
    found: dict[str, str] = {}
    for p in root.glob("*/*/gen-0001/bars.parquet"):
        found[p.parents[1].name.replace("_", "/")] = p.parents[2].name
    assert set(found) == set(CLEAN_SYMBOLS)
    for sym, src in found.items():
        want = "coinbaseexchange" if sym in CRYPTO else "yfinance"
        assert src == want, f"{sym}: source {src}, want {want}"


@pytest.mark.parametrize("symbol", ETFS)
def test_etf_index_equals_xnys_sessions(fixtures_root: Path, symbol: str) -> None:
    idx = _utc(_read(fixtures_root / "market", symbol).index)
    _assert_same_index(idx, _xnys(START, END), symbol)
    assert len(idx) == 1761, symbol


def test_holidays_absent_from_etf_index(fixtures_root: Path) -> None:
    idx = _utc(_read(fixtures_root / "market", "SPY").index)
    for day in ("2019-07-04", "2018-12-05"):
        assert _ts(day) not in _xnys(START, END), f"{day} is not an XNYS session"
        assert _ts(day) not in idx, f"{day} must be absent from SPY"


@pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/USD"])
def test_crypto_has_every_calendar_day(fixtures_root: Path, symbol: str) -> None:
    idx = _utc(_read(fixtures_root / "market", symbol).index)
    want = _every_day(START, END)
    assert len(want) == 2556
    _assert_same_index(idx, want, symbol)


def test_sol_starts_2020_04_10_and_has_every_day_after(fixtures_root: Path) -> None:
    idx = _utc(_read(fixtures_root / "market", "SOL/USD").index)
    assert idx[0] == _ts(SOL_START)
    assert idx[-1] == _ts(END)
    _assert_same_index(idx, _every_day(SOL_START, END), "SOL/USD")
    assert len(idx) == 1727


@pytest.mark.parametrize("symbol", CLEAN_SYMBOLS)
def test_clean_columns_and_index_are_utc_midnight_sorted_unique(
    fixtures_root: Path, symbol: str
) -> None:
    df = _read(fixtures_root / "market", symbol)
    assert list(df.columns) == OHLCV + AUDIT
    idx = df.index
    assert idx.name == "ts"
    assert str(idx.tz) == "UTC"
    assert idx.is_monotonic_increasing
    assert idx.is_unique
    assert (idx == idx.normalize()).all()


@pytest.mark.parametrize("symbol", CLEAN_SYMBOLS)
def test_clean_ohlc_sanity(fixtures_root: Path, symbol: str) -> None:
    df = _read(fixtures_root / "market", symbol)
    prices = df[["open", "high", "low", "close"]]
    assert not df[OHLCV].isna().any().any(), "NaN in OHLCV"
    o, h, lo, c = df["open"], df["high"], df["low"], df["close"]
    assert (lo <= np.minimum(o, c)).all(), "low above min(open, close)"
    assert (h >= np.maximum(o, c)).all(), "high below max(open, close)"
    assert (prices > 0).all().all(), "non-positive price"
    assert (df["volume"] >= 1).all(), "volume below 1"


# ---------------------------------------------------------------- edge set
def test_edge_set_has_the_eight_expected_symbols(fixtures_root: Path) -> None:
    root = fixtures_root / "market_edge"
    found = {p.parents[1].name for p in root.glob("*/*/gen-0001/bars.parquet")}
    assert found == set(EDGE_SYMBOLS)


def test_zznew_starts_2021_03_01(fixtures_root: Path) -> None:
    idx = _utc(_read(fixtures_root / "market_edge", "ZZNEW").index)
    _assert_same_index(idx, _xnys("2021-03-01", END), "ZZNEW")


def test_zzgone_last_bar_is_2022_06_30(fixtures_root: Path) -> None:
    idx = _utc(_read(fixtures_root / "market_edge", "ZZGONE").index)
    assert idx[-1] == _ts("2022-06-30")
    _assert_same_index(idx, _xnys(START, "2022-06-30"), "ZZGONE")


def test_zzgap_lacks_exactly_the_six_sessions(fixtures_root: Path) -> None:
    idx = _utc(_read(fixtures_root / "market_edge", "ZZGAP").index)
    expected = _xnys(START, "2019-12-31")
    missing = [d.strftime("%Y-%m-%d") for d in expected.difference(idx)]
    assert missing == [
        "2019-07-08",
        "2019-07-09",
        "2019-07-10",
        "2019-07-11",
        "2019-07-12",
        "2019-07-15",
    ]
    assert len(idx.difference(expected)) == 0, "ZZGAP has bars on non-sessions"


def test_zzdup_has_exactly_one_duplicate_timestamp_on_2019_03_12(fixtures_root: Path) -> None:
    df = _read(fixtures_root / "market_edge", "ZZDUP")
    idx = _utc(df.index)
    dups = idx[idx.duplicated()]
    assert list(dups) == [_ts("2019-03-12")]
    assert len(idx) == len(_xnys(START, "2019-12-31")) + 1
    pair = df.loc[_ts("2019-03-12"), "close"].to_numpy()
    assert len(pair) == 2
    assert np.isclose(pair[1], round(pair[0] * 1.01, 4), rtol=0, atol=1e-9)


def test_zztz_index_is_not_utc(fixtures_root: Path) -> None:
    df = _read(fixtures_root / "market_edge", "ZZTZ")
    assert df.index.tz is not None
    assert str(df.index.tz) != "UTC"
    assert str(df.index.tz) == "America/New_York"
    _assert_same_index(_utc(df.index), _xnys(START, "2019-12-31"), "ZZTZ")


def test_zzbad_has_one_high_below_low_row_and_one_zero_close(fixtures_root: Path) -> None:
    df = _read(fixtures_root / "market_edge", "ZZBAD")
    assert list(df.index[df["high"] < df["low"]]) == [_ts("2019-05-15")]
    assert list(df.index[df["close"] == 0.0]) == [_ts("2019-05-16")]
    _assert_same_index(_utc(df.index), _xnys(START, "2019-12-31"), "ZZBAD")


def test_zzflat_is_constant_over_first_300_sessions(fixtures_root: Path) -> None:
    df = _read(fixtures_root / "market_edge", "ZZFLAT")
    _assert_same_index(_utc(df.index), _xnys(START, END)[:300], "ZZFLAT")
    for col in ("open", "high", "low", "close"):
        assert (df[col] == 100.0).all(), f"ZZFLAT {col} is not constant 100.0"
    assert (df["volume"] == 1_000_000.0).all()


def test_zzup_is_strictly_increasing_linear_series(fixtures_root: Path) -> None:
    df = _read(fixtures_root / "market_edge", "ZZUP")
    _assert_same_index(_utc(df.index), _xnys(START, END)[:300], "ZZUP")
    t = np.arange(300)
    close = 100.0 + 0.5 * t
    open_ = np.concatenate([[99.75], close[:-1]])
    np.testing.assert_allclose(df["close"].to_numpy(), close, rtol=0, atol=1e-9)
    np.testing.assert_allclose(df["open"].to_numpy(), open_, rtol=0, atol=1e-9)
    np.testing.assert_allclose(df["high"].to_numpy(), close, rtol=0, atol=1e-9)
    np.testing.assert_allclose(df["low"].to_numpy(), open_, rtol=0, atol=1e-9)
    assert (df["volume"] == 1_000_000.0).all()
    assert (df["close"].diff().dropna() > 0).all()
