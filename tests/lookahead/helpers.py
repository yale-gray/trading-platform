"""Lookahead checkers (SPEC-005 section 3).

Both checkers take ``fn: bars -> Series | DataFrame`` aligned to ``bars.index`` and raise
``AssertionError`` when the output at bar t depends on any bar after t. They are generic so
indicator adapters and (Wave 3) strategy signal functions plug in the same way.

Imported by sibling test modules as ``from helpers import ...`` (pytest prepend import mode
puts ``tests/lookahead`` on ``sys.path``; the directory deliberately has no ``__init__.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

Frame = pd.Series | pd.DataFrame
BarsFn = Callable[[pd.DataFrame], Frame]

_PRICE_COLS = ("open", "high", "low", "close")
_POISON_COLS = (*_PRICE_COLS, "volume")


def _frame(x: Frame) -> pd.DataFrame:
    return x.to_frame() if isinstance(x, pd.Series) else x


def select_ts(n: int, warmup: int, stride: int = 7, dense_below: int = 300) -> list[int]:
    """Bar positions t to check: all t for short series, else a strided sample.

    Long series get every ``stride``-th t from ``warmup - 2``, the first 3 and last 3 bars, and
    the warmup boundary exactly (warmup-1, warmup, warmup+1).
    """
    if n <= dense_below:
        return list(range(n))
    picks = set(range(max(warmup - 2, 0), n, stride))
    picks.update(range(min(3, n)))
    picks.update(range(max(n - 3, 0), n))
    picks.update(t for t in (warmup - 1, warmup, warmup + 1) if 0 <= t < n)
    return sorted(picks)


def repair_ohlc(bars: pd.DataFrame) -> pd.DataFrame:
    """Make high/low consistent with open/close so validators cannot mask a leak."""
    out = bars.copy()
    if all(c in out.columns for c in _PRICE_COLS):
        out["high"] = out[list(_PRICE_COLS)].max(axis=1)
        out["low"] = out[list(_PRICE_COLS)].min(axis=1)
    return out


def assert_truncation_invariant(
    fn: BarsFn, bars: pd.DataFrame, ts_indices: Iterable[int], rtol: float = 0.0
) -> int:
    """Output at t from ``bars[:t+1]`` must equal output at t from the full data.

    Returns the number of non-NaN cells compared, so callers can assert the check was not
    vacuous (NaN == NaN counts as equal).
    """
    full = _frame(fn(bars))
    compared = 0
    for t in ts_indices:
        prefix = _frame(fn(bars.iloc[: t + 1]))
        assert len(prefix) == t + 1, f"fn output not aligned to input at t={t}"
        expected = full.iloc[t : t + 1]
        try:
            assert_frame_equal(
                prefix.iloc[-1:],
                expected,
                check_exact=(rtol == 0.0),
                rtol=rtol,
                check_names=False,
            )
        except AssertionError as exc:
            raise AssertionError(f"truncation invariance violated at t={t}: {exc}") from exc
        compared += int(expected.notna().to_numpy().sum())
    return compared


def assert_future_perturbation_invariant(
    fn: BarsFn, bars: pd.DataFrame, ts_indices: Iterable[int], rng: np.random.Generator
) -> int:
    """Output at t must not change when all bars after t are replaced by random garbage.

    Stronger than truncation: catches centered windows, backward fill, whole-series
    normalisation and shift(-1) even when the code tolerates missing future bars.
    Returns the number of non-NaN cells compared.
    """
    base = _frame(fn(bars))
    cols = [c for c in _POISON_COLS if c in bars.columns]
    compared = 0
    for t in ts_indices:
        poisoned = bars.copy()
        tail = poisoned.iloc[t + 1 :]
        if len(tail):
            poisoned.iloc[t + 1 :, [poisoned.columns.get_loc(c) for c in cols]] = rng.uniform(
                0.01, 1e6, size=(len(tail), len(cols))
            )
            poisoned = repair_ohlc(poisoned)
        got = _frame(fn(poisoned)).iloc[t : t + 1]
        expected = base.iloc[t : t + 1]
        try:
            assert_frame_equal(got, expected, check_exact=True, check_names=False)
        except AssertionError as exc:
            raise AssertionError(f"future perturbation changed output at t={t}: {exc}") from exc
        compared += int(expected.notna().to_numpy().sum())
    return compared
