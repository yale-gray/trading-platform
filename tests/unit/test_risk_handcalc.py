"""Hand-calculated sizing, cap, stop, kill-switch and rebalance cases (SPEC-005 6.3, 6.6).

All runs go through ``PortfolioEngine`` public API with hand-built bars. Arithmetic
is written out in comments so a reviewer can re-derive every expected value.

Starting book (default config, $100,000.00): core_momentum 50000.00, mean_reversion
20000.00, crypto_trend 20000.00, cash 10000.00. Equity symbols fill at open +5 bps,
crypto at open +10 bps. Orders signalled on bar t fill at bar t+1 open.
"""

from datetime import date
from decimal import Decimal

import pytest

from tradebot.portfolio.config import EngineConfig, SleeveConfig, default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType, SleeveSnapshot
from tradebot.portfolio.money import floor8
from tradebot.sim.broker import Bar
from tradebot.sim.orders import OrderOrigin, OrderReason, Side

pytestmark = pytest.mark.unit

D1, D2, D3, D4 = date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)
CORE = "core_momentum"
MEAN = "mean_reversion"
CRYPTO = "crypto_trend"
CASH = "cash"


def _bar(
    symbol: str,
    open_: float,
    close: float,
    vol20: float | None = 0.02,
    atr14: float | None = None,
) -> Bar:
    return Bar(
        symbol=symbol,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        vol20=vol20,
        atr14=atr14,
    )


def _exp(d: Decimal) -> int:
    return d.as_tuple().exponent  # type: ignore[return-value]


def _sleeve(snap_sleeves: list[SleeveSnapshot], sid: str) -> SleeveSnapshot:
    return next(s for s in snap_sleeves if s.sleeve_id == sid)


def _config(sleeves: list[SleeveConfig]) -> EngineConfig:
    base = default_config()
    return EngineConfig(
        initial_capital=base.initial_capital,
        sleeves=tuple(sleeves),
        risk=base.risk,
        costs=base.costs,
    )


def _events(events: list, kind: EventType) -> list:
    return [e for e in events if e.type is kind]


# ---------------------------------------------------------------- start-up split (6.6)


def test_initial_sleeve_split_is_50_20_20_10() -> None:
    # 100000 x 0.50 = 50000.00 ; x 0.20 = 20000.00 ; x 0.20 = 20000.00 ; x 0.10 = 10000.00
    eng = PortfolioEngine(default_config())
    snap = eng.snapshot()
    assert _sleeve(snap.sleeves, CORE).cash == Decimal("50000.00")
    assert _sleeve(snap.sleeves, MEAN).cash == Decimal("20000.00")
    assert _sleeve(snap.sleeves, CRYPTO).cash == Decimal("20000.00")
    assert _sleeve(snap.sleeves, CASH).cash == Decimal("10000.00")
    assert snap.equity == Decimal("100000.00")
    eng.verify()


# ---------------------------------------------------------------- vol-scaled sizing (6.6)


def test_vol_scaled_sizing_tilts_weights_inverse_to_vol() -> None:
    # Raw targets 0.2 and 0.2 on core (sum W = 0.4). Inverse-vol scores:
    #   SPY: 0.2 / 0.01 = 20 ; QQQ: 0.2 / 0.04 = 5 ; score sum = 25.
    #   SPY weight = 0.4 x 20 / 25 = 0.32 ; QQQ weight = 0.4 x 5 / 25 = 0.08.
    # Targets on core sleeve equity 50000: SPY 0.32 x 50000 = 16000.00 at ref 100
    #   -> 160 shares ; QQQ 0.08 x 50000 = 4000.00 at ref 50 -> 80 shares.
    # Without the tilt SPY would be 10000 / 100 = 100 shares, so the test separates the two.
    eng = PortfolioEngine(default_config())
    eng.process_bar(
        D1,
        {
            "SPY": _bar("SPY", 100.0, 100.0, vol20=0.01),
            "QQQ": _bar("QQQ", 50.0, 50.0, vol20=0.04),
        },
    )
    res = eng.submit_target_weights(CORE, {"SPY": 0.2, "QQQ": 0.2}, D1)
    qty = {o.symbol: o.qty_requested for o in res.orders}
    assert qty == {"SPY": Decimal("160"), "QQQ": Decimal("80")}
    assert all(o.side is Side.BUY for o in res.orders)
    assert res.rejections == []
    eng.verify()


# ---------------------------------------------------------------- 25% asset cap (6.6)


