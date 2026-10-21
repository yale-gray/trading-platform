"""Canaries: the checkers must fail on deliberately leaky functions (SPEC-005 s3)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from helpers import (
    BarsFn,
    assert_future_perturbation_invariant,
    assert_truncation_invariant,
    select_ts,
)

LEAKS: dict[str, BarsFn] = {
    "shift_minus_1": lambda b: b["close"].shift(-1),
    "centered_rolling": lambda b: b["close"].rolling(5, center=True).mean(),
    "global_normalisation": lambda b: b["close"] / b["close"].max(),
    # bfill only leaks where the input has leading NaN, so give it a warmup region
    "bfill": lambda b: b["close"].rolling(5).mean().bfill(),
}


@pytest.fixture
def bars(market_cache: Path) -> pd.DataFrame:
    (path,) = (market_cache / "yfinance" / "SPY").glob("gen-*/bars.parquet")
    return pd.read_parquet(path)[["open", "high", "low", "close", "volume"]]


@pytest.mark.parametrize("name", sorted(LEAKS))
def test_truncation_checker_catches_leak(name: str, bars: pd.DataFrame) -> None:
    with pytest.raises(AssertionError, match="truncation invariance violated"):
        assert_truncation_invariant(LEAKS[name], bars, select_ts(len(bars), 0))


@pytest.mark.parametrize("name", sorted(LEAKS))
def test_poisoning_checker_catches_leak(name: str, bars: pd.DataFrame) -> None:
    rng = np.random.default_rng(1)
    with pytest.raises(AssertionError, match="future perturbation changed output"):
        assert_future_perturbation_invariant(LEAKS[name], bars, select_ts(len(bars), 0), rng)


def test_checkers_pass_a_causal_function(bars: pd.DataFrame) -> None:
    """Control: the same checkers accept a clean function, so failures above are real."""
    fn: BarsFn = lambda b: b["close"].rolling(5).mean()  # noqa: E731
    ts = select_ts(len(bars), 4)
    assert assert_truncation_invariant(fn, bars, ts) > 0
    assert assert_future_perturbation_invariant(fn, bars, ts, np.random.default_rng(1)) > 0
