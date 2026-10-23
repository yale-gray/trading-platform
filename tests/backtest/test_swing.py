"""Swing account (ADR-0001 A27): 15m bars, ``intraday_flatten=False``, positions held overnight."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from support.intraday_synth import frame_from_closes, multi_session_index
from test_intraday_driver import _cfg, _run_driver, hold_spec

from tradebot.portfolio.models import EventType, FillSide
from tradebot.strategy.schema import RulesSpec

pytestmark = pytest.mark.unit

MON_OPEN = "2024-01-08T14:30:00+00:00"
TUE_OPEN = "2024-01-09T14:30:00+00:00"


def _bars() -> dict[str, pd.DataFrame]:
    """AAA over Fri Jan 5 .. Tue Jan 9 2024; the open of every bar is its previous close."""
    idx = multi_session_index(date(2024, 1, 5), date(2024, 1, 9))
    closes = [100.0 + 0.01 * k for k in range(len(idx))]
    return {"AAA": frame_from_closes(idx, closes)}


def _clock_spec(entry: dict) -> RulesSpec:
    return RulesSpec.model_validate(
        {
            "id": "swing_clock",
            "name": "swing",
            "asset_class": "equity",
            "timeframe": "15m",
            "universe": ["AAA"],
            "schedule": {"freq": "bar"},
            "entry": entry,
        }
    )


def test_a_swing_account_holds_overnight_and_a_flattening_one_does_not() -> None:
    # Entry is true on bars 20..25 of each session (hold-while), then false on bar 0.
    spec = _clock_spec({"ge": [{"session": "bar_index"}, 20]})
    held = {}
    for flatten in (False, True):
        cfg = _cfg(spec, date(2024, 1, 8), date(2024, 1, 9), flatten=flatten)
        cfg = cfg.model_copy(update={"decision_lag_bars": None})  # clock-only default
        engine, driver, steps = _run_driver(cfg, _bars())
        last_mon = next(s for s in steps if s.ts.isoformat() == "2024-01-08T20:45:00+00:00")
        held[flatten] = len(last_mon.bar_result.snapshot.sleeves[0].positions)
        engine.verify()
    assert held == {False: 1, True: 0}


def test_swing_decision_on_the_last_bar_fills_at_the_next_sessions_first_open() -> None:
    # Entry is true only on bar 25 (the session's last bar) and false on bar 0 and everywhere else.
    spec = _clock_spec({"eq": [{"session": "bar_index"}, 25]})
    cfg = _cfg(spec, date(2024, 1, 8), date(2024, 1, 9), flatten=False)
    cfg = cfg.model_copy(update={"decision_lag_bars": None})
    engine, driver, steps = _run_driver(cfg, _bars())
    last_mon = next(s for s in steps if s.ts.isoformat() == "2024-01-08T20:45:00+00:00")
    assert last_mon.signals and last_mon.targets  # the swing account decides on the last bar
    fills = [e for e in engine.ledger if e.type is EventType.FILL]
    first = fills[0]
    assert first.side is FillSide.BUY and first.asof.isoformat() == TUE_OPEN
    # Bought at Tuesday's first open = Monday's last close (open[t] = close[t-1] in the fixture).
    monday_last_close = float(_bars()["AAA"].loc["2024-01-08T20:45:00Z", "close"])
    assert float(first.price) == pytest.approx(monday_last_close)
    # Tuesday bar 0 decides "flat" (entry false), so the SELL fills at bar 1's open.
    assert fills[1].side is FillSide.SELL
    assert fills[1].asof.isoformat() == "2024-01-09T14:45:00+00:00"


def test_swing_data_rules_may_decide_on_an_earlier_session_bar() -> None:
    # Always-entry has no clock operand, so the normal lag 2 applies; Tuesday's first two steps
    # evaluate Monday's last two bars in a swing account, but not in a flattening one.
    seen = {}
    for flatten in (False, True):
        cfg = _cfg(hold_spec(["AAA"]), date(2024, 1, 8), date(2024, 1, 9), flatten=flatten)
        cfg = cfg.model_copy(update={"decision_lag_bars": None})
        _, _, steps = _run_driver(cfg, _bars())
        tue0 = next(s for s in steps if s.ts.isoformat() == TUE_OPEN)
        seen[flatten] = tue0.signals["day"].asof.isoformat() if tue0.signals else None
    assert seen[False] == "2024-01-08T20:30:00+00:00"
    assert seen[True] is None
