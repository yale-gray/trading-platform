"""Seeded synthetic 15-minute bars for day-trading tests (ADR-0001 A21 bar contract).

The contract matches the data layer's intraday output: columns ``open, high, low, close, volume``
(float64), a tz-aware UTC index labelled at the bar *open*, regular session only (09:30-16:00
New York, so 26 bars a day; early-close days are shorter), sessions taken from the XNYS calendar.

Two kinds of builders:

* :func:`make_intraday_bars`: random but reproducible bars for many symbols (speed benchmark,
  smoke tests); everything derives from ``seed``;
* :func:`session_index` + :func:`frame_from_closes`: exact, hand-scripted bars for tests that
  check numbers by hand.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import exchange_calendars as xcals
import numpy as np
import pandas as pd

BAR = pd.Timedelta(minutes=15)
COLUMNS = ["open", "high", "low", "close", "volume"]

_CAL = xcals.get_calendar("XNYS", start="2015-01-01", end="2035-12-31")


def session_index(day: date) -> pd.DatetimeIndex:
    """UTC bar-open timestamps of the XNYS session on ``day`` (empty if it is not a session)."""
    label = pd.Timestamp(day)
    if not _CAL.is_session(label):
        return pd.DatetimeIndex([], tz="UTC")
    open_ = _CAL.session_open(label)
    close = _CAL.session_close(label)
    return pd.date_range(open_, close, freq=BAR, inclusive="left", tz="UTC")


def multi_session_index(start: date, end: date) -> pd.DatetimeIndex:
    """Concatenated :func:`session_index` of every session in ``[start, end]``."""
    labels = _CAL.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    parts = [
        pd.date_range(_CAL.session_open(d), _CAL.session_close(d), freq=BAR, inclusive="left")
        for d in labels
    ]
    if not parts:
        return pd.DatetimeIndex([], tz="UTC")
    return pd.DatetimeIndex(parts[0].append(parts[1:]), tz="UTC")


def frame_from_closes(
    index: pd.DatetimeIndex,
    closes: Sequence[float],
    *,
    opens: Sequence[float] | None = None,
    spread: float = 0.1,
    volume: float | Sequence[float] = 1000.0,
) -> pd.DataFrame:
    """Bars whose close is exactly ``closes``.

    ``open[t]`` defaults to ``close[t-1]`` (``close[0]`` for the first bar); ``high``/``low``
    are the body extremes plus/minus ``spread``. ``volume`` is a constant or a per-bar list.
    """
    c = np.asarray(closes, dtype="float64")
    if len(c) != len(index):
        raise ValueError("closes and index differ in length")
    o = np.r_[c[:1], c[:-1]] if opens is None else np.asarray(opens, dtype="float64")
    v = np.full(len(c), volume, dtype="float64") if np.isscalar(volume) else np.asarray(volume)
    return pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, c) + spread,
            "low": np.minimum(o, c) - spread,
            "close": c,
            "volume": v.astype("float64"),
        },
        index=index,
    )[COLUMNS]


def make_intraday_bars(
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    seed: int = 7,
    bar_vol: float = 0.0015,
    drift: float = 0.0,
) -> dict[str, pd.DataFrame]:
    """Reproducible random 15m bars for ``symbols`` over the XNYS sessions in ``[start, end]``.

    Each symbol follows ``market factor + own noise`` log-returns (standard deviation about
    ``bar_vol`` per bar, volatility clustered by session), starts near a symbol-specific price,
    gaps a little at each open, and has a U-shaped intraday volume profile. All randomness comes
    from ``np.random.default_rng(seed)``, drawn in a fixed order, so the same arguments give
    identical frames.
    """
    idx = multi_session_index(start, end)
    n = len(idx)
    if n == 0:
        return {}
    rng = np.random.default_rng(seed)
    # Position of each bar inside its session (for gaps and the volume profile).
    days = idx.tz_convert("America/New_York").normalize()
    codes = pd.factorize(days)[0]
    first = np.r_[True, codes[1:] != codes[:-1]]
    start_pos = np.maximum.accumulate(np.where(first, np.arange(n), 0))
    bar_in_day = np.arange(n) - start_pos
    n_days = int(codes.max()) + 1
    market = rng.standard_normal(n) * bar_vol * 0.5
    day_vol = np.exp(rng.standard_normal(n_days) * 0.25)[codes]
    u_shape = 1.0 + 1.5 * np.exp(-bar_in_day / 3.0) + 1.0 * np.exp(-(25 - bar_in_day) / 3.0)
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        base = float(rng.uniform(20.0, 300.0))
        beta = float(rng.uniform(0.6, 1.4))
        idio = rng.standard_normal(n) * bar_vol * day_vol
        gap = np.where(first, rng.standard_normal(n) * bar_vol * 2.0, 0.0)
        ret = drift + beta * market + idio + gap
        close = base * np.exp(np.cumsum(ret))
        open_ = np.r_[base, close[:-1]]
        open_ = np.where(first, open_ * np.exp(gap), open_)  # the gap shows up in the open
        wick_h = np.abs(rng.standard_normal(n)) * bar_vol * 0.5
        wick_l = np.abs(rng.standard_normal(n)) * bar_vol * 0.5
        high = np.maximum(open_, close) * (1.0 + wick_h)
        low = np.minimum(open_, close) * (1.0 - wick_l)
        vol = np.round(
            rng.uniform(0.7, 1.3) * 20000.0 * u_shape * np.exp(rng.standard_normal(n) * 0.3)
        )
        out[sym] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx
        )[COLUMNS].astype("float64")
    return out


def assert_bar_contract(frames: Mapping[str, pd.DataFrame]) -> None:
    """Raise unless every frame meets the A21 intraday bar contract (used by tests)."""
    for sym, df in frames.items():
        idx = df.index
        assert isinstance(idx, pd.DatetimeIndex) and str(idx.tz) == "UTC", sym
        assert idx.is_monotonic_increasing and idx.is_unique, sym
        assert list(df.columns) == COLUMNS and (df.dtypes == "float64").all(), sym
        ny = idx.tz_convert("America/New_York")
        assert ((ny.hour * 60 + ny.minute) >= 570).all(), sym  # >= 09:30
        assert ((ny.hour * 60 + ny.minute) < 960).all(), sym  # < 16:00
        assert (idx.minute % 15 == 0).all(), sym
