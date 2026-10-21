"""Ledger fold, rejections, atomic fill+fee, replay == live (SPEC-003 §9, I3/I5/I14/I18).

Every expected number is derived by hand in the comments; none is read back from code.
"""

import copy
from datetime import date
from decimal import ROUND_DOWN, Context, Decimal, localcontext

import pytest

from tradebot.portfolio.ledger import Ledger, LedgerError, LedgerState, replay
from tradebot.portfolio.models import EventType, FillSide, LedgerEvent, Stage

pytestmark = pytest.mark.unit

D = Decimal
D1, D2, D3, D4, D5 = (date(2024, 1, d) for d in (2, 3, 4, 5, 8))
BUY, SELL = FillSide.BUY, FillSide.SELL


def _ledger(cash: str = "1000.00", sleeve: str = "core") -> Ledger:
    led = Ledger()
    led.append(asof=D1, stage=Stage.CLOSE, sleeve_id=sleeve, type=EventType.DEPOSIT, amount=D(cash))
    return led


def _fill(
    led: Ledger,
    side: FillSide,
    qty: str,
    price: str,
    *,
    asof: date = D1,
    fee: str = "0.00",
    sleeve: str = "core",
    symbol: str = "SPY",
    order_id: str | None = None,
) -> list[LedgerEvent]:
    return led.append_fill(
        asof=asof,
        stage=Stage.OPEN_FILL,
        sleeve_id=sleeve,
        symbol=symbol,
        side=side,
        qty=D(qty),
        price=D(price),
        order_id=order_id or f"O{led.next_seq:08d}",
        fee=D(fee),
    )


# --- Fold per event type -----------------------------------------------------------


def test_deposit_buy_add_trim_exit_with_exact_cents() -> None:
    led = _ledger("1000.00")
    st = led.state
    assert (st.cash["core"], st.deposits) == (D("1000.00"), D("1000.00"))

    _fill(led, BUY, "3", "100")  # 3 * 100 = 300.00 -> cash 700.00, basis 300.00
    _fill(led, BUY, "2", "110")  # 2 * 110 = 220.00 -> cash 480.00, basis 520.00, qty 5
    p = led.state.positions["core"]["SPY"]
    assert (led.state.cash["core"], p.qty, p.cost_basis) == (D("480.00"), D("5"), D("520.00"))

    # SELL 1 @ 120: proceeds 120.00; reduce = q2(520 * 1 / 5) = 104.00; realized 16.00
    ev = _fill(led, SELL, "1", "120")[0]
    assert ev.realized_pnl == D("16.00")
    st = led.state
    p = st.positions["core"]["SPY"]
    assert (st.cash["core"], p.qty, p.cost_basis) == (D("600.00"), D("4"), D("416.00"))
    assert st.realized == D("16.00")

    # SELL remaining 4 @ 120: proceeds 480.00; reduce = all 416.00; realized 64.00
    ev = _fill(led, SELL, "4", "120")[0]
    assert ev.realized_pnl == D("64.00")
    st = led.state
    assert st.positions["core"] == {}
    assert (st.cash["core"], st.realized) == (D("1080.00"), D("80.00"))


def test_fee_reduces_cash_and_accumulates_without_touching_basis() -> None:
    led = _ledger("1000.00")
    # BUY 10 @ 10 = 100.00 plus fee 0.50: cash 1000 - 100 - 0.50 = 899.50, basis stays 100.00
    evs = _fill(led, BUY, "10", "10", fee="0.50")
    assert [e.type for e in evs] == [EventType.FILL, EventType.FEE]
    assert [e.seq for e in evs] == [2, 3]
    st = led.state
    assert st.cash["core"] == D("899.50")
    assert st.fees == D("0.50")
    assert st.positions["core"]["SPY"].cost_basis == D("100.00")


def test_sell_fee_is_deducted_from_proceeds() -> None:
    led = _ledger("1000.00")
    _fill(led, BUY, "10", "10")  # cash 900.00, basis 100.00
    # SELL 10 @ 12 = 120.00, fee 1.20: cash 900 + 120 - 1.20 = 1018.80; realized 20.00 (pre-fee)
    ev = _fill(led, SELL, "10", "12", fee="1.20")[0]
    st = led.state
    assert (st.cash["core"], st.fees, ev.realized_pnl) == (D("1018.80"), D("1.20"), D("20.00"))


