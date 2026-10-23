"""Audit 007 fixes at the driver/engine level: flatten cancels orders (m1), decision lag (A22)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
from support.intraday_synth import frame_from_closes, multi_session_index
from test_intraday_driver import _cfg, _run_driver, hold_spec

from tradebot.backtest.driver import BarDriver, build_engine_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType, FillSide
from tradebot.sim.orders import OrderStatus


def test_a_buy_still_pending_at_the_flatten_is_cancelled_and_never_fills_next_morning() -> None:
    """BBB's only Monday bar is 15:30 ET (20:30 UTC): its BUY, submitted there, cannot fill on the
    last bar (no bar) and would fill on Tuesday's first bar unless the flatten cancels it."""
    idx = multi_session_index(date(2024, 1, 5), date(2024, 1, 9))
    aaa = frame_from_closes(idx, [100.0] * len(idx))
    keep = (idx == pd.Timestamp("2024-01-08T20:30:00Z")) | (
        idx >= pd.Timestamp("2024-01-09", tz="UTC")
    )
    bbb_idx = idx[keep]
    bbb = frame_from_closes(bbb_idx, [20.0] * len(bbb_idx))
    cfg = _cfg(hold_spec(["AAA", "BBB"]), date(2024, 1, 8), date(2024, 1, 9))
    engine, driver, steps = _run_driver(cfg, {"AAA": aaa, "BBB": bbb})

    monday_end = next(s for s in steps if s.ts == pd.Timestamp("2024-01-08T20:45:00Z"))
    assert driver.is_session_end(monday_end.ts)
    cancelled = [o for o in monday_end.bar_result.cancelled if o.symbol == "BBB"]
    assert cancelled and all(o.status is OrderStatus.CANCELLED for o in cancelled)
    buys = [
        e
        for e in engine.ledger
        if e.type is EventType.FILL and e.symbol == "BBB" and e.side is FillSide.BUY
    ]
    # Every BBB BUY that filled was decided on Tuesday; none carried over from Monday.
    assert buys and all(e.asof >= datetime.fromisoformat("2024-01-09T14:30:00+00:00") for e in buys)
    first = min(buys, key=lambda e: e.seq)
    order = next(o for o in engine.orders() if o.order_id == first.order_id)
    assert order.created_asof >= datetime.fromisoformat("2024-01-09T14:30:00+00:00")


def test_decision_lag_delays_the_fill_by_that_many_bars_and_never_crosses_the_session() -> None:
    idx = multi_session_index(date(2024, 1, 5), date(2024, 1, 9))
    bars = {"AAA": frame_from_closes(idx, [100.0] * len(idx))}
    firsts = {}
    for lag in (0, 1, 2):
        cfg = _cfg(hold_spec(["AAA"]), date(2024, 1, 8), date(2024, 1, 9))
        engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
        driver = BarDriver(engine, cfg.sleeves, bars, start=cfg.start, decision_lag_bars=lag)
        list(driver.run(end=cfg.end))
        buys = [e for e in engine.ledger if e.type is EventType.FILL and e.side is FillSide.BUY]
        firsts[lag] = buys[0].asof
        # Tuesday's first BUY is `lag` bars after the usual one: no stale Monday decision.
        expect = pd.Timestamp("2024-01-09T14:45:00Z") + pd.Timedelta(minutes=15 * lag)
        assert pd.Timestamp(buys[1].asof) == expect
    assert firsts[0].isoformat() == "2024-01-08T14:45:00+00:00"  # t+1 (the old behaviour)
    assert firsts[1].isoformat() == "2024-01-08T15:00:00+00:00"  # t+2
    assert firsts[2].isoformat() == "2024-01-08T15:15:00+00:00"  # t+3


def test_the_default_lag_is_two_bars_for_15m() -> None:
    idx = multi_session_index(date(2024, 1, 5), date(2024, 1, 9))
    bars = {"AAA": frame_from_closes(idx, [100.0] * len(idx))}
    cfg = _cfg(hold_spec(["AAA"]), date(2024, 1, 8), date(2024, 1, 9))
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
    assert (
        BarDriver(engine, cfg.sleeves, bars, start=cfg.start)._lag == 2
    )  # A22: SIP arrives after bar t+2 opens
