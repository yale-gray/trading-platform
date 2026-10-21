"""Model validation: float rejection (I17), shapes, canonical JSON, immutable metadata."""

import copy
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from tradebot.portfolio.models import (
    EventType,
    FillSide,
    LedgerEvent,
    PortfolioSnapshot,
    Position,
    PositionSnapshot,
    SleeveSnapshot,
    Stage,
)

pytestmark = pytest.mark.unit

D = Decimal
ASOF = date(2024, 1, 2)
REJECT = (TypeError, ValidationError)


def _fill_kw(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "seq": 1, "asof": ASOF, "stage": Stage.OPEN_FILL, "sleeve_id": "core",
        "type": EventType.FILL, "amount": D("300.00"), "symbol": "SPY", "side": FillSide.BUY,
        "qty": D("3"), "price": D("100"), "order_id": "O00000001",
    }  # fmt: skip
    kw.update(over)
    return kw


def _pos_snap(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "symbol": "SPY", "qty": D("3"), "mv": D("303.00"), "cost_basis": D("300.00"),
        "unrealized": D("3.00"), "entry_asof": ASOF,
        "high_water_close": D("101"), "stop_level": D("90"),
    }  # fmt: skip
    kw.update(over)
    return kw


def _sleeve(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "sleeve_id": "core", "cash": D("1.00"), "equity": D("304.00"), "weight": D("1"),
        "positions": [PositionSnapshot(**_pos_snap())],
    }  # fmt: skip
    kw.update(over)
    return kw


def _snap(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "asof": ASOF, "equity": D("304.00"), "cash": D("1.00"), "drawdown": D("0"),
        "peak_equity": D("304.00"), "killed": False, "last_seq": 1,
        "sleeves": [SleeveSnapshot(**_sleeve())], "exposures": {"SPY": D("0.5")},
        "crypto_fraction": D("0"),
    }  # fmt: skip
    kw.update(over)
    return kw


def test_valid_models_construct() -> None:
    LedgerEvent(**_fill_kw())
    Position(symbol="SPY", qty=D("3"), cost_basis=D("300.00"), entry_asof=ASOF)
    PortfolioSnapshot(**_snap())


@pytest.mark.parametrize("field", ["amount", "qty", "price", "slippage_cost", "realized_pnl"])
def test_ledger_event_rejects_float_in_every_decimal_field(field: str) -> None:
    with pytest.raises(REJECT):
        LedgerEvent(**_fill_kw(**{field: 1.5}))


@pytest.mark.parametrize("field", ["qty", "cost_basis", "high_water_close", "stop_level"])
def test_position_rejects_float_in_every_decimal_field(field: str) -> None:
    kw: dict[str, Any] = {
        "symbol": "SPY", "qty": D("3"), "cost_basis": D("300.00"), "entry_asof": ASOF,
        "high_water_close": D("101"), "stop_level": D("90"),
    }  # fmt: skip
    kw[field] = 0.5
    with pytest.raises(REJECT):
        Position(**kw)


@pytest.mark.parametrize(
    "field", ["qty", "mv", "cost_basis", "unrealized", "high_water_close", "stop_level"]
)
def test_position_snapshot_rejects_float(field: str) -> None:
    with pytest.raises(REJECT):
        PositionSnapshot(**_pos_snap(**{field: 0.1}))


@pytest.mark.parametrize("field", ["cash", "equity", "weight"])
def test_sleeve_snapshot_rejects_float(field: str) -> None:
    with pytest.raises(REJECT):
        SleeveSnapshot(**_sleeve(**{field: 0.1}))


@pytest.mark.parametrize("field", ["equity", "cash", "drawdown", "peak_equity", "crypto_fraction"])
def test_portfolio_snapshot_rejects_float(field: str) -> None:
    with pytest.raises(REJECT):
        PortfolioSnapshot(**_snap(**{field: 0.1}))