def test_transfers_move_cash_between_sleeves_and_keep_total() -> None:
    led = _ledger("1000.00", "a")
    led.append(
        asof=D1, stage=Stage.CLOSE, sleeve_id="b", type=EventType.DEPOSIT, amount=D("500.00")
    )
    kw = {"asof": D1, "stage": Stage.CLOSE, "transfer_id": "T000001", "amount": D("250.00")}
    led.append(sleeve_id="a", type=EventType.CASH_TRANSFER_OUT, **kw)
    led.append(sleeve_id="b", type=EventType.CASH_TRANSFER_IN, **kw)
    st = led.state
    # a: 1000 - 250 = 750.00; b: 500 + 250 = 750.00; total 1500.00 unchanged
    assert (st.cash["a"], st.cash["b"]) == (D("750.00"), D("750.00"))
    assert sum(st.cash.values()) == D("1500.00")
    assert st.deposits == D("1500.00")


def test_annotations_change_nothing_but_the_sequence() -> None:
    led = _ledger()
    before = led.state
    for t in (
        EventType.KILL_SWITCH_TRIPPED,
        EventType.KILL_SWITCH_RESET,
        EventType.REBALANCE_PLANNED,
        EventType.REBALANCE_INCOMPLETE,
        EventType.STOP_TRIGGERED,
    ):
        led.append(asof=D1, stage=Stage.CLOSE, sleeve_id="core", type=t, metadata={"why": "x"})
    after = led.state
    assert after.cash == before.cash and after.positions == before.positions
    assert (after.deposits, after.fees, after.realized) == (D("1000.00"), D("0.00"), D("0.00"))
    assert after.last_seq == 6


# --- Proportional basis reduction and rounding ------------------------------------


def test_basis_reduction_rounds_to_cents_and_sums_to_original() -> None:
    led = _ledger("1000.00")
    # 3 * 33.33333333 = 99.99999999 -> q2 = 100.00 basis
    _fill(led, BUY, "3", "33.33333333")
    assert led.state.positions["core"]["SPY"].cost_basis == D("100.00")

    # sell 1: reduce = q2(100.00 * 1 / 3) = q2(33.3333..) = 33.33 ; left 66.67 ; realized 40 - 33.33
    ev1 = _fill(led, SELL, "1", "40")[0]
    assert ev1.realized_pnl == D("6.67")
    assert led.state.positions["core"]["SPY"].cost_basis == D("66.67")

    # sell 1 of 2: reduce = q2(66.67 / 2) = q2(33.335) = 33.34 (HALF_EVEN, 3 is odd -> up)
    ev2 = _fill(led, SELL, "1", "40")[0]
    assert ev2.realized_pnl == D("6.66")  # 40.00 - 33.34
    assert led.state.positions["core"]["SPY"].cost_basis == D("33.33")

    # last share: reduce = whole remaining basis 33.33. 33.33 + 33.34 + 33.33 = 100.00 exactly
    ev3 = _fill(led, SELL, "1", "40")[0]
    assert ev3.realized_pnl == D("6.67")
    assert led.state.positions["core"] == {}
    # total realized = 120.00 - 100.00 = 20.00 (6.67 + 6.66 + 6.67)
    assert led.state.realized == D("20.00")


# --- entry_asof (I18) ----------------------------------------------------------------


def test_entry_asof_set_on_first_buy_kept_on_add_and_trim_reset_on_reentry() -> None:
    led = _ledger("10000.00")

    def entry() -> date | None:
        pos = led.state.positions.get("core", {}).get("SPY")
        return None if pos is None else pos.entry_asof

    assert entry() is None
    _fill(led, BUY, "10", "10", asof=D1)
    assert entry() == D1
    _fill(led, BUY, "5", "10", asof=D2)  # add: unchanged
    assert entry() == D1
    _fill(led, SELL, "5", "10", asof=D3)  # trim: unchanged
    assert entry() == D1
    _fill(led, SELL, "10", "10", asof=D4)  # exit: flat, no position, no entry
    assert entry() is None
    _fill(led, BUY, "1", "10", asof=D5)  # re-entry: new date
    assert entry() == D5


