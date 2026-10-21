"""Cache tests (SPEC-001 AC 4-6, Section 4): round-trip, immutability, generations."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tradebot.clock import FixedClock
from tradebot.data.cache import BarCache
from tradebot.data.errors import (
    CacheCorruptError,
    DataRevisionError,
    ImmutabilityError,
)
from tradebot.data.types import BAR_COLUMNS

SRC = "yfinance"
SYM = "SPY"
START = pd.Timestamp("2024-01-01", tz="UTC")


def make_frame(
    n: int, start: pd.Timestamp = START, scale: float = 1.0, seed: int = 0
) -> pd.DataFrame:
    """Deterministic float64 bar frame of ``n`` daily rows."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC", name="ts")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {
            "open": close * 0.999 * scale,
            "high": close * 1.01 * scale,
            "low": close * 0.99 * scale,
            "close": close * scale,
            "volume": rng.integers(1_000, 100_000, n).astype("float64"),
        },
        index=idx,
    )


def new_cache(root: Path) -> BarCache:
    return BarCache(root, FixedClock(datetime(2025, 1, 1, tzinfo=UTC)))


def _cmp(a: pd.DataFrame, b: pd.DataFrame) -> None:
    pd.testing.assert_frame_equal(a, b, check_freq=False, check_index_type=False)


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.unit
def test_create_read_round_trip(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    frame = make_frame(30)
    assert not cache.exists(SRC, SYM)
    assert cache.create(SRC, SYM, frame, basis="synthetic_test") == 1
    assert cache.exists(SRC, SYM)
    assert cache.current_gen(SRC, SYM) == 1
    assert cache.generations(SRC, SYM) == [1]
    out = cache.read(SRC, SYM)
    assert list(out.columns) == list(BAR_COLUMNS)
    _cmp(out, frame)
    info = cache.gen_info(SRC, SYM)
    assert info["first"] == "2024-01-01"
    assert info["last"] == "2024-01-30"
    assert info["events"] == []


@pytest.mark.unit
def test_create_twice_refused(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    cache.create(SRC, SYM, make_frame(5), basis="synthetic_test")
    with pytest.raises(ImmutabilityError):
        cache.create(SRC, SYM, make_frame(5), basis="synthetic_test")


@pytest.mark.unit
def test_read_window_and_audit_columns(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    frame = make_frame(30)
    cache.create(SRC, SYM, frame, basis="synthetic_test")
    s, e = frame.index[5], frame.index[9]
    assert len(cache.read(SRC, SYM, start=s, end=e)) == 5
    audited = cache.read(SRC, SYM, include_audit=True)
    assert {"fetched_at", "source_rev"} <= set(audited.columns)


@pytest.mark.unit
def test_symbol_with_slash_maps_to_filesystem_safe_dir(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    cache.create("coinbaseexchange", "BTC/USD", make_frame(5), basis="synthetic_test")
    assert (tmp_path / "coinbaseexchange" / "BTC_USD" / "meta.json").is_file()
    assert len(cache.read("coinbaseexchange", "BTC/USD")) == 5


@pytest.mark.unit
def test_append_adds_only_new_rows_and_skips_identical_overlap(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    full = make_frame(40)
    cache.create(SRC, SYM, full.iloc[:30], basis="synthetic_test")
    before = cache.read(SRC, SYM)
    added = cache.append(SRC, SYM, full.iloc[20:])  # 10 identical overlap rows + 10 new
    assert added == 10
    after = cache.read(SRC, SYM)
    _cmp(after, full)
    _cmp(after.iloc[:30], before)
    assert cache.append(SRC, SYM, full.iloc[35:]) == 0  # pure overlap


@pytest.mark.unit
def test_append_differing_overlap_raises_and_leaves_cache(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    full = make_frame(40)
    cache.create(SRC, SYM, full.iloc[:30], basis="synthetic_test")
    snapshot = _tree_hashes(tmp_path)
    bad = full.iloc[25:].copy()
    bad.iloc[2, bad.columns.get_loc("close")] += 0.01
    with pytest.raises(ImmutabilityError):
        cache.append(SRC, SYM, bad)
    assert _tree_hashes(tmp_path) == snapshot


@pytest.mark.unit
def test_append_interior_rows_refused(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    full = make_frame(30)
    gappy = full.drop(full.index[10])
    cache.create(SRC, SYM, gappy, basis="synthetic_test")
    with pytest.raises(ImmutabilityError):
        cache.append(SRC, SYM, full.iloc[[10]])


@pytest.mark.unit
def test_constant_ratio_restatement_rescales_only_new_rows_and_records_event(
    tmp_path: Path,
) -> None:
    cache = new_cache(tmp_path)
    true = make_frame(40)
    cache.create(SRC, SYM, true.iloc[:30], basis="synthetic_test")
    before = cache.read(SRC, SYM)
    fetched = true.iloc[20:].copy()
    fetched[["open", "high", "low", "close"]] *= 0.9  # provider restated by 0.9
    res = cache.apply_fetch(SRC, SYM, fetched)
    assert res.kind == "restatement"
    assert res.ratio == pytest.approx(1 / 0.9)
    assert res.n_overlap == 10
    after = cache.read(SRC, SYM)
    _cmp(after.iloc[:30], before)
    assert len(after) == 40
    # new rows land back on the cached basis
    np.testing.assert_allclose(
        after.iloc[30:][["open", "high", "low", "close"]].to_numpy(),
        true.iloc[30:][["open", "high", "low", "close"]].to_numpy(),
    )
    events = cache.gen_info(SRC, SYM)["events"]
    assert len(events) == 1
    assert events[0]["ratio"] == pytest.approx(1 / 0.9)
    assert events[0]["n_overlap"] == 10


@pytest.mark.unit
def test_restatement_without_new_rows_still_records_event(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    true = make_frame(30)
    cache.create(SRC, SYM, true, basis="synthetic_test")
    before = cache.read(SRC, SYM)
    fetched = true.iloc[20:].copy()
    fetched[["open", "high", "low", "close"]] *= 1.5
    res = cache.apply_fetch(SRC, SYM, fetched)
    assert res.kind == "restatement"
    _cmp(cache.read(SRC, SYM), before)
    assert len(cache.gen_info(SRC, SYM)["events"]) == 1


@pytest.mark.unit
def test_no_change_fetch_appends_without_event(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    true = make_frame(40)
    cache.create(SRC, SYM, true.iloc[:30], basis="synthetic_test")
    res = cache.apply_fetch(SRC, SYM, true.iloc[20:])
    assert res.kind == "no_change"
    assert len(cache.read(SRC, SYM)) == 40
    assert cache.gen_info(SRC, SYM)["events"] == []


@pytest.mark.unit
def test_varying_ratio_quarantines_and_appends_nothing(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    true = make_frame(40)
    cache.create(SRC, SYM, true.iloc[:30], basis="synthetic_test")
    before = cache.read(SRC, SYM)
    sha_before = cache.gen_info(SRC, SYM)["sha256"]
    fetched = true.iloc[20:].copy()
    fetched["close"] *= np.linspace(0.9, 1.1, len(fetched))
    with pytest.raises(DataRevisionError):
        cache.apply_fetch(SRC, SYM, fetched)
    _cmp(cache.read(SRC, SYM), before)
    assert cache.gen_info(SRC, SYM)["sha256"] == sha_before
    assert cache.gen_info(SRC, SYM)["events"] == []
    q = list((tmp_path / SRC / SYM / "quarantine").glob("*.parquet"))
    assert len(q) == 1
    _cmp(pd.read_parquet(q[0]), fetched)


@pytest.mark.unit
def test_no_overlap_fetch_is_rejected_and_quarantined(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    true = make_frame(50)
    cache.create(SRC, SYM, true.iloc[:30], basis="synthetic_test")
    with pytest.raises(DataRevisionError):
        cache.apply_fetch(SRC, SYM, true.iloc[40:])
    assert len(cache.read(SRC, SYM)) == 30


@pytest.mark.unit
def test_rebase_creates_new_generation_old_stays_readable(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    original = make_frame(30)
    cache.create(SRC, SYM, original, basis="synthetic_test")
    rebased = make_frame(30, scale=0.8)
    assert cache.rebase(SRC, SYM, rebased, basis="synthetic_rebased") == 2
    assert cache.current_gen(SRC, SYM) == 2
    assert cache.generations(SRC, SYM) == [1, 2]
    _cmp(cache.read(SRC, SYM), rebased)
    _cmp(cache.read(SRC, SYM, generation=1), original)
    assert cache.gen_info(SRC, SYM, 1)["basis"] == "synthetic_test"
    assert cache.gen_info(SRC, SYM)["basis"] == "synthetic_rebased"


@pytest.mark.unit
def test_fingerprint_changes_across_generations_and_appends(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    full = make_frame(40)
    cache.create(SRC, SYM, full.iloc[:30], basis="synthetic_test")
    entries = [(SRC, SYM)]
    fp1 = cache.fingerprint(entries)
    assert fp1 == cache.fingerprint(entries)
    assert fp1 != cache.fingerprint(entries, start=START)
    cache.append(SRC, SYM, full.iloc[30:])
    fp_appended = cache.fingerprint(entries)
    assert fp_appended != fp1
    cache.rebase(SRC, SYM, make_frame(40, scale=0.5), basis="synthetic_rebased")
    fp2 = cache.fingerprint(entries)
    assert fp2 not in (fp1, fp_appended)
    assert cache.fingerprint(entries, generation=1) == fp_appended


@pytest.mark.unit
def test_corrupted_bytes_raise_cache_corrupt(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    cache.create(SRC, SYM, make_frame(20), basis="synthetic_test")
    bars = tmp_path / SRC / SYM / "gen-0001" / "bars.parquet"
    data = bytearray(bars.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bars.write_bytes(bytes(data))
    with pytest.raises(CacheCorruptError):
        cache.read(SRC, SYM)


@pytest.mark.unit
def test_missing_bars_file_raises_cache_corrupt(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    cache.create(SRC, SYM, make_frame(20), basis="synthetic_test")
    (tmp_path / SRC / SYM / "gen-0001" / "bars.parquet").unlink()
    with pytest.raises(CacheCorruptError):
        cache.read(SRC, SYM)


@pytest.mark.unit
def test_atomic_write_leaves_no_partial_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = new_cache(tmp_path)
    full = make_frame(40)
    cache.create(SRC, SYM, full.iloc[:30], basis="synthetic_test")
    snapshot = _tree_hashes(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        cache.append(SRC, SYM, full.iloc[30:])
    monkeypatch.undo()
    assert _tree_hashes(tmp_path) == snapshot  # no stray .tmp, nothing altered
    assert not list(tmp_path.rglob("*.tmp"))
    assert len(cache.read(SRC, SYM)) == 30


@pytest.mark.unit
def test_atomic_write_failure_on_create_leaves_nothing_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = new_cache(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        cache.create(SRC, SYM, make_frame(10), basis="synthetic_test")
    monkeypatch.undo()
    assert not cache.exists(SRC, SYM)
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.unit
def test_crash_between_bars_write_and_gen_json_publish_keeps_old_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tradebot.data.cache as cache_mod

    cache = new_cache(tmp_path)
    full = make_frame(40)
    cache.create(SRC, SYM, full.iloc[:30], basis="synthetic_test")
    gdir = tmp_path / SRC / SYM / "gen-0001"
    old_gen_json = (gdir / "gen.json").read_bytes()

    def crash(path: Path, obj: dict[str, object]) -> None:
        if path.name == "gen.json":
            raise KeyboardInterrupt("simulated crash before publish")
        raise AssertionError("unexpected json write")

    monkeypatch.setattr(cache_mod, "_atomic_json", crash)
    with pytest.raises(KeyboardInterrupt):
        cache.append(SRC, SYM, full.iloc[30:])
    monkeypatch.undo()

    assert list(gdir.glob("bars-*.parquet"))  # the new bars file was written but not published
    assert (gdir / "gen.json").read_bytes() == old_gen_json
    info = cache.gen_info(SRC, SYM)
    assert _sha(gdir / "bars.parquet") == info["sha256"]
    _cmp(cache.read(SRC, SYM), full.iloc[:30])  # no CacheCorruptError

    # the cache is not wedged: a retry publishes and sweeps the orphan and the old file
    assert cache.append(SRC, SYM, full.iloc[30:]) == 10
    _cmp(cache.read(SRC, SYM), full)
    info = cache.gen_info(SRC, SYM)
    assert info["file"].startswith("bars-")
    assert sorted(p.name for p in gdir.glob("*.parquet")) == [info["file"]]
    assert _sha(gdir / info["file"]) == info["sha256"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.unit
def test_gen_json_without_file_name_reads_bars_parquet_unchanged(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    cache.create(SRC, SYM, make_frame(20), basis="synthetic_test")
    info = cache.gen_info(SRC, SYM)
    assert "file" not in info
    assert (tmp_path / SRC / SYM / "gen-0001" / "bars.parquet").is_file()


@pytest.mark.unit
def test_second_writer_is_refused_while_lock_held(tmp_path: Path) -> None:
    import fcntl

    import tradebot.data.cache as cache_mod
    from tradebot.data.errors import DataError

    cache = new_cache(tmp_path)
    full = make_frame(40)
    cache.create(SRC, SYM, full.iloc[:30], basis="synthetic_test")
    fd = os.open(tmp_path / SRC / SYM, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(DataError, match="lock"):
            with cache_mod._symbol_lock(tmp_path / SRC / SYM, timeout=0.1):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert cache.append(SRC, SYM, full.iloc[30:]) == 10


@pytest.mark.unit
def test_spot_basis_prefix_accepted(tmp_path: Path) -> None:
    cache = new_cache(tmp_path)
    cache.create("coinbaseexchange", "BTC/USD", make_frame(5), basis="spot_coinbaseexchange")
    assert cache.gen_info("coinbaseexchange", "BTC/USD")["basis"] == "spot_coinbaseexchange"


@pytest.mark.unit
def test_bad_basis_rejected(tmp_path: Path) -> None:
    from tradebot.data.errors import DataError

    with pytest.raises(DataError):
        new_cache(tmp_path).create(SRC, SYM, make_frame(5), basis="bogus")


@pytest.mark.unit
def test_committed_fixtures_readable_and_never_modified(market_cache: Path) -> None:
    cache = BarCache(market_cache, FixedClock(datetime(2025, 1, 1, tzinfo=UTC)))
    before = _tree_hashes(market_cache)
    committed = _tree_hashes(Path(__file__).parents[1] / "fixtures" / "market")
    assert before == committed
    assert cache.exists(SRC, SYM)
    bars = cache.read(SRC, SYM)
    assert not bars.empty
    assert list(bars.columns) == list(BAR_COLUMNS)
    assert cache.read(SRC, SYM, generation=cache.current_gen(SRC, SYM)).equals(bars)
    cache.fingerprint([(SRC, SYM)])
    assert _tree_hashes(market_cache) == before


@pytest.mark.property
@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    n=st.integers(5, 40),
    k=st.integers(1, 15),
    seed=st.integers(0, 10_000),
)
def test_property_round_trip_and_incremental_append_preserve_old_bytes(
    n: int, k: int, seed: int
) -> None:
    full = make_frame(n + k, seed=seed)
    with tempfile.TemporaryDirectory() as d:
        cache = new_cache(Path(d))
        cache.create(SRC, SYM, full.iloc[:n], basis="synthetic_test")
        before = cache.read(SRC, SYM)
        _cmp(before, full.iloc[:n])
        assert cache.append(SRC, SYM, full) == k
        after = cache.read(SRC, SYM)
        _cmp(after, full)
        assert after.iloc[:n].to_numpy().tobytes() == before.to_numpy().tobytes()
