"""Wave 3 ledger/portfolio fixes: F7, F10, F11, F12, F14, R1, R3, R4, I4/I7, append_transfer.

Every expected number is derived by hand in the comments.
"""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

import tradebot.portfolio.portfolio as portfolio_mod
from tradebot.portfolio.ledger import Ledger, LedgerError, LedgerState, replay
from tradebot.portfolio.models import (
    EventType,
    FillSide,
    LedgerEvent,
    Position,
    Stage,
)
from tradebot.portfolio.money import to_text
from tradebot.portfolio.portfolio import Portfolio

pytestmark = pytest.mark.unit

D = Decimal
D1, D2 = date(2024, 1, 2), date(2024, 1, 3)
BUY, SELL = FillSide.BUY, FillSide.SELL


def _led(**sleeves: str) -> Ledger:
    led = Ledger(equity_qty_quantum=D("1"))  # whole-share mode (I4 pinned to quantum 1)
    for sid, amt in (sleeves or {"core": "1000.00"}).items():
        led.append(asof=D1, stage=Stage.CLOSE, sleeve_id=sid, type=EventType.DEPOSIT, amount=D(amt))
    return led


def _fill(led, side, qty, price, *, sym="SPY", sleeve="core", oid=None, fee="0.00"):
    return led.append_fill(
        asof=D2,
        stage=Stage.OPEN_FILL,
        sleeve_id=sleeve,
        symbol=sym,
        side=side,
        qty=D(qty),
        price=D(price),
        order_id=oid or f"O{led.next_seq:06d}",
        fee=D(fee),
    )


def _ev(seq, type, **kw):
    kw.setdefault("sleeve_id", "core")
    return LedgerEvent(seq=seq, asof=D1, stage=Stage.CLOSE, type=type, **kw)


# --- append_transfer --------------------------------------------------------------------


def test_append_transfer_books_pair_atomically() -> None:
    led = _led(a="1000.00", b="500.00")
    out, inn = led.append_transfer(D2, Stage.CLOSE, "a", "b", D("250.25"), "T000001")
    # a: 1000.00 - 250.25 = 749.75 ; b: 500.00 + 250.25 = 750.25
    assert (out.type, inn.type) == (EventType.CASH_TRANSFER_OUT, EventType.CASH_TRANSFER_IN)
    assert (out.seq, inn.seq) == (3, 4)
    assert led.state.cash == {"a": D("749.75"), "b": D("750.25")}
    assert out.metadata == {"to": "b"} and inn.metadata == {"from": "a"}
    assert led.state.unpaired_transfers() == []


def test_append_transfer_failure_leaves_nothing() -> None:
    led = _led(a="100.00", b="0.01")
    before = (len(led), led.state.cash)
    with pytest.raises(LedgerError):  # 100.01 > 100.00 cash
        led.append_transfer(D2, Stage.CLOSE, "a", "b", D("100.01"), "T1")
    with pytest.raises(LedgerError):  # IN leg to an unknown sleeve: OUT must be rolled back
        led.append_transfer(D2, Stage.CLOSE, "a", "ghost", D("10.00"), "T2")
    with pytest.raises(LedgerError):
        led.append_transfer(D2, Stage.CLOSE, "a", "a", D("1.00"), "T3")
    with pytest.raises(LedgerError):
        led.append_transfer(D2, Stage.CLOSE, "a", "b", D("0.00"), "T4")
    assert (len(led), led.state.cash) == before
    assert led.state.transfers == {}
    led.append_transfer(D2, Stage.CLOSE, "a", "b", D("10.00"), "T2")  # id T2 was not burned
    with pytest.raises(LedgerError):  # reuse of a booked transfer_id
        led.append_transfer(D2, Stage.CLOSE, "a", "b", D("1.00"), "T2")
    assert led.state.cash == {"a": D("90.00"), "b": D("10.01")}


# --- F7: grids, duplicate fill, FEE linkage ------------------------------------------------


