"""Every INDICATORS entry passes truncation invariance and future poisoning (SPEC-005 s3)."""

from __future__ import annotations

import importlib
from collections.abc import Callable
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

from tradebot.strategy.indicators import (
    INDICATORS,
    atr,
    bbands,
    devatr,
    ema,
    macd,
    noise_lower,
    noise_upper,
    ret,
    rsi,
    sma,
    vol,
)

# name -> (adapter bars -> output, warmup = first valid bar). Exact comparison (rtol=0) for all.
INDICATOR_CASES: dict[str, tuple[BarsFn, int]] = {
    "sma": (lambda b: sma(b["close"], 20), 19),
    "ema": (lambda b: ema(b["close"], 20), 19),
    "ret": (lambda b: ret(b["close"], 20, skip=5), 25),
    "rsi": (lambda b: rsi(b["close"], 14), 14),
    "macd": (lambda b: macd(b["close"]), 33),
    "bbands": (lambda b: bbands(b["close"]), 19),
    "atr": (lambda b: atr(b["high"], b["low"], b["close"], 14), 14),
    "devatr": (lambda b: devatr(b["high"], b["low"], b["close"], 20), 20),
    "vol": (lambda b: vol(b["close"], 20), 20),
    "noise_upper": (lambda b: noise_upper(b["open"], b["close"], 20), 20),
    "noise_lower": (lambda b: noise_lower(b["open"], b["close"], 20), 20),
}
_NAMES = sorted(INDICATOR_CASES)


@pytest.fixture
def spy_bars(market_cache: Path) -> pd.DataFrame:
    (path,) = (market_cache / "yfinance" / "SPY").glob("gen-*/bars.parquet")
    bars = pd.read_parquet(path)[["open", "high", "low", "close", "volume"]]
    assert len(bars) == 1761
    return bars


@pytest.mark.parametrize("name", _NAMES)
def test_truncation_invariance(name: str, spy_bars: pd.DataFrame) -> None:
    fn, warmup = INDICATOR_CASES[name]
    ts = select_ts(len(spy_bars), warmup)
    assert assert_truncation_invariant(fn, spy_bars, ts) > 0


@pytest.mark.parametrize("name", _NAMES)
def test_future_poisoning_invariance(name: str, spy_bars: pd.DataFrame) -> None:
    fn, warmup = INDICATOR_CASES[name]
    ts = select_ts(len(spy_bars), warmup)
    rng = np.random.default_rng(20250101)
    assert assert_future_perturbation_invariant(fn, spy_bars, ts, rng) > 0


def test_every_indicator_is_covered() -> None:
    """Adding an indicator to INDICATORS without a lookahead case fails here."""
    assert set(_NAMES) == set(INDICATORS), (
        f"missing lookahead cases: {sorted(set(INDICATORS) - set(_NAMES))}; "
        f"stale cases: {sorted(set(_NAMES) - set(INDICATORS))}"
    )


def test_every_strategy_is_covered() -> None:
    """Same completeness rule for STRATEGIES once SPEC-002 T3 lands (skips until then).

    STRATEGIES lives in ``tradebot.strategy.schema`` as a lazy attribute.
    """
    try:
        schema = importlib.import_module("tradebot.strategy.schema")
    except ImportError:
        pytest.skip("tradebot.strategy.schema does not exist yet")
    registry: dict[str, Callable] | None = getattr(schema, "STRATEGIES", None)
    if registry is None:
        pytest.skip("STRATEGIES registry does not exist yet")
    if not Path(__file__).with_name("test_strategy_truncation.py").exists():
        pytest.skip("strategy truncation tests arrive in Wave 3 (002 T9)")
    try:
        covered = importlib.import_module("test_strategy_truncation").STRATEGY_NAMES
    except AttributeError:
        pytest.fail("STRATEGIES exists but test_strategy_truncation.STRATEGY_NAMES is missing")
    assert set(covered) == set(registry)