def test_asset_cap_shrinks_buy_to_25_percent_of_equity() -> None:
    # Target 0.6 x core equity 50000 = 30000.00 at ref 100 -> 300 shares requested.
    # Asset room = 0.25 x equity 100000.00 - existing exposure 0 = 25000.00.
    # 25000.00 / 100 = 250 -> shrink to 250 shares (exposure 25000.00 = 25% of 100000.00).
    # Shrink is recorded as MAX_ASSET_SHRUNK with requested 300, accepted 250.
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"SPY": _bar("SPY", 100.0, 100.0)})
    res = eng.submit_target_weights(CORE, {"SPY": 0.6}, D1)
    assert len(res.orders) == 1
    assert res.orders[0].qty_requested == Decimal("250")
    (rej,) = res.rejections
    assert rej.reason is OrderReason.MAX_ASSET_SHRUNK
    assert rej.requested == Decimal("300")
    assert rej.accepted == Decimal("250")

    r2 = eng.process_bar(D2, {"SPY": _bar("SPY", 100.0, 100.0)})
    assert r2.fills[0].qty_filled == Decimal("250")
    # Fill: ceil8(100 x 1.0005) = 100.05 ; notional 250 x 100.05 = 25012.50 (cash out).
    assert r2.fills[0].fill_price == Decimal("100.05000000")
    eng.verify()


# ---------------------------------------------------------------- 30% crypto cap (6.6)


def test_crypto_cap_shrinks_second_crypto_buy_to_30_percent() -> None:
    # Custom 50/50 book: core (equity) and crypto (equity 50000), vol_scaled off.
    # BTC target 0.4 x 50000 = 20000.00 at ref 20000 -> 1.0 BTC; asset room 25000 - 0 OK.
    #   crypto room = 0.30 x 100000 - 0 = 30000 ; 20000 fits.
    # ETH target 0.3 x 50000 = 15000.00 at ref 1000 -> 15 ETH requested.
    #   pending BTC notional 20000.00 -> crypto room = 30000 - 20000 = 10000.00.
    #   10000 / 1000 = 10 ETH -> shrink to 10 (MAX_CRYPTO_SHRUNK, requested 15, accepted 10).
    sleeves = [
        SleeveConfig(
            sleeve_id=CORE,
            name="core",
            target_weight=Decimal("0.5"),
            vol_scaled=False,
            allowed_asset_classes=frozenset(default_config().sleeves[0].allowed_asset_classes),
        ),
        SleeveConfig(
            sleeve_id=CRYPTO,
            name="crypto",
            target_weight=Decimal("0.5"),
            vol_scaled=False,
            allowed_asset_classes=frozenset(default_config().sleeves[2].allowed_asset_classes),
        ),
    ]
    eng = PortfolioEngine(_config(sleeves))
    eng.process_bar(
        D1,
        {
            "BTC/USD": _bar("BTC/USD", 20000.0, 20000.0),
            "ETH/USD": _bar("ETH/USD", 1000.0, 1000.0),
        },
    )
    res = eng.submit_target_weights(CRYPTO, {"BTC/USD": 0.4, "ETH/USD": 0.3}, D1)
    qty = {o.symbol: o.qty_requested for o in res.orders}
    assert qty == {"BTC/USD": Decimal("1"), "ETH/USD": Decimal("10")}
    (rej,) = res.rejections
    assert rej.symbol == "ETH/USD"
    assert rej.reason is OrderReason.MAX_CRYPTO_SHRUNK
    assert rej.requested == Decimal("15")
    assert rej.accepted == Decimal("10")
    eng.verify()


# ---------------------------------------------------------------- trailing stop (6.6, ADR D5)


def _stop_config() -> EngineConfig:
    sleeves = [
        sc.model_copy(update={"trailing_stop_atr_mult": Decimal("2.5")})
        if sc.sleeve_id == CORE
        else sc
        for sc in default_config().sleeves
    ]
    return _config(sleeves)