def test_fill_price_and_qty_grids() -> None:
    led = _led()
    with pytest.raises(ValidationError):  # price 1.0000000001 has 10 dp
        _fill(led, BUY, "1", "1.0000000001")
    with pytest.raises(ValidationError):  # qty 1E-9 has 9 dp
        _fill(led, BUY, "1E-9", "100", sym="BTC/USD")
    with pytest.raises(LedgerError, match="1 grid"):
        _fill(led, BUY, "1.5", "100")  # equity must be integral
    assert len(led) == 1
    # Finer equity grid: 1.5 is fine at quantum 0.5, and a 1e-6 grid rejects 1e-7.
    fine = Ledger(equity_qty_quantum=D("0.000001"))
    fine.append(
        asof=D1, stage=Stage.CLOSE, sleeve_id="core", type=EventType.DEPOSIT, amount=D("1000.00")
    )
    _fill(fine, BUY, "1.5", "100")
    with pytest.raises(LedgerError, match="grid"):
        _fill(fine, BUY, "0.0000001", "100")
    _fill(led, BUY, "0.5", "100.00000001", sym="BTC/USD")  # 8 dp qty/price fine
    # 0.5 * 100.00000001 = 50.000000005 -> half-even to cents = 50.00
    assert led.state.cash["core"] == D("950.00")


def test_duplicate_fill_order_id_rejected_without_side_effects() -> None:
    led = _led()
    _fill(led, BUY, "1", "10", oid="X1")  # cash 990.00
    with pytest.raises(LedgerError, match="duplicate FILL"):
        _fill(led, BUY, "1", "10", oid="X1")
    assert len(led) == 2 and led.state.cash["core"] == D("990.00")
    assert led.state.fill_orders == {"X1": "core"}


def test_fee_must_link_to_a_fill_once() -> None:
    dep = _ev(1, EventType.DEPOSIT, amount=D("1000.00"))
    orphan = _ev(2, EventType.FEE, amount=D("1.00"), order_id="O1")
    with pytest.raises(LedgerError, match="no FILL"):
        Ledger([dep, orphan])
    fill = _ev(
        2,
        EventType.FILL,
        amount=D("10.00"),
        symbol="SPY",
        side=BUY,
        qty=D("1"),
        price=D("10"),
        order_id="O1",
    )
    fee = _ev(3, EventType.FEE, amount=D("1.00"), order_id="O1")
    led = Ledger([dep, fill, fee])
    assert led.state.cash["core"] == D("989.00")  # 1000 - 10 - 1
    with pytest.raises(LedgerError, match="second FEE"):
        Ledger([dep, fill, fee, _ev(4, EventType.FEE, amount=D("1.00"), order_id="O1")])
    other = _ev(1, EventType.DEPOSIT, amount=D("5.00"), sleeve_id="b")
    with pytest.raises(LedgerError, match="FEE sleeve"):
        Ledger(
            [
                dep,
                other.model_copy(update={"seq": 2}),
                fill.model_copy(update={"seq": 3}),
                _ev(4, EventType.FEE, amount=D("1.00"), order_id="O1", sleeve_id="b"),
            ]
        )


# --- F10: DEPOSIT -------------------------------------------------------------------------


def test_deposit_rules() -> None:
    led = Ledger()
    with pytest.raises(ValidationError):
        led.append(
            asof=D1, stage=Stage.CLOSE, sleeve_id="s", type=EventType.DEPOSIT, amount=D("0.00")
        )
    with pytest.raises(ValidationError):  # off the cent grid
        led.append(
            asof=D1, stage=Stage.CLOSE, sleeve_id="s", type=EventType.DEPOSIT, amount=D("1.005")
        )
    led.append(
        asof=D1, stage=Stage.CLOSE, sleeve_id="s", type=EventType.DEPOSIT, amount=D("100.00")
    )
    led.append(
        asof=D1, stage=Stage.CLOSE, sleeve_id="s", type=EventType.DEPOSIT, amount=D("50.00")
    )  # still before any FILL: initial funding
    _fill(led, BUY, "1", "10", sleeve="s")
    with pytest.raises(LedgerError, match="initial funding"):
        led.append(
            asof=D2, stage=Stage.CLOSE, sleeve_id="s", type=EventType.DEPOSIT, amount=D("1.00")
        )
    # another sleeve with no fill yet may still be funded
    led.append(asof=D2, stage=Stage.CLOSE, sleeve_id="t", type=EventType.DEPOSIT, amount=D("1.00"))
    assert led.state.deposits == D("151.00")