# --- Rejections -------------------------------------------------------------------


def test_oversell_and_sell_without_position_and_unknown_sleeve_are_rejected() -> None:
    led = _ledger("1000.00")
    _fill(led, BUY, "5", "10")
    n = len(led)
    with pytest.raises(LedgerError):
        _fill(led, SELL, "6", "10")  # oversell
    with pytest.raises(LedgerError):
        _fill(led, SELL, "1", "10", symbol="QQQ")  # no position
    with pytest.raises(LedgerError):
        _fill(led, SELL, "1", "10", sleeve="nope")  # unknown sleeve
    assert len(led) == n


def test_cash_overdraw_rejected_and_exactly_zero_cash_allowed() -> None:
    led = _ledger("100.00")
    with pytest.raises(LedgerError):
        _fill(led, BUY, "1", "100.01")  # 100.01 > 100.00
    _fill(led, BUY, "1", "100")  # spends exactly all cash: allowed, cash 0.00 (I3)
    assert led.state.cash["core"] == D("0.00")
    with pytest.raises(LedgerError):
        led.append(
            asof=D1,
            stage=Stage.CLOSE,
            sleeve_id="core",
            type=EventType.CASH_TRANSFER_OUT,
            transfer_id="T000001",
            amount=D("0.01"),
        )


def test_failed_append_leaves_state_unchanged() -> None:
    led = _ledger("100.00")
    n, snap = len(led), led.state
    with pytest.raises(LedgerError):
        _fill(led, BUY, "2", "100")
    assert len(led) == n
    assert led.next_seq == n + 1
    assert led.state.cash == snap.cash and led.state.last_seq == snap.last_seq


def test_zero_notional_buy_is_rejected() -> None:
    led = _ledger("1000.00", "crypto")
    # 1E-8 BTC * 60000.12345678 = 0.00060000 -> q2 = 0.00 : free crypto must not be bookable
    with pytest.raises(LedgerError, match="zero-notional"):
        _fill(led, BUY, "1E-8", "60000.12345678", sleeve="crypto", symbol="BTC/USD")
    assert led.state.positions.get("crypto", {}) == {}


def test_non_dense_seq_rejected_on_replay_and_on_construction() -> None:
    dep = LedgerEvent(
        seq=1, asof=D1, stage=Stage.CLOSE, sleeve_id="core", type=EventType.DEPOSIT,
        amount=D("10.00"),
    )  # fmt: skip
    gap = dep.model_copy(update={"seq": 3})
    with pytest.raises(LedgerError, match="dense"):
        replay([dep, gap])
    with pytest.raises(LedgerError, match="dense"):
        replay([gap])
    with pytest.raises(LedgerError, match="dense"):
        replay([dep, dep])  # repeated seq 1
    with pytest.raises(LedgerError, match="dense"):
        Ledger([dep, gap])


def test_low_level_append_refuses_fill_and_fee() -> None:
    led = _ledger()
    with pytest.raises(LedgerError, match="append_fill"):
        led.append(asof=D1, stage=Stage.OPEN_FILL, sleeve_id="core", type=EventType.FEE,
                   amount=D("1.00"), order_id="O00000001")  # fmt: skip


# --- Atomic fill + fee (F3) -------------------------------------------------------


def test_fill_that_fits_but_fee_that_does_not_books_nothing() -> None:
    led = _ledger("500.00")
    # BUY 5 @ 100 = 500.00 fits exactly; fee 0.50 does not (cash would be -0.50).
    with pytest.raises(LedgerError):
        _fill(led, BUY, "5", "100", fee="0.50")
    assert len(led) == 1  # only the DEPOSIT
    st = led.state
    assert st.cash["core"] == D("500.00") and st.positions.get("core", {}) == {}
    assert st.fees == D("0.00") and st.last_seq == 1
    assert led.next_seq == 2


def test_fill_plus_fee_fits_exactly_and_books_both_with_dense_seq() -> None:
    led = _ledger("500.50")
    evs = _fill(led, BUY, "5", "100", fee="0.50")  # 500.00 + 0.50 = 500.50 = all cash
    assert [(e.seq, e.type) for e in evs] == [(2, EventType.FILL), (3, EventType.FEE)]
    assert evs[0].order_id == evs[1].order_id
    assert led.state.cash["core"] == D("0.00")
    assert len(led) == 3


