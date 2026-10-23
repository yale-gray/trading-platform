"""Scenario tests for ``PortfolioEngine`` (SPEC-003 AC 3-6 and 11; T13).

Each scenario drives the engine through hand-built bars from ``scenario_bars``.
Expected numbers are worked out by hand in the comments and are not copied from
the code's output. Costs come from ``default_config()``:

* equity: fee 0 bps, slippage 5 bps. BUY at open 100 fills at ceil8(100 x 1.0005)
  = 100.05; SELL at open 100 fills at floor8(100 x 0.9995) = 99.95.
* crypto: fee 10 bps, slippage 10 bps. BUY at open 100 fills at ceil8(100 x 1.001)
  = 100.10; fee = q2_half_up(notional x 0.001).
* R5 cash reservation per unit at ref 100: equity 100.05, crypto 100.20.

Sleeve equity used for sizing is the sleeve's cash plus marks at the last close.
Limit checks (R3/R4) use reference (close) prices, so slippage is not counted in
the limits (SPEC-003 section 6, entry-time checks).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from scenario_bars import bar, book

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import EngineConfig, default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType, PortfolioSnapshot, PositionSnapshot
from tradebot.portfolio.state import EngineState
from tradebot.sim.broker import Bar
from tradebot.sim.orders import OrderOrigin, OrderReason, OrderStatus, Side

pytestmark = pytest.mark.integration

CORE = "core_momentum"  # 50% of capital, equity, no stop
MR = "mean_reversion"  # 20%, equity (crypto only in the custom config below)
CRYPTO = "crypto_trend"  # 20%, crypto only
CASH = "cash"  # 10%, no strategy

SPY = "SPY"
BTC = "BTC/USD"
ETH = "ETH/USD"

# 2024 weekdays unless a scenario needs a weekend (Jan 1 2024 was a Monday).
JAN2 = date(2024, 1, 2)  # Tue
JAN3 = date(2024, 1, 3)  # Wed
JAN4 = date(2024, 1, 4)  # Thu
JAN5 = date(2024, 1, 5)  # Fri
JAN6 = date(2024, 1, 6)  # Sat
JAN7 = date(2024, 1, 7)  # Sun
JAN8 = date(2024, 1, 8)  # Mon
JAN9 = date(2024, 1, 9)  # Tue
JAN31 = date(2024, 1, 31)  # Wed
FEB1 = date(2024, 2, 1)  # Thu, first bar of February
FEB2 = date(2024, 2, 2)
FEB3 = date(2024, 2, 3)


def make_config(*, mr_stop_mult: str | None = None, mr_allows_crypto: bool = False) -> EngineConfig:
    """``default_config()`` with optional overrides on the mean-reversion sleeve only."""
    base = default_config()
    sleeves = []
    for sc in base.sleeves:
        if sc.sleeve_id == MR:
            upd: dict[str, object] = {}
            if mr_stop_mult is not None:
                upd["trailing_stop_atr_mult"] = Decimal(mr_stop_mult)
            if mr_allows_crypto:
                upd["allowed_asset_classes"] = frozenset({AssetClass.EQUITY, AssetClass.CRYPTO})
            sc = sc.model_copy(update=upd)
        sleeves.append(sc)
    return EngineConfig(
        initial_capital=base.initial_capital,
        sleeves=tuple(sleeves),
        risk=base.risk,
        costs=base.costs,
        equity_qty_quantum=Decimal("1"),  # hand-computed whole-share scenarios
    )


def sleeve_positions(snap: PortfolioSnapshot, sleeve_id: str) -> list[PositionSnapshot]:
    return next(s for s in snap.sleeves if s.sleeve_id == sleeve_id).positions


def position(snap: PortfolioSnapshot, sleeve_id: str, symbol: str) -> PositionSnapshot | None:
    return next((p for p in sleeve_positions(snap, sleeve_id) if p.symbol == symbol), None)


def ledger_json(engine: PortfolioEngine) -> list[str]:
    return [e.model_dump_json() for e in engine.ledger]


def order_status(engine: PortfolioEngine, order_id: str) -> OrderStatus:
    return next(o.status for o in engine.export_state().orders if o.order_id == order_id)


# --------------------------------------------------------------------------- scenario 1


def test_cross_sleeve_spy_cap_shrinks_second_sleeve() -> None:
    """AC 3: SPY targeted by two sleeves on the same bar; the second is shrunk.

    Reference equity at submission is 100000 (deposits only), so the 25% asset cap
    is 25000 of SPY across all sleeves.
    * core: 0.30 x 50000 = 15000 -> 150 sh at ref 100. Room 25000 >= 15000: accepted.
    * mean reversion: 0.80 x 20000 = 16000 -> 160 sh. Room = 25000 - 15000 pending
      = 10000 -> 100 sh, MAX_ASSET_SHRUNK (requested 160, accepted 100).
    Total pending SPY notional = 25000 = 25% of 100000.
    """
    eng = PortfolioEngine(make_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))

    core = eng.submit_target_weights(CORE, {SPY: 0.30}, JAN2)
    mr = eng.submit_target_weights(MR, {SPY: 0.80}, JAN2)

    assert [o.qty_requested for o in core.orders] == [Decimal(150)]
    assert core.rejections == []
    assert [o.qty_requested for o in mr.orders] == [Decimal(100)]
    assert [(r.symbol, r.reason, r.requested, r.accepted) for r in mr.rejections] == [
        (SPY, OrderReason.MAX_ASSET_SHRUNK, Decimal(160), Decimal(100))
    ]
    total_spy_notional = sum(o.qty_requested for o in core.orders + mr.orders) * Decimal(100)
    assert total_spy_notional <= Decimal("0.25") * Decimal(100000)

    # Both fill at the next open at 100.05. Equity then sits at 99987.50 (slippage),
    # so SPY is 25000 / 99987.50 = 25.003%: the cap was enforced at entry, not after.
    filled = eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0)))
    assert len(filled.fills) == 2
    assert {o.fill_price for o in filled.fills} == {Decimal("100.05000000")}
    snap = eng.snapshot()
    assert snap.equity == Decimal("99987.50")
    assert snap.exposures[SPY] <= Decimal("0.2501")
    eng.verify()


# --------------------------------------------------------------------------- scenario 2


def test_crypto_30_percent_cap_applies_across_sleeves() -> None:
    """AC 3: crypto cap of 30% of equity (30000) across crypto_trend and mean reversion.

    Custom config lets mean reversion hold crypto too (its default is equity only).
    Both crypto symbols have vol20 0.02, so the inverse-vol tilt is an even split.
    * crypto_trend {BTC 0.4, ETH 0.4}: total weight 0.8 kept; each 0.4 x 20000 = 8000
      -> 80 units at ref 100. Cash: 80 x 100.20 = 8016 <= 20000; the second symbol
      sees 20000 - 8016 = 11984 >= 8016.
    * mean reversion {ETH 1.0}: target 20000 -> 200 units.
      R3 (25% per asset): ETH room = 25000 - 8000 pending = 17000 -> 170 (MAX_ASSET_SHRUNK).
      R4 (30% crypto): room = 30000 - (8000 + 8000) = 14000 -> 140 (MAX_CRYPTO_SHRUNK).
      R5: 140 x 100.20 = 14028 <= 20000 ok.
    Total crypto notional = 8000 + 8000 + 14000 = 30000 = 30% of 100000.
    """
    eng = PortfolioEngine(make_config(mr_allows_crypto=True))
    crypto_bars = book(bar(BTC, 100.0, 100.0, vol20=0.02), bar(ETH, 100.0, 100.0, vol20=0.02))
    eng.process_bar(JAN2, crypto_bars)

    cta = eng.submit_target_weights(CRYPTO, {BTC: 0.4, ETH: 0.4}, JAN2)
    mr = eng.submit_target_weights(MR, {ETH: 1.0}, JAN2)

    assert [(o.symbol, o.qty_requested) for o in cta.orders] == [
        (BTC, Decimal(80)),
        (ETH, Decimal(80)),
    ]
    assert cta.rejections == []
    assert [(o.symbol, o.qty_requested) for o in mr.orders] == [(ETH, Decimal(140))]
    assert [(r.symbol, r.reason, r.requested, r.accepted) for r in mr.rejections] == [
        (ETH, OrderReason.MAX_ASSET_SHRUNK, Decimal(200), Decimal(170)),
        (ETH, OrderReason.MAX_CRYPTO_SHRUNK, Decimal(200), Decimal(140)),
    ]
    crypto_notional = sum(o.qty_requested for o in cta.orders + mr.orders) * Decimal(100)
    assert crypto_notional == Decimal("30000")
    assert crypto_notional <= Decimal("0.30") * Decimal(100000)

    filled = eng.process_bar(JAN3, crypto_bars)
    assert len(filled.fills) == 3
    assert {o.fill_price for o in filled.fills} == {Decimal("100.10000000")}
    eng.verify()


# --------------------------------------------------------------------------- scenario 3


def test_stop_out_fills_next_open_then_lock_until_flat_then_rearms() -> None:
    """AC 4: close-based ATR trailing stop, one STOP SELL, lock, and re-arm.

    Mean reversion holds SPY with a 2.5 x ATR14 trailing stop; ATR14 is 2.0 on every bar.
    * JAN2: target 0.9 x 20000 = 18000 -> 180 sh (R5: 180 x 100.05 = 18009 <= 20000).
    * JAN3: fill 180 at 100.05 (notional 18009.00, mr cash 1991.00). High water 100,
      stop = 100 - 2.5 x 2 = 95.00. Close 100 > 95: no trigger.
    * JAN4: close 95.00 <= stop 95.00 (equality triggers): one STOP SELL of 180, which
      fills at the next open. Strategy targets SPY while the STOP is pending: STOP_LOCK.
    * JAN5: open 94 fills the STOP at floor8(94 x 0.9995) = 93.95300000; proceeds
      180 x 93.953 = 16911.54, so mr cash = 1991.00 + 16911.54 = 18902.54. No new stop.
      STOP_LOCK still applies to a BUY. Target 0 re-arms.
    * JAN8: target 0.9 x 18902.54 = 17012.286 -> 170 sh at ref 100. Accepted.
    """
    eng = PortfolioEngine(make_config(mr_stop_mult="2.5"))

    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0, atr14=2.0)))
    entry = eng.submit_target_weights(MR, {SPY: 0.9}, JAN2)
    assert [o.qty_requested for o in entry.orders] == [Decimal(180)]

    r3 = eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0, atr14=2.0)))
    assert [o.fill_price for o in r3.fills] == [Decimal("100.05000000")]
    assert r3.stop_orders == []  # stop 95 is not touched by a close of 100

    r4 = eng.process_bar(JAN4, book(bar(SPY, 100.0, 95.0, atr14=2.0)))
    assert len(r4.stop_orders) == 1
    stop = r4.stop_orders[0]
    assert (stop.origin, stop.side, stop.qty_requested, stop.status) == (
        OrderOrigin.STOP,
        Side.SELL,
        Decimal(180),
        OrderStatus.PENDING,
    )
    assert r4.fills == []
    assert [e.type for e in r4.new_ledger_events] == [EventType.STOP_TRIGGERED]

    locked = eng.submit_target_weights(MR, {SPY: 0.9}, JAN4)
    assert locked.orders == []
    assert [r.reason for r in locked.rejections] == [OrderReason.STOP_LOCK]

    r5 = eng.process_bar(JAN5, book(bar(SPY, 94.0, 94.0, atr14=2.0)))
    assert [o.order_id for o in r5.fills] == [stop.order_id]
    assert r5.fills[0].fill_price == Decimal("93.95300000")
    assert r5.fills[0].origin is OrderOrigin.STOP
    assert r5.stop_orders == []  # flat now: no second STOP
    assert position(eng.snapshot(), MR, SPY) is None

    still_locked = eng.submit_target_weights(MR, {SPY: 0.9}, JAN5)
    assert [r.reason for r in still_locked.rejections] == [OrderReason.STOP_LOCK]

    eng.submit_target_weights(MR, {}, JAN5)  # target 0 once: re-arm

    eng.process_bar(JAN8, book(bar(SPY, 100.0, 100.0, atr14=2.0)))
    reentry = eng.submit_target_weights(MR, {SPY: 0.9}, JAN8)
    assert reentry.rejections == []
    assert [(o.side, o.origin, o.qty_requested) for o in reentry.orders] == [
        (Side.BUY, OrderOrigin.STRATEGY, Decimal(170))
    ]

    stop_orders_seen = [o for r in (r3, r4, r5) for o in r.stop_orders]
    assert len(stop_orders_seen) == 1  # exactly one STOP SELL across the whole episode
    eng.verify()


# --------------------------------------------------------------------------- scenario 4


def test_kill_switch_blocks_buys_allows_exits_cancels_pending_and_freezes_rebalance() -> None:
    """AC 5: 20% drawdown trips the switch; BUYs blocked; exits fill; reset restores buying.

    * JAN2: core target 0.5 x 50000 = 25000 -> 250 SPY at 100 (R3: 25000 is not above room).
    * JAN3: fill 250 at 100.05 = 25012.50. Core cash 24987.50. Equity 99987.50 (peak 100000).
      crypto_trend targets BTC 0.9 x 20000 = 18000 -> 180 BTC pending. BTC has no bar on
      JAN4, so the order waits.
    * JAN4: SPY closes at 20. Equity = 74987.50 cash + 250 x 20 = 79987.50.
      Drawdown = 1 - 79987.50/100000 = 0.200125 >= 0.20: trips. Pending BTC BUY is
      cancelled. Core full exit (SELL 250) and mean-reversion BUY (500 at 20, blocked).
    * FEB1 (first bar of a new month, still tripped): no rebalance plan.
      Sell 250 at floor8(20 x 0.9995) = 19.99 -> 4997.50. Equity = 79985.00 (cash only).
    * Reset: peak = last equity 79985.00. Mean reversion BUY 500 at 20 = 10000 now fits.
    """
    eng = PortfolioEngine(make_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    assert [o.qty_requested for o in eng.submit_target_weights(CORE, {SPY: 0.5}, JAN2).orders] == [
        Decimal(250)
    ]

    eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0), bar(BTC, 100.0, 100.0, vol20=0.02)))
    assert eng.snapshot().equity == Decimal("99987.50")
    btc_buy = eng.submit_target_weights(CRYPTO, {BTC: 0.9}, JAN3)
    assert [o.qty_requested for o in btc_buy.orders] == [Decimal(180)]
    pending_btc_id = btc_buy.orders[0].order_id

    r4 = eng.process_bar(JAN4, book(bar(SPY, 20.0, 20.0)))
    assert r4.kill_switch_tripped is True
    assert [o.order_id for o in r4.cancelled] == [pending_btc_id]
    assert r4.cancelled[0].status is OrderStatus.CANCELLED
    assert r4.fills == []
    assert r4.snapshot.killed is True
    assert r4.snapshot.drawdown >= Decimal("0.20")
    assert r4.snapshot.equity == Decimal("79987.50")
    assert EventType.KILL_SWITCH_TRIPPED in [e.type for e in r4.new_ledger_events]

    # BUYs are rejected with KILL_SWITCH while tripped.
    blocked = eng.submit_target_weights(MR, {SPY: 0.5}, JAN4)
    assert blocked.orders == []
    assert [r.reason for r in blocked.rejections] == [OrderReason.KILL_SWITCH]

    # Exits still work: a full exit of the core SPY position is accepted.
    exit_result = eng.submit_target_weights(CORE, {}, JAN4)
    assert [(o.side, o.qty_requested) for o in exit_result.orders] == [(Side.SELL, Decimal(250))]
    assert exit_result.rejections == []

    # FEB1 is a new month, so step F would normally plan a rebalance. Tripped: no plan.
    r_feb = eng.process_bar(FEB1, book(bar(SPY, 20.0, 20.0), bar(BTC, 100.0, 100.0, vol20=0.02)))
    assert [o.fill_price for o in r_feb.fills] == [Decimal("19.99000000")]
    assert r_feb.fills[0].symbol == SPY  # the BTC order was cancelled, nothing fills
    assert r_feb.rebalance_planned is False
    assert EventType.REBALANCE_PLANNED not in [e.type for e in r_feb.new_ledger_events]
    assert r_feb.snapshot.equity == Decimal("79985.00")
    assert eng.export_state().rebalance_plan is None

    eng.reset_kill_switch("tester", "scenario 4", FEB1)
    after_reset = eng.snapshot()
    assert after_reset.killed is False
    assert after_reset.peak_equity == Decimal("79985.00")

    # Buying works again: 0.5 x 20000 = 10000 -> 500 SPY at ref 20, accepted in full.
    resumed = eng.submit_target_weights(MR, {SPY: 0.5}, FEB1)
    assert resumed.rejections == []
    assert [(o.side, o.qty_requested) for o in resumed.orders] == [(Side.BUY, Decimal(500))]
    eng.verify()


# --------------------------------------------------------------------------- scenario 5


def test_month_boundary_rebalance_moves_sleeves_to_targets_with_cash_only() -> None:
    """AC 6: monthly rebalance back to 50/20/20/10 using cash transfers and strategy sells.

    * JAN2 / JAN3: core buys 250 SPY at 100.05 = 25012.50 (core cash 24987.50).
    * JAN31: SPY closes at 120, so core is 24987.50 + 250 x 120 = 54987.50.
      Other sleeves: 20000, 20000, 10000. Equity = 104987.50.
    * FEB1 (first bar of February): plan with targets 52493.75 / 20997.50 / 20997.50 /
      10498.75. Surplus: core +2493.75, mr -997.50, crypto -997.50, cash -498.75.
      Core submits 0.5 x 52493.75 = 26246.875 -> 218 sh, so it sells 32 (3840 >= min).
    * FEB2: the 32 SELL fills at floor8(120 x 0.9995) = 119.94 -> 3838.08. Core cash is
      28825.58. Step C moves 2493.75 from core to the three underweight sleeves
      (paired OUT/IN events, net zero). Plan completes.
    * After: core 26331.83 + 218 x 120 = 52491.83; mr 20997.50; crypto 20997.50;
      cash 10498.75. Total 104985.58 (the 1.92 difference is the sell slippage).
    """
    eng = PortfolioEngine(make_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    eng.submit_target_weights(CORE, {SPY: 0.5}, JAN2)
    eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0)))
    assert position(eng.snapshot(), CORE, SPY).qty == Decimal(250)  # type: ignore[union-attr]

    eng.process_bar(JAN31, book(bar(SPY, 120.0, 120.0)))
    assert eng.snapshot().equity == Decimal("104987.50")

    feb1 = eng.process_bar(FEB1, book(bar(SPY, 120.0, 120.0)))
    assert feb1.rebalance_planned is True
    assert EventType.REBALANCE_PLANNED in [e.type for e in feb1.new_ledger_events]

    core_sell = eng.submit_target_weights(CORE, {SPY: 0.5}, FEB1)
    assert [(o.side, o.qty_requested) for o in core_sell.orders] == [(Side.SELL, Decimal(32))]
    for sid in (MR, CRYPTO, CASH):
        assert eng.submit_target_weights(sid, {}, FEB1).orders == []

    feb2 = eng.process_bar(FEB2, book(bar(SPY, 120.0, 120.0)))
    assert [(o.side, o.fill_price) for o in feb2.fills] == [(Side.SELL, Decimal("119.94000000"))]

    outs = [e for e in feb2.new_ledger_events if e.type is EventType.CASH_TRANSFER_OUT]
    ins = [e for e in feb2.new_ledger_events if e.type is EventType.CASH_TRANSFER_IN]
    assert sum(e.amount for e in outs) == Decimal("2493.75")
    assert sum(e.amount for e in ins) == Decimal("2493.75")  # transfers net to zero
    assert {e.sleeve_id for e in outs} == {CORE}
    assert {e.sleeve_id for e in ins} == {MR, CRYPTO, CASH}
    assert eng.export_state().rebalance_plan is None

    snap = eng.snapshot()
    by_id = {s.sleeve_id: s for s in snap.sleeves}
    assert by_id[MR].equity == Decimal("20997.50")
    assert by_id[CRYPTO].equity == Decimal("20997.50")
    assert by_id[CASH].equity == Decimal("10498.75")
    assert by_id[CORE].equity == Decimal("52491.83")
    for sid, target in ((CORE, "0.5"), (MR, "0.2"), (CRYPTO, "0.2"), (CASH, "0.1")):
        assert abs(by_id[sid].weight - Decimal(target)) <= Decimal("0.001")
    # Transfers never move positions: core still holds 218 SPY.
    assert position(snap, CORE, SPY).qty == Decimal(218)  # type: ignore[union-attr]
    eng.verify()


# --------------------------------------------------------------------------- scenario 6


def test_friday_orders_fill_crypto_on_saturday_and_etf_on_monday() -> None:
    """Mixed calendars (ADR D6): the engine clock is every date with any bar.

    * FRI JAN5: core targets SPY 0.3 x 50000 = 15000 -> 150 sh; crypto_trend targets
      BTC 0.9 x 20000 = 18000 -> 180 BTC. Both orders are pending.
    * SAT JAN6: only BTC has a bar (open 100). The BTC BUY fills at ceil8(100 x 1.001)
      = 100.10, notional 180 x 100.10 = 18018.00, fee q2_half_up(18.018) = 18.02.
      The SPY order has no bar: it waits (count 1).
    * SUN JAN7: only BTC has a bar. Nothing to fill; SPY waits (count 2).
    * MON JAN8: SPY opens at 100, so the SPY BUY fills at 100.05 with fill_asof JAN8.
    """
    eng = PortfolioEngine(make_config())
    fri = book(bar(SPY, 100.0, 100.0), bar(BTC, 100.0, 100.0, vol20=0.02))
    eng.process_bar(JAN5, fri)
    spy_order = eng.submit_target_weights(CORE, {SPY: 0.3}, JAN5).orders[0]
    btc_order = eng.submit_target_weights(CRYPTO, {BTC: 0.9}, JAN5).orders[0]
    assert (spy_order.qty_requested, btc_order.qty_requested) == (Decimal(150), Decimal(180))

    sat = eng.process_bar(JAN6, book(bar(BTC, 100.0, 100.0, vol20=0.02)))
    assert [o.order_id for o in sat.fills] == [btc_order.order_id]
    filled_btc = sat.fills[0]
    assert filled_btc.fill_asof == JAN6
    assert filled_btc.fill_price == Decimal("100.10000000")
    assert filled_btc.fee == Decimal("18.02")
    assert order_status(eng, spy_order.order_id) is OrderStatus.PENDING

    sun = eng.process_bar(JAN7, book(bar(BTC, 100.0, 100.0, vol20=0.02)))
    assert sun.fills == []
    assert order_status(eng, spy_order.order_id) is OrderStatus.PENDING

    mon = eng.process_bar(JAN8, book(bar(SPY, 100.0, 100.0)))
    assert [o.order_id for o in mon.fills] == [spy_order.order_id]
    assert mon.fills[0].fill_asof == JAN8
    assert mon.fills[0].fill_price == Decimal("100.05000000")
    eng.verify()


# --------------------------------------------------------------------------- scenario 7


def test_entry_asof_follows_add_trim_full_exit_and_reentry() -> None:
    """I18 / AC 11: entry_asof is the fill date that took the position from flat.

    * JAN2: core targets 0.2 x 50000 = 10000 -> 100 SPY (fills JAN3).
    * JAN3: fill 100 at 100.05. entry_asof = JAN3. Sleeve equity = 39995 + 10000 = 49995.
      Add: 0.3 x 49995 = 14998.50 -> 149 sh, so BUY 49 (fills JAN4). The fill is 4902.45.
    * JAN4: add fills, so 149 sh. entry_asof still JAN3. Trim: 0.1 x 49992.55 = 4999.255
      -> 49 sh, so SELL 100 (fills JAN5).
    * JAN5: trim fills at 99.95, leaving 49 sh. entry_asof still JAN3. Full exit: SELL 49.
    * JAN8: exit fills at 99.95. Flat: no SPY position, so entry_asof is None.
      Cash = 49985.10. Re-entry: 0.2 x 49985.10 = 9997.02 -> 99 sh (fills JAN9).
    * JAN9: re-entry fills. entry_asof = JAN9 (a new episode).
    """
    eng = PortfolioEngine(make_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    assert [o.qty_requested for o in eng.submit_target_weights(CORE, {SPY: 0.2}, JAN2).orders] == [
        Decimal(100)
    ]

    eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0)))
    assert position(eng.snapshot(), CORE, SPY).entry_asof == JAN3  # type: ignore[union-attr]
    assert [o.qty_requested for o in eng.submit_target_weights(CORE, {SPY: 0.3}, JAN3).orders] == [
        Decimal(49)
    ]

    eng.process_bar(JAN4, book(bar(SPY, 100.0, 100.0)))
    add = position(eng.snapshot(), CORE, SPY)
    assert (add.qty, add.entry_asof) == (Decimal(149), JAN3)  # type: ignore[union-attr]
    trim = eng.submit_target_weights(CORE, {SPY: 0.1}, JAN4)
    assert [(o.side, o.qty_requested) for o in trim.orders] == [(Side.SELL, Decimal(100))]

    eng.process_bar(JAN5, book(bar(SPY, 100.0, 100.0)))
    after_trim = position(eng.snapshot(), CORE, SPY)
    assert (after_trim.qty, after_trim.entry_asof) == (Decimal(49), JAN3)  # type: ignore[union-attr]
    exit_all = eng.submit_target_weights(CORE, {}, JAN5)
    assert [(o.side, o.qty_requested) for o in exit_all.orders] == [(Side.SELL, Decimal(49))]

    eng.process_bar(JAN8, book(bar(SPY, 100.0, 100.0)))
    assert position(eng.snapshot(), CORE, SPY) is None
    assert eng.export_state().positions[CORE] == []
    reentry = eng.submit_target_weights(CORE, {SPY: 0.2}, JAN8)
    assert [(o.side, o.qty_requested) for o in reentry.orders] == [(Side.BUY, Decimal(99))]

    eng.process_bar(JAN9, book(bar(SPY, 100.0, 100.0)))
    fresh = position(eng.snapshot(), CORE, SPY)
    assert (fresh.qty, fresh.entry_asof) == (Decimal(99), JAN9)  # type: ignore[union-attr]
    eng.verify()


# --------------------------------------------------------------------------- scenario 8


def _script() -> list[tuple[date, dict[str, Bar], dict[str, dict[str, float]]]]:
    """A deterministic sequence touching stops, locks, a month rebalance and transfers.

    Each step is (asof, bars, {sleeve: target weights}); the targets are submitted
    after ``process_bar``. Indices: 0 JAN2 ... 5 FEB1 (plan pending, SELL pending) ...
    """
    return [
        (JAN2, book(bar(SPY, 100.0, 100.0, atr14=2.0)), {CORE: {SPY: 0.2}, MR: {SPY: 0.5}}),
        (JAN3, book(bar(SPY, 100.0, 100.0, atr14=2.0)), {}),
        (JAN4, book(bar(SPY, 100.0, 95.0, atr14=2.0)), {MR: {SPY: 0.5}}),
        (JAN5, book(bar(SPY, 94.0, 94.0, atr14=2.0)), {MR: {}}),
        (JAN31, book(bar(SPY, 120.0, 120.0, atr14=2.0)), {}),
        (FEB1, book(bar(SPY, 120.0, 120.0, atr14=2.0)), {CORE: {}, MR: {}, CRYPTO: {}, CASH: {}}),
        (FEB2, book(bar(SPY, 120.0, 120.0, atr14=2.0)), {}),
        (FEB3, book(bar(SPY, 121.0, 121.0, atr14=2.0)), {}),
    ]


def _run_step(
    eng: PortfolioEngine,
    asof: date,
    bars: dict[str, Bar],
    targets: dict[str, dict[str, float]],
) -> str:
    result = eng.process_bar(asof, bars)
    for sid, weights in targets.items():
        eng.submit_target_weights(sid, weights, asof)
    return result.model_dump_json()


def test_restore_mid_scenario_matches_uninterrupted_run() -> None:
    """AC 11 / I15: export mid-scenario, JSON round trip, resume; identical to a straight run.

    The checkpoint is after FEB1: a rebalance plan is pending, the core SELL is pending,
    and a position carries an entry_asof and stop state. Ledger, state JSON and every
    later BarResult snapshot must match the uninterrupted run byte for byte.
    """
    cfg = make_config(mr_stop_mult="2.5")
    script = _script()
    checkpoint = 6

    straight = PortfolioEngine(cfg)
    straight_results = [_run_step(straight, *step) for step in script]

    first = PortfolioEngine(cfg)
    for step in script[:checkpoint]:
        _run_step(first, *step)
    mid = first.export_state()
    assert mid.rebalance_plan is not None
    assert any(o.status is OrderStatus.PENDING for o in mid.orders)

    restored = EngineState.from_json(mid.to_json())
    resumed = PortfolioEngine(cfg, state=restored)
    assert position(resumed.snapshot(), CORE, SPY).entry_asof == JAN3  # type: ignore[union-attr]
    resumed_results = [_run_step(resumed, *step) for step in script[checkpoint:]]

    assert resumed_results == straight_results[checkpoint:]
    assert ledger_json(resumed) == ledger_json(straight)
    assert resumed.export_state().to_json() == straight.export_state().to_json()
    resumed.verify()
    straight.verify()
