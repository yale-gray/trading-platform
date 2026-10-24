"""Order model, state machine, id generator and JSON round-trip (SPEC-003 section 3).

Expected values are derived by hand. Transition legality is checked over the full
6 x 6 status table, so every pair is either named as legal or must raise.
"""

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from tradebot.sim.orders import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransition,
    Order,
    OrderIdGenerator,
    OrderOrigin,
    OrderReason,
    OrderRequest,
    OrderStatus,
    Side,
    transition,
)

pytestmark = pytest.mark.unit

D = Decimal
ASOF = date(2024, 3, 15)
FILL_ASOF = date(2024, 3, 18)  # next trading day (I8: fill after creation)

# The only legal (from, to) pairs, written out by hand from SPEC-003 section 3.
LEGAL_PAIRS = {
    (OrderStatus.NEW, OrderStatus.PENDING),
    (OrderStatus.NEW, OrderStatus.REJECTED),
    (OrderStatus.PENDING, OrderStatus.FILLED),
    (OrderStatus.PENDING, OrderStatus.REJECTED),
    (OrderStatus.PENDING, OrderStatus.CANCELLED),
    (OrderStatus.PENDING, OrderStatus.EXPIRED),
}


def _new_order(**overrides: object) -> Order:
    fields: dict[str, object] = {
        "order_id": "O00000001",
        "sleeve_id": "core",
        "symbol": "SPY",
        "side": Side.BUY,
        "origin": OrderOrigin.STRATEGY,
        "qty_requested": D("100"),
        "created_asof": ASOF,
    }
    fields.update(overrides)
    return Order.model_validate(fields)


def _order_in(status: OrderStatus) -> Order:
    """Build an order in ``status`` by walking legal edges from NEW."""
    order = _new_order()
    if status is OrderStatus.NEW:
        return order
    if status is OrderStatus.REJECTED:
        return transition(order, OrderStatus.REJECTED, reason=OrderReason.KILL_SWITCH)
    order = transition(order, OrderStatus.PENDING)
    if status is OrderStatus.PENDING:
        return order
    if status is OrderStatus.FILLED:
        return transition(
            order,
            OrderStatus.FILLED,
            qty_accepted=D("100"),
            qty_filled=D("100"),
            fill_asof=FILL_ASOF,
            fill_price=D("100.05000000"),
            fee=D("0.00"),
        )
    return transition(order, status)


# --- State machine ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "dst"),
    [(s, d) for s in OrderStatus for d in OrderStatus],
    ids=lambda v: v.value,
)
def test_transition_table_legal_or_raises(src: OrderStatus, dst: OrderStatus) -> None:
    order = _order_in(src)
    assert order.status is src
    if (src, dst) in LEGAL_PAIRS:
        # A FILLED order must carry its fill fields (checked separately below).
        extra: dict[str, Any] = (
            {
                "qty_accepted": D("100"),
                "qty_filled": D("100"),
                "fill_asof": FILL_ASOF,
                "fill_price": D("100.05000000"),
                "fee": D("0.00"),
            }
            if dst is OrderStatus.FILLED
            else {}
        )
        assert transition(order, dst, **extra).status is dst
    else:
        with pytest.raises(IllegalTransition):
            transition(order, dst)


def test_legal_table_matches_hand_written_pairs() -> None:
    derived = {(src, dst) for src, dsts in LEGAL_TRANSITIONS.items() for dst in dsts}
    assert derived == LEGAL_PAIRS


def test_terminal_states_have_no_outgoing_edges() -> None:
    assert TERMINAL_STATES == {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    }
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset()


def test_illegal_transition_is_a_value_error() -> None:
    assert issubclass(IllegalTransition, ValueError)


def test_transition_does_not_mutate_input() -> None:
    order = _new_order()
    before = order.model_dump()
    result = transition(
        order,
        OrderStatus.PENDING,
    )
    assert order.model_dump() == before
    assert order.status is OrderStatus.NEW
    assert result is not order
    assert result.status is OrderStatus.PENDING


def test_transition_with_fill_fields_does_not_mutate_input() -> None:
    pending = _order_in(OrderStatus.PENDING)
    before = pending.model_dump()
    filled = transition(
        pending,
        OrderStatus.FILLED,
        qty_accepted=D("100"),
        qty_filled=D("100"),
        fill_asof=FILL_ASOF,
        fill_price=D("100.05000000"),
        fee=D("0.00"),
    )
    assert pending.model_dump() == before
    assert pending.qty_filled == D("0")
    assert pending.fill_price is None
    assert filled.fill_price == D("100.05000000")


def test_illegal_transition_leaves_input_unchanged() -> None:
    filled = _order_in(OrderStatus.FILLED)
    before = filled.model_dump()
    with pytest.raises(IllegalTransition):
        transition(filled, OrderStatus.PENDING)
    assert filled.model_dump() == before