# --- F14 / R4: canonical exponents, qty grid ------------------------------------------------


def test_cent_fields_are_canonical() -> None:
    ev = _ev(1, EventType.DEPOSIT, amount=D("1E+2"))
    assert ev.amount == D("100.00") and ev.amount.as_tuple().exponent == -2
    assert to_text(ev.amount) == "100.00"
    pos = Position(symbol="SPY", qty=D("1"), cost_basis=D("5E+1"), entry_asof=D1)
    assert to_text(pos.cost_basis) == "50.00"
    with pytest.raises(ValidationError):
        _ev(1, EventType.DEPOSIT, amount=D("1.005"))


def test_position_qty_grid() -> None:
    Position(symbol="BTC/USD", qty=D("0.00000001"), cost_basis=D("0.00"), entry_asof=D1)
    with pytest.raises(ValidationError):
        Position(symbol="BTC/USD", qty=D("1E-9"), cost_basis=D("0.00"), entry_asof=D1)


# --- R1: replay-side FILL/FEE and transfer pairing -------------------------------------------


def test_ledger_from_events_checks_expected_fee() -> None:
    dep = _ev(1, EventType.DEPOSIT, amount=D("1000.00"))
    fill = _ev(
        2,
        EventType.FILL,
        amount=D("100.00"),
        symbol="SPY",
        side=BUY,
        qty=D("10"),
        price=D("10"),
        order_id="O1",
    )
    fee_ev = _ev(3, EventType.FEE, amount=D("0.10"), order_id="O1")
    want = lambda ev: D("0.10")  # noqa: E731  (10 bps of 100.00)
    with pytest.raises(LedgerError, match="expected fee 0.10, booked 0.00"):
        Ledger([dep, fill], expected_fee=want)
    assert len(Ledger([dep, fill, fee_ev], expected_fee=want)) == 3
    assert len(Ledger([dep, fill])) == 2  # no cost context: zero-fee fill is indistinguishable


def test_ledger_from_events_rejects_unpaired_or_unequal_transfers() -> None:
    a = _ev(1, EventType.DEPOSIT, amount=D("100.00"), sleeve_id="a")
    b = _ev(2, EventType.DEPOSIT, amount=D("100.00"), sleeve_id="b")
    out = _ev(3, EventType.CASH_TRANSFER_OUT, amount=D("10.00"), sleeve_id="a", transfer_id="T1")
    inn = _ev(4, EventType.CASH_TRANSFER_IN, amount=D("10.00"), sleeve_id="b", transfer_id="T1")
    with pytest.raises(LedgerError, match="unpaired"):
        Ledger([a, b, out])
    assert len(Ledger([a, b, out, inn])) == 4
    with pytest.raises(LedgerError, match="legs differ"):
        Ledger([a, b, out, inn.model_copy(update={"amount": D("9.99")})])
    with pytest.raises(LedgerError, match="already has an OUT"):
        Ledger([a, b, out, out.model_copy(update={"seq": 4})])


# --- R3: zero-proceeds SELL ---------------------------------------------------------------------


def test_zero_proceeds_sell_only_as_full_exit() -> None:
    led = _led(core="1000.00")
    _fill(led, BUY, "0.5", "100", sym="BTC/USD")  # 50.00
    with pytest.raises(LedgerError, match="zero proceeds"):
        _fill(led, SELL, "0.00000001", "100", sym="BTC/USD")  # 1e-6 -> 0.00, partial
    assert len(led) == 3 - 1  # DEPOSIT + BUY only
    _fill(led, SELL, "0.49999999", "100", sym="BTC/USD")  # 49.999999 -> 50.00, realized 0.00
    assert led.state.positions["core"]["BTC/USD"].qty == D("0.00000001")
    ev = _fill(led, SELL, "0.00000001", "100", sym="BTC/USD")[0]  # dust full exit, 0.00
    assert ev.amount == D("0.00") and ev.realized_pnl == D("0.00")
    assert "BTC/USD" not in led.state.positions["core"]


# --- Portfolio: F11, F12, I4/I7 in verify ------------------------------------------------------


