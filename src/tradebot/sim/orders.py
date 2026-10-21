"""Orders, enums and the order state machine (SPEC-003 §3, §9, §10).

Models are frozen and JSON round-trippable: every ``Decimal`` is serialized as
its canonical text (ADR D7) and parsed back strictly, so a round trip is exact.
:func:`transition` is the only sanctioned way to change ``Order.status``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from tradebot.portfolio.models import Dec


class Side(StrEnum):
    """Order side (long-only: SELL closes or trims)."""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    """Order lifecycle states; ``NEW`` is transient and never persisted."""

    NEW = "NEW"
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class OrderOrigin(StrEnum):
    """Who created the order; the source of ``exit_reason`` (ADR D3)."""

    STRATEGY = "STRATEGY"
    STOP = "STOP"
    REBALANCE = "REBALANCE"
    MANUAL = "MANUAL"


TERMINAL_STATES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    }
)

LEGAL_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.PENDING, OrderStatus.REJECTED}),
    OrderStatus.PENDING: frozenset(
        {
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}


class IllegalTransition(ValueError):
    """An order status change that the state machine forbids."""


class OrderReason(StrEnum):
    """Machine-readable reasons for a veto, shrink or reject (SPEC-003 §6)."""

    KILL_SWITCH = "KILL_SWITCH"
    NO_DATA = "NO_DATA"
    NO_VOL_DATA = "NO_VOL_DATA"
    STOP_LOCK = "STOP_LOCK"
    MAX_ASSET_SHRUNK = "MAX_ASSET_SHRUNK"
    MAX_ASSET = "MAX_ASSET"
    MAX_CRYPTO_SHRUNK = "MAX_CRYPTO_SHRUNK"
    MAX_CRYPTO = "MAX_CRYPTO"
    CASH_SHRUNK = "CASH_SHRUNK"
    BELOW_MIN_SIZE = "BELOW_MIN_SIZE"
    SHRUNK_CASH = "SHRUNK_CASH"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"


class OrderRequest(BaseModel):
    """A market-order request (v1 supports market orders only)."""

    model_config = ConfigDict(frozen=True)

    sleeve_id: str
    symbol: str
    side: Side
    qty: Dec
    origin: OrderOrigin = OrderOrigin.STRATEGY

    @model_validator(mode="after")
    def _check_qty(self) -> OrderRequest:
        if not self.qty.is_finite() or self.qty <= 0:
            raise ValueError(f"qty must be finite and > 0, got {self.qty}")
        return self


class Order(BaseModel):
    """An order and its fill outcome. Change status only via :func:`transition`."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    sleeve_id: str
    symbol: str
    side: Side
    origin: OrderOrigin
    qty_requested: Dec
    qty_accepted: Dec | None = None
    qty_filled: Dec = Decimal("0")
    status: OrderStatus = OrderStatus.NEW
    created_asof: date
    ref_price: Dec | None = None
    fill_asof: date | None = None
    fill_price: Dec | None = None
    fee: Dec | None = None
    reason: OrderReason | None = None

    @model_validator(mode="after")
    def _check_quantities(self) -> Order:
        """I14: ``0 <= qty_filled <= qty_accepted <= qty_requested``."""
        if self.qty_requested <= 0:
            raise ValueError("qty_requested must be > 0")
        if self.qty_filled < 0:
            raise ValueError("qty_filled must be >= 0")
        if self.qty_accepted is not None:
            if self.qty_accepted < 0 or self.qty_accepted > self.qty_requested:
                raise ValueError("qty_accepted must be within [0, qty_requested]")
            if self.qty_filled > self.qty_accepted:
                raise ValueError("qty_filled must be <= qty_accepted")
        elif self.qty_filled > 0:
            raise ValueError("qty_filled > 0 requires qty_accepted")
        if self.fill_asof is not None and self.fill_asof <= self.created_asof:
            raise ValueError("I8: fill_asof must be after created_asof")
        if self.status is OrderStatus.FILLED:
            if (
                self.fill_asof is None
                or self.fill_price is None
                or self.fee is None
                or self.qty_filled <= 0
            ):
                raise ValueError("FILLED needs fill_asof, fill_price, fee and qty_filled > 0")
            if self.fill_price <= 0 or self.fee < 0:
                raise ValueError("FILLED needs fill_price > 0 and fee >= 0")
        elif self.qty_filled != 0:
            raise ValueError(f"{self.status} order cannot carry qty_filled > 0")
        return self


_UNSET: Any = object()


def transition(
    order: Order,
    new_status: OrderStatus,
    *,
    qty_accepted: Decimal | None = _UNSET,
    qty_filled: Decimal = _UNSET,
    fill_asof: date | None = _UNSET,
    fill_price: Decimal | None = _UNSET,
    fee: Decimal | None = _UNSET,
    reason: OrderReason | None = _UNSET,
) -> Order:
    """Return a copy of ``order`` moved to ``new_status``.

    Legal edges: ``NEW -> PENDING|REJECTED`` and
    ``PENDING -> FILLED|REJECTED|CANCELLED|EXPIRED``. Anything else, including
    any move out of a terminal state or a self-transition, raises
    :class:`IllegalTransition`. Optional keyword arguments update the matching
    fields in the same step; the result is re-validated (I14 quantities).
    """
    if new_status not in LEGAL_TRANSITIONS[order.status]:
        raise IllegalTransition(f"{order.order_id}: {order.status} -> {new_status}")
    updates: dict[str, Any] = {"status": new_status}
    for key, val in (
        ("qty_accepted", qty_accepted),
        ("qty_filled", qty_filled),
        ("fill_asof", fill_asof),
        ("fill_price", fill_price),
        ("fee", fee),
        ("reason", reason),
    ):
        if val is not _UNSET:
            updates[key] = val
    return Order.model_validate({**order.__dict__, **updates})


class OrderIdGenerator:
    """Deterministic ``O{n:08d}`` ids. ``next_n`` is persisted engine state."""

    def __init__(self, next_n: int = 1) -> None:
        if isinstance(next_n, bool) or not isinstance(next_n, int) or next_n < 1:
            raise ValueError(f"next_n must be an int >= 1, got {next_n!r}")
        self._next_n = next_n

    @property
    def next_n(self) -> int:
        """The counter value the next id will use (persist this)."""
        return self._next_n

    def next_id(self) -> str:
        """Return the next id and advance the counter."""
        oid = f"O{self._next_n:08d}"
        self._next_n += 1
        return oid