def test_transition_applies_optional_fields_in_same_step() -> None:
    pending = _order_in(OrderStatus.PENDING)
    filled = transition(
        pending,
        OrderStatus.FILLED,
        qty_accepted=D("100"),
        qty_filled=D("60"),
        fill_asof=FILL_ASOF,
        fill_price=D("100.05000000"),
        fee=D("0.00"),
        reason="SHRUNK_CASH",
    )
    assert filled.qty_accepted == D("100")
    assert filled.qty_filled == D("60")
    assert filled.fill_asof == FILL_ASOF
    assert str(filled.fill_price) == "100.05000000"
    assert str(filled.fee) == "0.00"
    assert filled.reason == "SHRUNK_CASH"


# --- Quantity rule: 0 <= filled <= accepted <= requested (I14) -------------------


def test_quantity_chain_at_boundaries_is_valid() -> None:
    # filled = accepted = requested, and accepted = 0 with filled = 0, are both legal.
    _new_order(
        status=OrderStatus.FILLED,
        qty_accepted=D("100"),
        qty_filled=D("100"),
        fill_asof=FILL_ASOF,
        fill_price=D("100.05000000"),
        fee=D("0.00"),
    )
    _new_order(qty_accepted=D("0"), qty_filled=D("0"))
    _new_order(qty_accepted=D("60"), qty_filled=D("0"))


@pytest.mark.parametrize(
    "fields",
    [
        {"qty_requested": D("0")},  # requested must be > 0
        {"qty_requested": D("-1")},
        {"qty_filled": D("-1"), "qty_accepted": D("100")},  # filled >= 0
        {"qty_accepted": D("-1")},  # accepted >= 0
        {"qty_accepted": D("101")},  # accepted <= requested
        {"qty_accepted": D("60"), "qty_filled": D("61")},  # filled <= accepted
        {"qty_filled": D("1")},  # filled > 0 needs accepted
    ],
    ids=[
        "requested_zero",
        "requested_negative",
        "filled_negative",
        "accepted_negative",
        "accepted_above_requested",
        "filled_above_accepted",
        "filled_without_accepted",
    ],
)
def test_quantity_rule_violations_are_rejected(fields: dict[str, Decimal]) -> None:
    with pytest.raises(ValueError):
        _new_order(**fields)


def test_transition_that_breaks_quantity_rule_raises() -> None:
    pending = _order_in(OrderStatus.PENDING)
    with pytest.raises(ValueError):
        transition(
            pending,
            OrderStatus.FILLED,
            qty_accepted=D("50"),
            qty_filled=D("60"),
        )
    assert pending.status is OrderStatus.PENDING


# --- Deterministic ids -----------------------------------------------------------


def test_ids_are_sequential_and_zero_padded() -> None:
    gen = OrderIdGenerator()
    assert gen.next_n == 1
    assert gen.next_id() == "O00000001"
    assert gen.next_id() == "O00000002"
    assert gen.next_id() == "O00000003"
    assert gen.next_n == 4


def test_resume_from_next_n_continues_the_sequence() -> None:
    first = OrderIdGenerator()
    assert [first.next_id() for _ in range(3)] == ["O00000001", "O00000002", "O00000003"]
    resumed = OrderIdGenerator(next_n=first.next_n)
    assert resumed.next_id() == "O00000004"
    assert OrderIdGenerator(next_n=42).next_id() == "O00000042"


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1"])
def test_next_n_must_be_int_at_least_one(bad: object) -> None:
    with pytest.raises(ValueError):
        OrderIdGenerator(next_n=bad)  # type: ignore[arg-type]


# --- Decimal fields: canonical text, exact exponent, no floats --------------------


def test_json_round_trip_keeps_exact_decimal_text_and_exponent() -> None:
    # Quantities of 1E-8 must come back as "1E-8" (the exponent, not 0.00000001);
    # fill_price keeps its eight trailing zeros.
    pending = _new_order(qty_requested=D("1E-8"))
    pending = transition(pending, OrderStatus.PENDING)
    order = transition(
        pending,
        OrderStatus.FILLED,
        qty_accepted=D("1E-8"),
        qty_filled=D("1E-8"),
        fill_asof=FILL_ASOF,
        fill_price=D("100.05000000"),
        fee=D("0.00"),
    )
    text = order.model_dump_json()
    payload = json.loads(text)
    assert payload["qty_requested"] == "1E-8"
    assert payload["qty_filled"] == "1E-8"
    assert payload["fill_price"] == "100.05000000"
    assert payload["fee"] == "0.00"

    restored = Order.model_validate_json(text)
    assert restored == order
    assert str(restored.fill_price) == "100.05000000"
    assert restored.fill_price.as_tuple().exponent == -8
    assert str(restored.fee) == "0.00"
    assert restored.fee.as_tuple().exponent == -2
    assert restored.qty_requested.as_tuple().exponent == -8
    assert restored.qty_filled.as_tuple().exponent == -8