def _open_stop_position() -> tuple[PortfolioEngine, date]:
    """Buy 100 SPY on D1 signal (0.2 x 50000 = 10000 at ref 100), fill D2 open 100.

    D2 close 120 with ATR 2: high-water close 120, stop = 120 - 2.5 x 2 = 115.
    """
    eng = PortfolioEngine(_stop_config())
    eng.process_bar(D1, {"SPY": _bar("SPY", 100.0, 100.0, atr14=2.0)})
    eng.submit_target_weights(CORE, {"SPY": 0.2}, D1)
    r2 = eng.process_bar(D2, {"SPY": _bar("SPY", 100.0, 120.0, atr14=2.0)})
    assert r2.fills[0].qty_filled == Decimal("100")
    pos = _sleeve(r2.snapshot.sleeves, CORE).positions[0]
    # Stop after the fill bar: hwc = close 120.00 ; candidate = 120.00 - 5.00 = 115.00
    assert pos.high_water_close == Decimal("120.00000000")
    assert pos.stop_level == Decimal("115.00000000")
    assert _exp(pos.stop_level) == -8
    return eng, D2


@pytest.mark.parametrize(
    ("close", "triggers"),
    [
        (Decimal("115.00"), True),  # equality: close == stop_level triggers (ADR D5, <=)
        (Decimal("115.01"), False),  # one cent above: no trigger
        (Decimal("114.99"), True),  # below: triggers
    ],
    ids=["equal_triggers", "above_no_trigger", "below_triggers"],
)
def test_trailing_stop_triggers_at_or_below_stop(close: Decimal, triggers: bool) -> None:
    # Stop level 115.00. Close at D3 is compared with 115.00 via close <= stop_level.
    eng, _ = _open_stop_position()
    r3 = eng.process_bar(D3, {"SPY": _bar("SPY", 115.0, float(close), atr14=2.0)})
    if not triggers:
        assert r3.stop_orders == []
        assert _events(r3.new_ledger_events, EventType.STOP_TRIGGERED) == []
        eng.verify()
        return

    (stop,) = r3.stop_orders
    assert stop.origin is OrderOrigin.STOP
    assert stop.side is Side.SELL
    assert stop.qty_requested == Decimal("100")
    (trig,) = _events(r3.new_ledger_events, EventType.STOP_TRIGGERED)
    assert trig.price == close.quantize(Decimal("0.00000001"))

    # The STOP exit fills at the next bar's open: floor8(116.00 x 0.9995) = 115.94200000.
    r4 = eng.process_bar(D4, {"SPY": _bar("SPY", 116.0, 116.0, atr14=2.0)})
    assert r4.fills[0].origin is OrderOrigin.STOP
    assert r4.fills[0].fill_price == floor8(Decimal("116.00") * Decimal("0.9995"))
    assert r4.fills[0].fill_price == Decimal("115.94200000")
    assert _sleeve(r4.snapshot.sleeves, CORE).positions == []
    eng.verify()


def test_trailing_stop_level_never_decreases() -> None:
    # D3: close 118.00, ATR 3. hwc stays 120 (max of 120 and 118).
    # candidate = 120 - 2.5 x 3 = 112.50 < prior stop 115.00, so the level stays 115.00.
    # 118.00 > 115.00 so no trigger.
    eng, _ = _open_stop_position()
    r3 = eng.process_bar(D3, {"SPY": _bar("SPY", 118.0, 118.0, atr14=3.0)})
    pos = _sleeve(r3.snapshot.sleeves, CORE).positions[0]
    assert pos.high_water_close == Decimal("120.00000000")
    assert pos.stop_level == Decimal("115.00000000")
    assert r3.stop_orders == []
    eng.verify()


# ---------------------------------------------------------------- kill switch (6.6, ADR D8)


def _kill_run(close_d3: float) -> tuple[PortfolioEngine, object]:
    """250 SPY bought at 100.05 (25012.50); cash 74987.50; peak stays 100000.

    At D2 close 100: equity = 74987.50 + 250 x 100 = 99987.50 (dd 0.0125%).
    At D3 close ``close_d3``: equity = 74987.50 + 250 x close_d3.
    """
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"SPY": _bar("SPY", 100.0, 100.0)})
    # Target 0.5 x 50000 = 25000.00 at ref 100 -> 250 shares; asset room 25000 is not exceeded.
    res = eng.submit_target_weights(CORE, {"SPY": 0.5}, D1)
    assert res.orders[0].qty_requested == Decimal("250")
    eng.process_bar(D2, {"SPY": _bar("SPY", 100.0, 100.0)})
    r3 = eng.process_bar(D3, {"SPY": _bar("SPY", 80.0, close_d3)})
    return eng, r3