def _pf_with_position() -> tuple[Portfolio, Decimal]:
    led = _led(core="1000.00", other="500.00")
    _fill(led, BUY, "4", "25")  # 100.00
    return Portfolio(led), D("30")


def test_snapshot_rejects_bad_close_for_held_symbol() -> None:
    pf, _ = _pf_with_position()
    for bad in (D("0"), D("-5"), D("NaN")):
        with pytest.raises(LedgerError, match="close for held symbol SPY"):
            pf.snapshot(D2, {"SPY": bad})
    with pytest.raises(LedgerError, match="close for held symbol SPY"):
        pf.snapshot(D2, {"SPY": 30.0})  # type: ignore[dict-item]
    flat = Portfolio(_led())
    assert flat.snapshot(D2, {"QQQ": D("-1")}).equity == D("1000.00")  # unheld: ignored


def test_verify_with_state_skips_replay_but_full_replay_available(monkeypatch) -> None:
    pf, close = _pf_with_position()
    snap = pf.snapshot(D2, {"SPY": close})  # mv 4*30=120.00; equity 900+500+120=1520.00
    assert snap.equity == D("1520.00")
    calls: list[int] = []
    real = replay
    monkeypatch.setattr(
        portfolio_mod, "replay", lambda evs, q=None: calls.append(1) or real(evs, q)
    )
    pf.verify(snap)  # default: replays (old behaviour)
    assert calls == [1]
    pf.verify(snap, state=pf.ledger.state)  # folded state given: no replay
    assert calls == [1]
    pf.verify(snap, state=pf.ledger.state, full_replay=True)
    assert calls == [1, 1]
    pf.verify(snap, full_replay=False)
    assert calls == [1, 1]


def test_ledger_state_view_is_not_a_copy() -> None:
    led = _led()
    assert led.state_view is led.state_view
    assert led.state is not led.state


def test_verify_remarks_with_closes_and_unknown_sleeve() -> None:
    pf, close = _pf_with_position()
    snap = pf.snapshot(D2, {"SPY": close})
    pf.verify(snap, closes={"SPY": close})
    with pytest.raises(LedgerError, match="mv != qty"):
        pf.verify(snap, closes={"SPY": D("31")})  # 4*31 = 124.00 != 120.00
    ghost = LedgerState()
    ghost.cash = {"zzz": D("1.00")}
    with pytest.raises(LedgerError, match="unknown to the ledger"):
        pf.verify(snap, state=ghost)


def test_verify_checks_i4_and_i7_on_state() -> None:
    pf, close = _pf_with_position()
    snap = pf.snapshot(D2, {"SPY": close})
    bad = pf.ledger.state
    bad.positions["core"]["SPY"] = Position(
        symbol="SPY", qty=D("4.5"), cost_basis=D("100.00"), entry_asof=D2
    )
    with pytest.raises(LedgerError, match="I4"):
        pf.verify(snap, state=bad)
    bad = pf.ledger.state
    bad.positions["core"]["SPY"] = Position.model_construct(
        symbol="SPY", qty=D("0"), cost_basis=D("0.00"), entry_asof=D2
    )
    with pytest.raises(LedgerError, match="I4"):
        pf.verify(snap, state=bad)
    bad = pf.ledger.state
    bad.positions["core"]["BTC/USD"] = Position(
        symbol="BTC/USD", qty=D("1"), cost_basis=D("1.00"), entry_asof=D2
    )
    bad.positions["core"]["BTC/USD"] = bad.positions["core"]["BTC/USD"].model_copy(
        update={"qty": D("1.000000001")}  # model_copy skips validation: 9 dp
    )
    with pytest.raises(LedgerError, match="8 decimals"):
        pf.verify(snap, state=bad)
    bad = pf.ledger.state
    bad.transfers["T9"] = {"OUT": ("core", D("5.00"))}
    with pytest.raises(LedgerError, match="I7"):
        pf.verify(snap, state=bad)
    bad = pf.ledger.state
    bad.transfers["T9"] = {"OUT": ("core", D("5.00")), "IN": ("other", D("4.00"))}
    with pytest.raises(LedgerError, match="net to zero"):
        pf.verify(snap, state=bad)
    pf.verify(snap)