def test_zero_fee_books_no_fee_event() -> None:
    led = _ledger()
    evs = _fill(led, BUY, "1", "10")
    assert [e.type for e in evs] == [EventType.FILL]


# --- Replay == live (I5) -------------------------------------------------------------


def _same(a: LedgerState, b: LedgerState) -> None:
    assert a.cash == b.cash
    assert a.positions == b.positions
    assert (a.deposits, a.fees, a.realized, a.last_seq) == (
        b.deposits,
        b.fees,
        b.realized,
        b.last_seq,
    )
    assert a.opened_seq == b.opened_seq


def test_replay_of_events_equals_live_state() -> None:
    led = _ledger("5000.00")
    led.append(
        asof=D1, stage=Stage.CLOSE, sleeve_id="s2", type=EventType.DEPOSIT, amount=D("2000.00")
    )
    _fill(led, BUY, "7", "33.33333333", fee="0.25")
    _fill(led, BUY, "3", "41.5", sleeve="s2", symbol="QQQ")
    _fill(led, SELL, "2", "40", fee="0.10")
    kw = {"asof": D2, "stage": Stage.CLOSE, "transfer_id": "T000001", "amount": D("100.00")}
    led.append(sleeve_id="core", type=EventType.CASH_TRANSFER_OUT, **kw)
    led.append(sleeve_id="s2", type=EventType.CASH_TRANSFER_IN, **kw)
    led.append(asof=D2, stage=Stage.CLOSE, sleeve_id="core", type=EventType.STOP_TRIGGERED)
    _fill(led, SELL, "5", "45", asof=D3)
    _same(replay(led.events), led.state)
    _same(Ledger(led.events).state, led.state)
    # events are stored with realized_pnl filled in, so a replay re-verifies it
    assert all(e.realized_pnl is not None for e in led.events if e.side is SELL)


def test_replay_rejects_a_tampered_realized_pnl() -> None:
    led = _ledger()
    _fill(led, BUY, "3", "100")
    sell = _fill(led, SELL, "1", "120")[0]  # true realized 120 - 100 = 20.00
    assert sell.realized_pnl == D("20.00")
    bad = sell.model_copy(update={"realized_pnl": D("21.00")})
    with pytest.raises(LedgerError, match="realized_pnl"):
        replay([*led.events[:-1], bad])


# --- Ambient decimal context must not matter (F2) -----------------------------------


def _scenario() -> tuple[LedgerState, list[str]]:
    led = _ledger("1000.00")
    _fill(led, BUY, "3", "33.33333333")  # basis 100.00
    _fill(led, SELL, "1", "40")  # realized 6.67 ; basis 66.67
    _fill(led, SELL, "1", "40")  # reduce 33.34
    out = [str(e.realized_pnl) for e in led.events if e.side is SELL]
    return led.state, out


@pytest.mark.parametrize(
    "ctx",
    [Context(prec=3), Context(prec=5, rounding=ROUND_DOWN), Context(prec=50)],
    ids=["prec3", "prec5-round-down", "prec50"],
)
def test_ledger_fold_is_independent_of_ambient_context(ctx: Context) -> None:
    ref_state, ref_out = _scenario()
    assert ref_out == ["6.67", "6.66"]
    with localcontext(ctx):
        state, out = _scenario()
    _same(state, ref_state)
    assert out == ref_out
    pos = state.positions["core"]["SPY"]
    assert str(pos.cost_basis) == "33.33"  # on the cent grid, not 33.3 or 33.333...


# --- Events and state are not aliased ---------------------------------------------------


def test_state_property_is_a_copy_and_events_are_immutable() -> None:
    led = _ledger()
    st = led.state
    st.cash["core"] = D("0.00")
    assert led.state.cash["core"] == D("1000.00")
    ev = led.events[0]
    with pytest.raises(Exception):  # noqa: B017 - frozen model
        ev.amount = D("1.00")  # type: ignore[misc]
    assert copy.deepcopy(ev) == ev
