"""Seeded in-memory synthetic bars for the gate tests (SPEC-005 section 7, T18).

Independent of the committed fixtures (7 years) and of ``scripts/make_fixtures.py``: a fixture
regeneration cannot change these. Daily business-day bars, UTC, bar contract compliant.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _frame(
    index: pd.DatetimeIndex, rets: np.ndarray, rng: np.random.Generator, p0: float = 100.0
) -> pd.DataFrame:
    close = p0 * np.cumprod(1.0 + rets)
    prev = np.concatenate([[p0], close[:-1]])
    open_ = prev * (1.0 + rng.normal(0.0, 0.0005, len(close)))
    wick_hi = np.abs(rng.normal(0.0, 0.003, len(close)))
    wick_lo = np.abs(rng.normal(0.0, 0.003, len(close)))
    high = np.maximum(open_, close) * (1.0 + wick_hi)
    low = np.minimum(open_, close) * (1.0 - wick_lo)
    vol = rng.uniform(1e6, 2e6, len(close))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=index
    ).astype("float64")


def business_index(years: float, start: str = "2010-01-04") -> pd.DatetimeIndex:
    """Weekday index, UTC midnight, about ``252 * years`` bars."""
    n = int(round(252 * years))
    return pd.bdate_range(start, periods=n, tz="UTC")


def random_walk(
    seed: int, years: float = 12.0, drift: float = 0.0, vol: float = 0.01
) -> pd.DataFrame:
    """Zero-drift (by default) daily random walk; the memorizer's market."""
    rng = np.random.default_rng(seed)
    idx = business_index(years)
    rets = rng.normal(drift, vol, len(idx))
    return _frame(idx, rets, rng)


def regime_market(seed: int, years: float = 12.0) -> pd.DataFrame:
    """Persistent bull and bear regimes, alternating about every half year (a trend market)."""
    rng = np.random.default_rng(seed)
    idx = business_index(years)
    n = len(idx)
    rets = np.empty(n)
    i, bull = 0, True
    while i < n:
        length = int(rng.integers(100, 180))
        mu, sd = (0.0012, 0.008) if bull else (-0.0018, 0.016)
        rets[i : i + length] = rng.normal(mu, sd, min(length, n - i))
        i += length
        bull = not bull
    return _frame(idx, rets, rng)
