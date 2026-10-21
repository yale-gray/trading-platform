"""Overlap reconciliation for adjusted-price restatements (SPEC-001 Section 5).

Adjusted prices are a function of future corporate actions, so a provider may
restate all of history by a constant factor. :func:`reconcile_overlap` compares
the cached tail with a fresh fetch over their overlapping dates and decides
whether the fetch is unchanged, a pure restatement (constant ratio) or a
genuine revision (varying ratio).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from tradebot.data.errors import DataRevisionError

DEFAULT_TOL = 1e-5
DEFAULT_MIN_OVERLAP = 3
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

ReconcileKind = Literal["no_change", "restatement"]


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a successful overlap reconciliation.

    ``new_rows`` holds only rows strictly after the cached last bar, already
    rescaled onto the cached basis. Cached rows are never part of the result.
    """

    kind: ReconcileKind
    ratio: float
    n_overlap: int
    new_rows: pd.DataFrame


def reconcile_overlap(
    cached: pd.DataFrame,
    fetched: pd.DataFrame,
    tol: float = DEFAULT_TOL,
    min_overlap: int = DEFAULT_MIN_OVERLAP,
) -> ReconcileResult:
    """Three-way reconcile of ``fetched`` against the ``cached`` basis.

    ``ratio_i = cached_close_i / fetched_close_i`` over the shared dates.
    ``tol`` is a **relative** tolerance (default ``1e-5``, ~10 ppm):

    * ``max|ratio - 1| <= tol``: no change; new rows appended as fetched.
    * ``(max(ratio) - min(ratio)) / mean(ratio) <= tol`` and at least
      ``min_overlap`` shared bars: pure restatement; the *new* rows' OHLC are
      multiplied by ``mean(ratio)``. Volume is left as reported.
    * otherwise (including no overlap, or a non-identical overlap of fewer
      than ``min_overlap`` bars, which cannot show the ratio is constant):
      raises :class:`DataRevisionError`; the caller quarantines the fetch.
    """
    if cached.empty:
        raise ValueError("cached frame is empty; nothing to reconcile against")
    last = cached.index.max()
    shared = cached.index.intersection(fetched.index)
    if len(shared) == 0:
        raise DataRevisionError("fetch has no overlap with the cached tail; cannot verify basis")
    new_close = fetched.loc[shared, "close"].to_numpy(dtype="float64")
    if not np.all(np.isfinite(new_close)) or np.any(new_close <= 0):
        raise DataRevisionError("fetched overlap contains non-positive or NaN closes")
    ratios = cached.loc[shared, "close"].to_numpy(dtype="float64") / new_close
    mean = float(ratios.mean())
    new_rows = fetched.loc[fetched.index > last].copy()

    if float(np.max(np.abs(ratios - 1.0))) <= tol:
        return ReconcileResult("no_change", 1.0, len(shared), new_rows)
    if len(shared) < min_overlap:
        raise DataRevisionError(
            f"overlap of {len(shared)} bar(s) is below the minimum {min_overlap} needed to "
            f"classify a restatement (ratio {mean:.8f}): treated as a revision"
        )
    if float(ratios.max() - ratios.min()) / mean <= tol:
        for col in PRICE_COLUMNS:
            new_rows[col] = new_rows[col] * mean
        return ReconcileResult("restatement", mean, len(shared), new_rows)
    raise DataRevisionError(
        f"overlap ratios vary (min {ratios.min():.8f}, max {ratios.max():.8f}) "
        f"over {len(shared)} bars: provider revised history"
    )