def test_json_round_trip_of_crypto_fill_is_exact() -> None:
    pending = transition(_new_order(qty_requested=D("0.5"), symbol="BTC/USD"), OrderStatus.PENDING)
    filled = transition(
        pending,
        OrderStatus.FILLED,
        qty_accepted=D("0.5"),
        qty_filled=D("0.5"),
        fill_asof=FILL_ASOF,
        fill_price=D("20020.00000000"),
        fee=D("10.01"),
    )
    payload = json.loads(filled.model_dump_json())
    assert payload["qty_filled"] == "0.5"
    assert payload["fill_price"] == "20020.00000000"
    assert payload["fee"] == "10.01"
    restored = Order.model_validate_json(filled.model_dump_json())
    assert str(restored.fill_price) == "20020.00000000"
    assert str(restored.fee) == "10.01"


def test_canonical_text_input_is_parsed_with_exponent() -> None:
    order = _new_order(qty_requested="0.50", ref_price="20020.00000000")
    assert str(order.qty_requested) == "0.50"
    assert str(order.ref_price) == "20020.00000000"


@pytest.mark.parametrize("bad", [1.5, 100.05])
def test_float_is_rejected_in_order_decimal_fields(bad: float) -> None:
    with pytest.raises((TypeError, ValidationError)):
        _new_order(qty_requested=bad)
    with pytest.raises((TypeError, ValidationError)):
        _new_order(qty_accepted=D("100"), qty_filled=D("0"), ref_price=bad)


def test_float_is_rejected_in_transition_decimal_fields() -> None:
    pending = _order_in(OrderStatus.PENDING)
    with pytest.raises((TypeError, ValidationError)):
        transition(
            pending,
            OrderStatus.FILLED,
            qty_accepted=D("100"),
            qty_filled=D("100"),
            fill_price=100.05,  # type: ignore[arg-type]
        )
    assert pending.status is OrderStatus.PENDING


def test_float_is_rejected_in_order_request_qty() -> None:
    with pytest.raises((TypeError, ValidationError)):
        OrderRequest(sleeve_id="core", symbol="SPY", side=Side.BUY, qty=0.5)  # type: ignore[arg-type]


def test_order_request_requires_positive_qty() -> None:
    req = OrderRequest(sleeve_id="core", symbol="SPY", side=Side.BUY, qty=D("1E-8"))
    assert str(req.qty) == "1E-8"
    with pytest.raises(ValueError):
        OrderRequest(sleeve_id="core", symbol="SPY", side=Side.BUY, qty=D("0"))


# --- FILLED consistency, I8, reason enum (audit F9) --------------------------------


def _fill_kwargs(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "qty_accepted": D("100"),
        "qty_filled": D("100"),
        "fill_asof": FILL_ASOF,
        "fill_price": D("100.05000000"),
        "fee": D("0.00"),
    }
    kw.update(over)
    return kw


@pytest.mark.parametrize(
    "over",
    [
        {"fill_asof": None},
        {"fill_price": None},
        {"fee": None},
        {"qty_filled": D("0")},
        {"fill_asof": ASOF},  # I8: same bar as creation
        {"fill_asof": date(2024, 3, 14)},  # before creation
    ],
    ids=["no-asof", "no-price", "no-fee", "zero-qty", "same-day", "before-creation"],
)
def test_filled_requires_fill_fields_and_later_fill_asof(over: dict[str, Any]) -> None:
    pending = _order_in(OrderStatus.PENDING)
    with pytest.raises(ValidationError):
        transition(pending, OrderStatus.FILLED, **_fill_kwargs(**over))


def test_non_filled_order_cannot_carry_filled_qty() -> None:
    pending = _order_in(OrderStatus.PENDING)
    with pytest.raises(ValidationError):
        transition(pending, OrderStatus.CANCELLED, qty_accepted=D("100"), qty_filled=D("5"))


def test_reason_is_enum_with_spec_codes_and_rejects_unknown_text() -> None:
    assert {r.value for r in OrderReason} == {
        "KILL_SWITCH", "NO_DATA", "NO_VOL_DATA", "STOP_LOCK", "MAX_ASSET_SHRUNK",
        "MAX_ASSET", "MAX_CRYPTO_SHRUNK", "MAX_CRYPTO", "CASH_SHRUNK",
        "BELOW_MIN_SIZE", "SHRUNK_CASH", "INSUFFICIENT_CASH", "FLATTEN_NO_BAR", "FLATTEN_LATE",
        "DELIST_NO_BAR",
    }  # fmt: skip
    pending = _order_in(OrderStatus.PENDING)
    assert transition(pending, OrderStatus.REJECTED, reason="MAX_ASSET").reason is (
        OrderReason.MAX_ASSET
    )
    with pytest.raises(ValidationError):
        transition(pending, OrderStatus.REJECTED, reason="because")
