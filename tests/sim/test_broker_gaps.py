"""Audit gaps G7-G9 for the Wave 3 engine: SimBroker order of fills, cash shrink at fill, expiry,
and engine save/restore with live stops, locks, pending orders and a pending rebalance.

Every expected number is derived by hand in a comment (slippage 5 bps equity, 10 bps crypto,
fee 0 equity / 10 bps crypto, deposit into one sleeve).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tradebot.portfolio.config import default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.ledger import Ledger
from tradebot.portfolio.models import EventType, Stage
from tradebot.portfolio.portfolio import Portfolio
from tradebot.portfolio.state import EngineState
from tradebot.sim.broker import Bar, BrokerBook, SimBroker
from tradebot.sim.orders import (
    Order,
    OrderIdGenerator,
    OrderOrigin,
    OrderReason,
    OrderRequest,
    OrderStatus,
    Side,
)

pytestmark = pytest.mark.unit

D = Decimal
D1, D2, D3 = (date(2024, 1, 2) + timedelta(days=i) for i in range(3))
CORE = "core_momentum"


def _broker(cash: str = "10000.00", quantum: str = "1") -> SimBroker:
    led = Ledger()
    led.append(asof=D1, stage=Stage.CLOSE, sleeve_id=CORE, type=EventType.DEPOSIT, amount=D(cash))
    return SimBroker(
        default_config().model_copy(update={"equity_qty_quantum": D(quantum)}),
        Portfolio(led),
        id_gen=OrderIdGenerator(),
    )


def _bar(sym: str, px: float) -> Bar:
    return Bar(symbol=sym, open=px, high=px, low=px, close=px)


def _req(sym: str, qty: str, side: Side = Side.BUY) -> OrderRequest:
    return OrderRequest(sleeve_id=CORE, symbol=sym, side=side, qty=D(qty))


def _cash(b: SimBroker) -> Decimal:
    return b.portfolio.cash(CORE)


# ---------------------------------------------------------------------------- broker (G7)


def test_sells_fill_before_buys_even_when_the_buy_was_submitted_first() -> None:
    """Step A: SELLs first, so their proceeds fund a BUY that would not fit otherwise.

    Fill SPY 50 at open 100: 100 * 1.0005 = 100.05, 50 * 100.05 = 5,002.50, cash 4,997.50.
    D2 orders: BUY QQQ 90 (id 1; cost 90 * 100.05 = 9,004.50 > 4,997.50) and SELL SPY 50
    (id 2; 50 * floor8(100 * 0.9995) = 50 * 99.95 = 4,997.50 -> cash 9,995.00 >= 9,004.50).
    BUYs first would shrink the QQQ order to floor(4,997.50 / 100.05) = 49 shares.
    """
    b = _broker()
    b.submit_orders([_req("SPY", "50")], D1)
    b.process_bar(D2, {"SPY": _bar("SPY", 100.0)})
    assert _cash(b) == D("4997.50")
    b.submit_orders([_req("QQQ", "90"), _req("SPY", "50", Side.SELL)], D2)
    res = b.process_bar(D3, {"SPY": _bar("SPY", 100.0), "QQQ": _bar("QQQ", 100.0)})
    assert [o.side for o in res.fills] == [Side.SELL, Side.BUY]
    buy = res.fills[1]
    assert buy.qty_filled == D("90") and buy.reason is None
    assert _cash(b) == D("9995.00") - D("9004.50")  # 990.50


def test_buy_that_no_longer_fits_at_the_open_shrinks_and_keeps_its_accepted_qty() -> None:
    """Gap up: BUY 100 accepted at the old close, open now 120.

    Price 120 * 1.0005 = 120.06; 100 * 120.06 = 12,006.00 > 10,000.00. Largest affordable:
    83 * 120.06 = 9,964.98 (84 * 120.06 = 10,085.04). Order is FILLED for 83 with SHRUNK_CASH,
    ``qty_accepted`` stays 100, cash left 35.02.
    """
    b = _broker()
    b.submit_orders([_req("SPY", "100")], D1)
    res = b.process_bar(D2, {"SPY": _bar("SPY", 120.0)})
    (o,) = res.fills
    assert o.status is OrderStatus.FILLED and o.reason is OrderReason.SHRUNK_CASH
    assert (o.qty_requested, o.qty_accepted, o.qty_filled) == (D("100"), D("100"), D("83"))
    assert o.fill_price == D("120.06000000") and _cash(b) == D("35.02")


@pytest.mark.parametrize(
    ("cash", "filled", "reason"),
    [
        ("10005.00", "100", None),  # exact: 100 * 100.05 = 10,005.00 <= cash, no shrink
        ("10004.99", "99", OrderReason.SHRUNK_CASH),  # one cent short: 99 * 100.05 = 9,904.95
    ],
)
def test_affordability_boundary_is_inclusive(cash: str, filled: str, reason: OrderReason | None):
    b = _broker(cash)
    b.submit_orders([_req("SPY", "100")], D1)
    (o,) = b.process_bar(D2, {"SPY": _bar("SPY", 100.0)}).fills
    assert o.qty_filled == D(filled) and o.reason is reason


def test_no_cash_at_all_rejects_the_buy_as_insufficient_cash() -> None:
    b = _broker("50.00")  # 1 share costs 100.05
    b.submit_orders([_req("SPY", "1")], D1)
    res = b.process_bar(D2, {"SPY": _bar("SPY", 100.0)})
    (o,) = res.rejected
    assert o.status is OrderStatus.REJECTED and o.reason is OrderReason.INSUFFICIENT_CASH
    assert _cash(b) == D("50.00") and not res.new_ledger_events


def test_order_is_not_filled_by_the_bar_it_was_created_on() -> None:
    b = _broker()
    b.submit_orders([_req("SPY", "1")], D1)
    assert b.process_bar(D1, {"SPY": _bar("SPY", 100.0)}).fills == []
    assert [o.status for o in b.pending_orders] == [OrderStatus.PENDING]
    assert len(b.process_bar(D2, {"SPY": _bar("SPY", 100.0)}).fills) == 1


def test_pending_order_expires_after_max_pending_bars_without_a_bar_for_its_symbol() -> None:
    """max_pending_bars = 5: the order waits 4 bars still PENDING and EXPIRES on the 5th."""
    b = _broker()
    assert default_config().risk.max_pending_bars == 5
    b.submit_orders([_req("IWM", "1")], D1)
    ledger_len = len(b.portfolio.ledger)
    for i in range(1, 5):
        res = b.process_bar(D1 + timedelta(days=i), {"SPY": _bar("SPY", 100.0)})
        assert res.expired == [] and len(b.pending_orders) == 1
    res = b.process_bar(D1 + timedelta(days=5), {"SPY": _bar("SPY", 100.0)})
    (o,) = res.expired
    assert o.status is OrderStatus.EXPIRED and b.pending_orders == []
    assert len(b.portfolio.ledger) == ledger_len and _cash(b) == D("10000.00")


def test_zero_quantity_sell_is_rejected_as_below_min_size_not_insufficient_cash() -> None:
    """A SELL with nothing left to sell (restored book, flat ledger) is a size problem."""
    b = _broker()
    sell = Order(
        order_id="O00000007",
        sleeve_id=CORE,
        symbol="SPY",
        side=Side.SELL,
        origin=OrderOrigin.STRATEGY,
        qty_requested=D("5"),
        qty_accepted=D("5"),
        status=OrderStatus.PENDING,
        created_asof=D1,
    )
    b.restore_book(
        BrokerBook(
            orders=[sell], pending_ids=[sell.order_id], waited={sell.order_id: 0}, last_close={}
        )
    )
    (o,) = b.process_bar(D2, {"SPY": _bar("SPY", 100.0)}).rejected
    assert o.status is OrderStatus.REJECTED and o.reason is OrderReason.BELOW_MIN_SIZE


def test_crypto_fee_tie_rounds_half_up() -> None:
    """BUY 0.0001 BTC at open 50,000: price 50,050; notional q2(5.005) = 5.00 (HALF_EVEN tie);
    fee q2_half_up(5.00 * 0.001 = 0.005) = 0.01 (HALF_EVEN would give 0.00). Cash 100.00 -> 94.99.
    """
    b = _broker("100.00")
    b.submit_orders([_req("BTC/USD", "0.0001")], D1)
    (o,) = b.process_bar(D2, {"BTC/USD": _bar("BTC/USD", 50000.0)}).fills
    assert o.fill_price == D("50050.00000000") and o.fee == D("0.01")
    assert _cash(b) == D("94.99")


# ---------------------------------------------------------------------------- restore (G8)

SPY, QQQ, BTC = "SPY", "QQQ", "BTC/USD"


def _bars(spy_open: float, spy_close: float) -> dict[str, Bar]:
    return {
        SPY: Bar(
            symbol=SPY,
            open=spy_open,
            high=max(spy_open, spy_close),
            low=min(spy_open, spy_close),
            close=spy_close,
            vol20=0.02,
            atr14=2.0,
        ),
        QQQ: Bar(symbol=QQQ, open=200.0, high=200.0, low=200.0, close=200.0, vol20=0.02, atr14=3.0),
        BTC: Bar(
            symbol=BTC,
            open=40000.0,
            high=40000.0,
            low=40000.0,
            close=40000.0,
            vol20=0.03,
            atr14=800.0,
        ),
    }


def _engine_config():
    base = default_config().model_copy(update={"equity_qty_quantum": D("1")})
    sleeves = tuple(
        s.model_copy(update={"trailing_stop_atr_mult": D("1.5")})
        if s.sleeve_id == "mean_reversion"
        else s
        for s in base.sleeves
    )
    return base.model_copy(update={"sleeves": sleeves})


def test_restore_with_live_stops_locks_pending_orders_and_a_pending_rebalance() -> None:
    """Save while a STOP SELL and a BUY are pending, SPY is stop-locked and a monthly rebalance
    plan is open; the restored engine must continue byte-for-byte like the uninterrupted one.

    Mean-reversion holds SPY from Jan 31 (bar close 100, atr 2, mult 1.5 -> stop 97). On Feb 1
    the close is 90 <= 97: STOP exit pending and SPY locked; Feb 1 is also the first bar of
    February, so a rebalance plan is booked (transfers run on Feb 2).
    """
    cfg = _engine_config()
    days = [date(2024, 1, 29) + timedelta(days=i) for i in range(6)]  # Mon Jan 29 .. Sat Feb 3
    plan = [
        (_bars(100.0, 100.0), {"mean_reversion": {SPY: 0.5}}),  # Jan 29
        (_bars(100.0, 100.0), {}),  # Jan 30: SPY bought at the open
        (_bars(100.0, 100.0), {}),  # Jan 31
        (_bars(100.0, 90.0), {"core_momentum": {QQQ: 0.2}}),  # Feb 1: stop trips, BUY pending
        (_bars(90.0, 90.0), {"mean_reversion": {SPY: 0.5}}),  # Feb 2: STOP fills; lock bites
        (_bars(90.0, 91.0), {}),  # Feb 3
    ]

    def drive(eng: PortfolioEngine, upto: int, start: int = 0) -> list[str]:
        out: list[str] = []
        for i in range(start, upto):
            bars, targets = plan[i]
            out.append(eng.process_bar(days[i], bars).model_dump_json())
            for sid, w in targets.items():
                out.append(eng.submit_target_weights(sid, w, days[i]).model_dump_json())
        return out

    straight = PortfolioEngine(cfg)
    head = drive(straight, 4)
    mid = straight.export_state()
    # Preconditions: this really is the awkward moment.
    pend = [o for o in mid.orders if o.status is OrderStatus.PENDING]
    assert {(o.symbol, o.side, o.origin) for o in pend} == {
        (SPY, Side.SELL, OrderOrigin.STOP),
        (QQQ, Side.BUY, OrderOrigin.STRATEGY),
    }
    assert mid.stop_state["stopped_out"] == {"mean_reversion": [SPY]}
    assert mid.stop_state["levels"]["mean_reversion"][SPY]["stop_level"] == "97.00000000"
    assert mid.rebalance_plan is not None and mid.pending_waited

    restored = PortfolioEngine(cfg, EngineState.from_json(mid.to_json()))
    assert restored.export_state().to_json() == mid.to_json()
    restored.verify()

    tail_straight = drive(straight, 6, start=4)
    tail_restored = drive(restored, 6, start=4)
    assert tail_restored == tail_straight
    assert restored.export_state().to_json() == straight.export_state().to_json()
    assert head  # the first four bars ran
    # The lock survived the restore: the Feb 2 re-target of SPY is vetoed, not bought.
    locked = [r for r in tail_restored if '"STOP_LOCK"' in r]
    assert locked, "SPY re-entry should be rejected with STOP_LOCK after restore"
    assert not any(
        o.symbol == SPY and o.side is Side.BUY
        for o in restored.orders()
        if o.created_asof > days[3]
    )
