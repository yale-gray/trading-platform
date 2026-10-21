"""Restatement / reconciliation tests (SPEC-001 AC 6, Section 5)."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tradebot.clock import FixedClock
from tradebot.data.cache import BarCache
from tradebot.data.errors import DataRevisionError
from tradebot.data.restatement import DEFAULT_TOL, reconcile_overlap

PX = ["open", "high", "low", "close"]
SRC = "yfinance"
SYM = "SPY"


def make_frame(n: int, seed: int = 0) -> pd.DataFrame:
    """Deterministic float64 daily bar frame of ``n`` rows."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC", name="ts")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(1_000, 100_000, n).astype("float64"),
        },
        index=idx,
    )


def new_cache(root: Path) -> BarCache:
    return BarCache(root, FixedClock(datetime(2025, 1, 1, tzinfo=UTC)))


def _scaled(frame: pd.DataFrame, r: float) -> pd.DataFrame:
    out = frame.copy()
    out[PX] = out[PX] * r
    return out


@pytest.mark.unit
def test_identical_overlap_is_no_change() -> None:
    full = make_frame(40)
    res = reconcile_overlap(full.iloc[:30], full.iloc[20:])
    assert res.kind == "no_change"
    assert res.ratio == 1.0
    assert res.n_overlap == 10
    assert list(res.new_rows.index) == list(full.index[30:])
    pd.testing.assert_frame_equal(res.new_rows, full.iloc[30:])


@pytest.mark.unit
def test_constant_ratio_is_restatement_and_rescales_new_rows() -> None:
    full = make_frame(40)
    res = reconcile_overlap(full.iloc[:30], _scaled(full.iloc[20:], 0.8))
    assert res.kind == "restatement"
    assert res.ratio == pytest.approx(1.25)
    np.testing.assert_allclose(res.new_rows[PX].to_numpy(), full.iloc[30:][PX].to_numpy())
    # volume is left as reported
    np.testing.assert_array_equal(res.new_rows["volume"], full.iloc[30:]["volume"])


@pytest.mark.unit
def test_varying_ratio_raises_revision() -> None:
    full = make_frame(40)
    fetched = full.iloc[20:].copy()
    fetched["close"] *= np.linspace(0.95, 1.05, len(fetched))
    with pytest.raises(DataRevisionError):
        reconcile_overlap(full.iloc[:30], fetched)


@pytest.mark.unit
def test_no_overlap_raises_revision() -> None:
    full = make_frame(60)
    with pytest.raises(DataRevisionError):
        reconcile_overlap(full.iloc[:30], full.iloc[40:])


@pytest.mark.unit
def test_non_positive_overlap_close_raises_revision() -> None:
    full = make_frame(40)
    fetched = full.iloc[20:].copy()
    fetched.iloc[3, fetched.columns.get_loc("close")] = 0.0
    with pytest.raises(DataRevisionError):
        reconcile_overlap(full.iloc[:30], fetched)


@pytest.mark.unit
def test_empty_cached_raises_value_error() -> None:
    full = make_frame(10)
    with pytest.raises(ValueError):
        reconcile_overlap(full.iloc[:0], full)


@pytest.mark.unit
def test_reconcile_never_returns_cached_rows() -> None:
    full = make_frame(40)
    res = reconcile_overlap(full.iloc[:30], _scaled(full.iloc[10:], 2.0))
    assert res.new_rows.index.min() > full.index[29]


@pytest.mark.unit
def test_one_bar_overlap_with_three_percent_change_is_not_a_restatement() -> None:
    full = make_frame(40)
    fetched = _scaled(full.iloc[29:], 1.0 / 1.03)  # only the last cached bar is shared
    with pytest.raises(DataRevisionError, match="minimum"):
        reconcile_overlap(full.iloc[:30], fetched)


@pytest.mark.unit
def test_two_bar_overlap_restatement_refused_but_identical_overlap_is_no_change() -> None:
    full = make_frame(40)
    with pytest.raises(DataRevisionError):
        reconcile_overlap(full.iloc[:30], _scaled(full.iloc[28:], 0.5))
    res = reconcile_overlap(full.iloc[:30], full.iloc[29:])
    assert res.kind == "no_change"
    assert res.n_overlap == 1


@pytest.mark.unit
def test_min_overlap_is_a_parameter() -> None:
    full = make_frame(40)
    fetched = _scaled(full.iloc[28:], 0.5)
    res = reconcile_overlap(full.iloc[:30], fetched, min_overlap=2)
    assert res.kind == "restatement"
    assert res.ratio == pytest.approx(2.0)


@pytest.mark.unit
@pytest.mark.parametrize("true_scale", [1.0, 0.9958])
def test_two_ppm_jitter_is_not_a_revision(true_scale: float) -> None:
    full = make_frame(60)
    rng = np.random.default_rng(1)
    for _ in range(100):
        fetched = _scaled(full.iloc[40:], true_scale)
        fetched["close"] *= 1.0 + rng.uniform(-2e-6, 2e-6, len(fetched))
        res = reconcile_overlap(full.iloc[:50], fetched)
        assert res.kind == ("no_change" if true_scale == 1.0 else "restatement")


@pytest.mark.unit
@pytest.mark.parametrize("true_scale", [1.0, 0.9958])
def test_single_bar_change_of_1e_3_is_a_revision(true_scale: float) -> None:
    full = make_frame(60)
    for pos in range(10):
        fetched = _scaled(full.iloc[40:], true_scale)
        fetched.iloc[pos, fetched.columns.get_loc("close")] *= 1.001
        with pytest.raises(DataRevisionError):
            reconcile_overlap(full.iloc[:50], fetched)


