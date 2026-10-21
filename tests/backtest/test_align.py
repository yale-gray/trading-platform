"""``engine._align``: carry a benchmark series onto the fund's index (audit 002 F10)."""

from __future__ import annotations

import pandas as pd

from tradebot.backtest.engine import _align


def test_align_forward_fills_and_uses_fill_value_before_the_first_point() -> None:
    idx = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    late = pd.Series([50.0, 60.0], index=idx[2:4])  # starts at the 3rd fund bar, ends before last
    out = _align(late, idx, 100.0)
    # before the first point: the fill (initial capital), never 0; after the last: carried forward
    assert out.tolist() == [100.0, 100.0, 50.0, 60.0, 60.0]
    assert _align(late, idx, 0.0).tolist() == [0.0, 0.0, 50.0, 60.0, 60.0]
