"""SimBroker order-book export/restore, and engine checkpoint equivalence (Wave 3)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tradebot.portfolio.config import default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.ledger import Ledger
from tradebot.portfolio.models import EventType, Stage
from tradebot.portfolio.portfolio import Portfolio
from tradebot.portfolio.state import EngineState
from tradebot.sim.broker import Bar, BookError, BrokerBook, SimBroker
from tradebot.sim.orders import OrderIdGenerator, OrderRequest, OrderStatus, Side

pytestmark = pytest.mark.unit

D = Decimal
D1, D2, D3, D4 = (date(2024, 1, 2) + timedelta(days=i) for i in range(4))
CORE = "core_momentum"


def _broker() -> SimBroker:
    led = Ledger()
    led.append(
        asof=D1,
        stage=Stage.CLOSE,
        sleeve_id=CORE,
        type=EventType.DEPOSIT,
        amount=D("10000.00"),
    )
    return SimBroker(default_config(), Portfolio(led), id_gen=OrderIdGenerator())


def _bar(sym: str, px: float = 100.0) -> Bar:
    return Bar(symbol=sym, open=px, high=px, low=px, close=px)


def _req(sym: str, qty: str, side: Side = Side.BUY) -> OrderRequest:
    return OrderRequest(sleeve_id=CORE, symbol=sym, side=side, qty=D(qty))


def _busy_broker() -> SimBroker:
    """One filled order, one cancelled, one waiting for a symbol with no bar."""
    b = _broker()
    b.submit_orders([_req("SPY", "5"), _req("QQQ", "3")], D1)
    b.cancel_order(b.pending_orders[1].order_id)
    b.submit_orders([_req("IWM", "2")], D1)
    b.process_bar(D2, {"SPY": _bar("SPY")})
    return b


def test_export_restore_gives_identical_subsequent_fills() -> None:
    a = _busy_broker()
    book = a.export_book()
    assert len(book.orders) == 3 and len(book.pending_ids) == 1
    assert {o.status for o in book.orders} == {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.PENDING,
    }

    # Restore onto a broker over an identical ledger copy, then drive both the same way.
    b = SimBroker(
        default_config(),
        Portfolio(Ledger(list(a.portfolio.ledger.events))),
        id_gen=OrderIdGenerator(a._ids.next_n),
    )
    b.restore_book(book)
    assert b.export_book() == book

    for br in (a, b):
        br.submit_orders([_req("SPY", "1", Side.SELL)], D2)
    bars3 = {"SPY": _bar("SPY", 101.0), "IWM": _bar("IWM", 50.0)}
    ra, rb = a.process_bar(D3, bars3), b.process_bar(D3, bars3)
    assert ra == rb and len(ra.fills) == 2
    assert a.export_book() == b.export_book()
    assert a.portfolio.ledger.events == b.portfolio.ledger.events
    with pytest.raises(Exception):  # noqa: B017  (StaleBarError: last_asof restored)
        b.process_bar(D3, bars3)


def test_json_round_trip_is_exact_and_text_decimals() -> None:
    book = _busy_broker().export_book()
    text = book.model_dump_json()
    assert BrokerBook.model_validate_json(text) == book
    assert BrokerBook.model_validate_json(text).model_dump_json() == text
    assert '"last_close":{"SPY":"100.00000000"}' in text.replace(" ", "")
    assert text == _busy_broker().export_book().model_dump_json()  # deterministic


def _pending_book() -> BrokerBook:
    return _busy_broker().export_book()


def test_inconsistent_books_are_rejected() -> None:
    good = _pending_book()
    pid = good.pending_ids[0]
    filled = next(o for o in good.orders if o.status is OrderStatus.FILLED)
    cases = {
        "unknown pending id": good.model_copy(update={"pending_ids": [pid, "ORD-999999"]}),
        "pending id not PENDING": good.model_copy(
            update={"pending_ids": [pid, filled.order_id], "waited": {pid: 1, filled.order_id: 0}}
        ),
        "PENDING missing from ids": good.model_copy(update={"pending_ids": [], "waited": {}}),
        "duplicate pending id": good.model_copy(update={"pending_ids": [pid, pid]}),
        "waited mismatch": good.model_copy(update={"waited": {}}),
        "extra waited": good.model_copy(update={"waited": {pid: 1, filled.order_id: 0}}),
        "negative wait": good.model_copy(update={"waited": {pid: -1}}),
        "duplicate order": good.model_copy(update={"orders": [*good.orders, filled]}),
    }
    for name, bad in cases.items():
        b = _broker()
        with pytest.raises(BookError):
            b.restore_book(bad)
        assert b.export_book().orders == [], name  # a rejected restore changes nothing


# ------------------------------------------------------------------ engine equivalence

SYMS = ("SPY", "QQQ", "BTC/USD")


def _bars(i: int) -> dict[str, Bar]:
    out: dict[str, Bar] = {}
    for k, sym in enumerate(SYMS):
        base = (400.0, 350.0, 40000.0)[k]
        wave = ((i * (k + 3)) % 11 - 5) * 0.004
        o = base * (1 + 0.002 * i + wave)
        c = o * (1 + ((i + k) % 5 - 2) * 0.003)
        out[sym] = Bar(
            symbol=sym,
            open=round(o, 2),
            high=round(max(o, c) * 1.004, 2),
            low=round(min(o, c) * 0.996, 2),
            close=round(c, 2),
            vol20=0.2 + 0.01 * (i % 4),
            atr14=round(o * 0.015, 2),
        )
    # a missing bar for QQQ now and then keeps orders pending across the checkpoint
    if i in (14, 15, 16):
        del out["QQQ"]
    return out


def _crypto_sleeve() -> str:
    return next(s for s in PortfolioEngine(default_config()).sleeve_ids if "crypto" in s)


def test_engine_restore_at_bar_15_is_byte_identical() -> None:
    cfg = default_config()
    crypto = _crypto_sleeve()

    def drive(eng: PortfolioEngine, i: int) -> None:
        asof = date(2024, 1, 2) + timedelta(days=i)
        eng.process_bar(asof, _bars(i))
        if i % 3 == 0:
            w = {"SPY": 0.4, "QQQ": 0.3} if i % 6 == 0 else {"SPY": 0.5}
            eng.submit_target_weights(CORE, w, asof)
            eng.submit_target_weights(crypto, {"BTC/USD": 0.5 if i % 4 else 0.2}, asof)

    straight = PortfolioEngine(cfg)
    for i in range(40):
        drive(straight, i)

    first = PortfolioEngine(cfg)
    for i in range(15):
        drive(first, i)
    saved = first.export_state().to_json()
    resumed = PortfolioEngine(cfg, EngineState.from_json(saved))
    assert resumed.export_state().to_json() == saved
    for i in range(15, 40):
        drive(resumed, i)

    assert len(straight.ledger) > 20
    assert any(e.type is EventType.FILL for e in straight.ledger)
    assert [e.model_dump_json() for e in resumed.ledger] == [
        e.model_dump_json() for e in straight.ledger
    ]
    assert resumed.export_state().to_json() == straight.export_state().to_json()
    resumed.verify()