@pytest.mark.property
@settings(max_examples=60)
@given(
    r=st.floats(0.2, 5.0),
    n=st.integers(5, 30),
    overlap=st.integers(3, 10),
    k=st.integers(0, 8),
    seed=st.integers(0, 10_000),
)
def test_constant_ratio_classified_as_restatement(
    r: float, n: int, overlap: int, k: int, seed: int
) -> None:
    assume(abs(r - 1.0) > 10 * DEFAULT_TOL)
    overlap = min(overlap, n)
    full = make_frame(n + k, seed=seed)
    cached = full.iloc[:n]
    fetched = _scaled(full.iloc[n - overlap :], 1.0 / r)
    res = reconcile_overlap(cached, fetched)
    assert res.kind == "restatement"
    assert res.ratio == pytest.approx(r, rel=1e-9)
    assert res.n_overlap == overlap
    np.testing.assert_allclose(res.new_rows[PX].to_numpy(), full.iloc[n:][PX].to_numpy(), rtol=1e-9)


@pytest.mark.property
@settings(max_examples=60)
@given(
    r=st.floats(0.2, 5.0),
    n=st.integers(5, 30),
    overlap=st.integers(2, 10),
    which=st.integers(0, 9),
    delta=st.floats(1e-3, 0.5),
    sign=st.sampled_from([-1.0, 1.0]),
    seed=st.integers(0, 10_000),
)
def test_non_constant_perturbation_above_tol_is_revision(
    r: float, n: int, overlap: int, which: int, delta: float, sign: float, seed: int
) -> None:
    overlap = min(overlap, n)
    full = make_frame(n + 3, seed=seed)
    fetched = _scaled(full.iloc[n - overlap :], 1.0 / r)
    pos = which % overlap
    fetched.iloc[pos, fetched.columns.get_loc("close")] *= 1.0 + sign * delta
    with pytest.raises(DataRevisionError):
        reconcile_overlap(full.iloc[:n], fetched)


_step = st.one_of(
    st.tuples(st.just("none"), st.just(0), st.just(1.0)),
    st.tuples(st.just("append"), st.integers(1, 6), st.just(1.0)),
    st.tuples(st.just("restate"), st.integers(0, 6), st.floats(0.25, 4.0)),
)


@pytest.mark.property
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=st.lists(_step, min_size=1, max_size=5), seed=st.integers(0, 10_000))
def test_random_fetch_sequences_never_alter_old_rows_and_stay_continuous(
    steps: list[tuple[str, int, float]], seed: int
) -> None:
    total = 30 + sum(k for _, k, _ in steps)
    truth = make_frame(total, seed=seed)  # the series on the cache's original basis
    n = 30
    scale = 1.0  # provider's current restatement scale relative to the cache basis
    with tempfile.TemporaryDirectory() as d:
        cache = new_cache(Path(d))
        cache.create(SRC, SYM, truth.iloc[:n], basis="synthetic_test")
        for kind, k, ratio in steps:
            snapshot = cache.read(SRC, SYM)
            if kind == "restate":
                scale *= ratio
            window = truth.iloc[max(0, n - 10) : n + k]
            fetched = _scaled(window, scale)
            res = cache.apply_fetch(SRC, SYM, fetched)
            n += k
            after = cache.read(SRC, SYM)
            # old rows never altered (bitwise)
            assert after.iloc[: len(snapshot)].to_numpy().tobytes() == snapshot.to_numpy().tobytes()
            assert len(after) == n
            assert after.index.is_monotonic_increasing and after.index.is_unique
            if abs(scale - 1.0) > 10 * DEFAULT_TOL:
                assert res.kind == "restatement"
            if k:
                # chain-link continuity: the boundary return equals the fetched series' own
                boundary_cached = after["close"].iloc[n - k] / after["close"].iloc[n - k - 1]
                boundary_fetched = (
                    fetched["close"].loc[after.index[n - k]]
                    / fetched["close"].loc[after.index[n - k - 1]]
                )
                assert boundary_cached == pytest.approx(boundary_fetched, rel=1e-9)
                # new rows are back on the cache's basis
                np.testing.assert_allclose(
                    after[PX].iloc[n - k :].to_numpy(),
                    truth[PX].iloc[n - k : n].to_numpy(),
                    rtol=1e-9,
                )
        _ = cache.read(SRC, SYM)


@pytest.mark.property
@settings(
    max_examples=25,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    ratios=st.lists(st.floats(0.5, 2.0), min_size=1, max_size=4),
    seed=st.integers(0, 10_000),
)
def test_revision_never_appends_anything(ratios: list[float], seed: int) -> None:
    truth = make_frame(50, seed=seed)
    with tempfile.TemporaryDirectory() as d:
        cache = new_cache(Path(d))
        cache.create(SRC, SYM, truth.iloc[:40], basis="synthetic_test")
        before = cache.read(SRC, SYM)
        sha = cache.gen_info(SRC, SYM)["sha256"]
        for i, r in enumerate(ratios):
            fetched = truth.iloc[30:].copy()
            fetched["close"] *= r * (1.0 + 0.01 * (np.arange(len(fetched)) % (i + 2)))
            with pytest.raises(DataRevisionError):
                cache.apply_fetch(SRC, SYM, fetched)
        assert cache.gen_info(SRC, SYM)["sha256"] == sha
        pd.testing.assert_frame_equal(cache.read(SRC, SYM), before)