def test_portfolio_snapshot_rejects_float_in_exposures() -> None:
    with pytest.raises(REJECT):
        PortfolioSnapshot(**_snap(exposures={"SPY": 0.3}))


def test_int_and_non_canonical_text_are_also_rejected() -> None:
    with pytest.raises(REJECT):
        LedgerEvent(**_fill_kw(qty=3))
    with pytest.raises(REJECT):
        LedgerEvent(**_fill_kw(qty=" 3"))


# --- Shape rules ---------------------------------------------------------------------


def test_fill_amount_must_equal_rounded_notional_and_cents() -> None:
    with pytest.raises(ValidationError):
        LedgerEvent(**_fill_kw(amount=D("300.01")))  # 3 * 100 = 300.00
    with pytest.raises(ValidationError):
        LedgerEvent(**_fill_kw(slippage_cost=D("0.001")))  # off the cent grid


def test_negative_amount_and_annotation_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        LedgerEvent(**_fill_kw(amount=D("-1.00")))
    with pytest.raises(ValidationError):
        LedgerEvent(
            seq=1, asof=ASOF, stage=Stage.CLOSE, sleeve_id="core",
            type=EventType.STOP_TRIGGERED, amount=D("1.00"),
        )  # fmt: skip


def test_buy_fill_with_realized_pnl_and_fee_without_order_id_rejected() -> None:
    with pytest.raises(ValidationError):
        LedgerEvent(**_fill_kw(realized_pnl=D("1.00")))
    with pytest.raises(ValidationError):
        LedgerEvent(
            seq=1, asof=ASOF, stage=Stage.OPEN_FILL, sleeve_id="core",
            type=EventType.FEE, amount=D("1.00"),
        )  # fmt: skip


def test_position_shape_rules() -> None:
    with pytest.raises(ValidationError):
        Position(symbol="SPY", qty=D("0"), cost_basis=D("0.00"), entry_asof=ASOF)
    with pytest.raises(ValidationError):
        Position(symbol="SPY", qty=D("1"), cost_basis=D("1.001"), entry_asof=ASOF)
    with pytest.raises(ValidationError):
        Position(symbol="SPY", qty=D("1"), cost_basis=D("1.00"), entry_asof=None)  # I18


# --- Canonical JSON round trip (D7) -------------------------------------------------------


def test_event_json_round_trip_uses_canonical_text() -> None:
    ev = LedgerEvent(**_fill_kw(price=D("100.00000000"), qty=D("3"), amount=D("300.00")))
    js = ev.model_dump_json()
    assert '"amount":"300.00"' in js and '"price":"100.00000000"' in js and '"qty":"3"' in js
    back = LedgerEvent.model_validate_json(js)
    assert back == ev
    assert back.model_dump_json() == js


def test_snapshot_json_round_trip_is_byte_stable() -> None:
    snap = PortfolioSnapshot(**_snap())
    js = snap.model_dump_json()
    assert PortfolioSnapshot.model_validate_json(js).model_dump_json() == js
    assert '"exposures":{"SPY":"0.5"}' in js


# --- Immutable metadata (F8) ----------------------------------------------------------------


def test_metadata_is_frozen_and_detached_from_the_input() -> None:
    src = {"k": "v"}
    ev = LedgerEvent(
        seq=1, asof=ASOF, stage=Stage.CLOSE, sleeve_id="core",
        type=EventType.STOP_TRIGGERED, metadata=src,
    )  # fmt: skip
    src["k"] = "changed"  # caller's dict is not aliased
    assert dict(ev.metadata) == {"k": "v"}
    with pytest.raises(TypeError):
        ev.metadata["k"] = "HACK"  # type: ignore[index]
    with pytest.raises(TypeError):
        ev.metadata.update({"x": "y"})  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        del ev.metadata["k"]  # type: ignore[attr-defined]
    assert dict(ev.metadata) == {"k": "v"}
    assert copy.deepcopy(ev) == ev
    assert LedgerEvent.model_validate_json(ev.model_dump_json()) == ev