def test_kill_switch_trips_at_exactly_20_percent_drawdown() -> None:
    # Equity at D3 = 74987.50 + 250 x 20.05 = 74987.50 + 5012.50 = 80000.00.
    # Peak = 100000.00. drawdown = 1 - 80000 / 100000 = 0.20 -> trips (>= 20%).
    eng, r3 = _kill_run(20.05)
    assert r3.snapshot.equity == Decimal("80000.00")
    assert r3.snapshot.drawdown == Decimal("0.2000000000")
    assert _exp(r3.snapshot.drawdown) == -10
    assert r3.kill_switch_tripped is True
    assert r3.snapshot.killed is True
    assert _events(r3.new_ledger_events, EventType.KILL_SWITCH_TRIPPED)
    eng.verify()


def test_kill_switch_does_not_trip_at_19_99_percent_drawdown() -> None:
    # Equity at D3 = 74987.50 + 250 x 20.054 = 74987.50 + 5013.50 = 80001.00.
    # drawdown = 1 - 80001 / 100000 = 0.19999 (19.999%) -> below 20%, no trip.
    eng, r3 = _kill_run(20.054)
    assert r3.snapshot.equity == Decimal("80001.00")
    assert r3.snapshot.drawdown == Decimal("0.1999900000")
    assert r3.kill_switch_tripped is False
    assert r3.snapshot.killed is False
    assert _events(r3.new_ledger_events, EventType.KILL_SWITCH_TRIPPED) == []
    eng.verify()


# ---------------------------------------------------------------- rebalance transfer (6.6)


def test_rebalance_moves_cash_from_overweight_to_underweight_sleeves() -> None:
    # D2: buy 200 SPY (0.4 x 50000 = 20000 at ref 100). Fill open 100 -> 100.05 x 200 =
    #   20010.00 ; core cash 29990.00 ; portfolio cash 79990.00.
    # D3 (first bar of April, close 150): mark 200 x 150 = 30000.00.
    #   equity = 79990.00 + 30000.00 = 109990.00 ; core sleeve equity = 29990 + 30000 = 59990.00
    #   targets: core 0.50 x 109990 = 54995.00 ; mean 21998.00 ; crypto 21998.00 ; cash 10999.00
    #   surplus: core +4995.00 ; mean -1998.00 ; crypto -1998.00 ; cash -999.00 (sums to 0)
    # D4 step C: pool = min(4995.00, 4995.00) = 4995.00 moved out of core, split to receivers
    #   by deficit in sleeve-id order: cash 999.00 ; crypto_trend 1998.00 ;
    #   mean_reversion 1998.00.
    # Final sleeve cash: core 24995.00 (+ MV 30000 = 54995.00) ; mean 21998.00 ; crypto 21998.00 ;
    #   cash 10999.00.
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"SPY": _bar("SPY", 100.0, 100.0)})
    eng.submit_target_weights(CORE, {"SPY": 0.4}, D1)
    eng.process_bar(D2, {"SPY": _bar("SPY", 100.0, 150.0)})

    d3 = date(2024, 4, 1)
    d4 = date(2024, 4, 2)
    r3 = eng.process_bar(d3, {"SPY": _bar("SPY", 150.0, 150.0)})
    assert r3.rebalance_planned is True
    assert r3.snapshot.equity == Decimal("109990.00")
    assert _events(r3.new_ledger_events, EventType.CASH_TRANSFER_OUT) == []

    r4 = eng.process_bar(d4, {"SPY": _bar("SPY", 150.0, 150.0)})
    # One OUT/IN pair per receiver: three pairs, all giving from core.
    outs = _events(r4.new_ledger_events, EventType.CASH_TRANSFER_OUT)
    ins = _events(r4.new_ledger_events, EventType.CASH_TRANSFER_IN)
    assert {e.sleeve_id for e in outs} == {CORE}
    total_out = sum((e.amount for e in outs), Decimal("0"))
    assert total_out == Decimal("4995.00")
    assert _exp(total_out) == -2
    in_by_sleeve = {e.sleeve_id: e.amount for e in ins}
    assert in_by_sleeve == {
        CASH: Decimal("999.00"),
        CRYPTO: Decimal("1998.00"),
        MEAN: Decimal("1998.00"),
    }
    assert sum(in_by_sleeve.values(), Decimal("0")) == total_out

    snap = r4.snapshot
    assert _sleeve(snap.sleeves, CORE).cash == Decimal("24995.00")
    assert _sleeve(snap.sleeves, MEAN).cash == Decimal("21998.00")
    assert _sleeve(snap.sleeves, CRYPTO).cash == Decimal("21998.00")
    assert _sleeve(snap.sleeves, CASH).cash == Decimal("10999.00")
    eng.verify()
