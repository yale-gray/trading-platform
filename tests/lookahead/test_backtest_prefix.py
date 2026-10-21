"""Backtest-level prefix invariance (SPEC-005 section 3, T16).

Run each golden config on ``bars[:T]`` and on all bars. Every ledger event dated <= the last bar
of the short run, and the equity on those dates, must be identical; the short run's final-bar
signal must produce no fill (no bar t+1 exists). Cut dates: a mid-year Thursday and a Sunday
(equities' last bar is Friday, crypto trades the weekend), so month and week boundaries are
crossed.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from golden import harness as h

from tradebot.backtest.engine import run_backtest
from tradebot.portfolio.models import EventType

pytestmark = [pytest.mark.slow, pytest.mark.timeout(120)]

CUTS = {
    "G1": date(2022, 6, 30),
    "G2": date(2022, 6, 30),
    "G3": date(2022, 8, 14),
    "G4": date(2022, 8, 14),
}


def _truncate(bars: dict[str, pd.DataFrame], last: date) -> dict[str, pd.DataFrame]:
    lim = pd.Timestamp(last, tz="UTC")
    return {k: v.loc[v.index <= lim] for k, v in bars.items()}


@pytest.mark.parametrize("gid", h.IDS)
def test_backtest_prefix_invariance(gid: str) -> None:
    bars = h.load_bars()
    full = h.run_golden(gid).report.result
    cut = CUTS[gid]
    short_cfg = h.golden_config(gid).model_copy(update={"end": cut})
    short = run_backtest(short_cfg, _truncate(bars, cut))
    last = short.equity.index[-1].date()
    assert last <= cut

    expected = [ev for ev in full.ledger if ev.asof <= last]
    assert len(short.ledger) == len(expected) > 0
    for a, b in zip(short.ledger, expected, strict=True):
        assert a == b, f"ledger diverges at seq {a.seq}"

    pd.testing.assert_series_equal(
        short.equity, full.equity.loc[full.equity.index <= short.equity.index[-1]], check_exact=True
    )
    assert short.config_hash != full.config_hash  # different end date, same strategies

    fills = [ev for ev in short.ledger if ev.type is EventType.FILL]
    assert fills
    assert max(ev.asof for ev in fills) <= last
    # orders the last bar's signals created cannot have filled: nothing exists at t+1
    full_after = [ev for ev in full.ledger if ev.type is EventType.FILL and ev.asof > last]
    assert full_after, "cut chosen so that the full run trades after it"
