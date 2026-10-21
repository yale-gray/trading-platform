"""Ledger, position and snapshot models (spec 003 §9). No floats anywhere (I17).

Every money, quantity, price and ratio field uses :data:`Dec`, a strict Decimal
type: canonical text or ``Decimal`` in, canonical text out, floats rejected.
``Dec`` lives here so ``sim.orders`` can import it without an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, NoReturn

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from tradebot.portfolio.money import from_text, money_context, q2, to_text


def _parse_decimal(v: Any) -> Any:
    """Accept canonical text (strict) or a ``Decimal``; reject floats (and ints)."""
    if isinstance(v, str):
        return from_text(v)
    if isinstance(v, Decimal):
        return v
    raise TypeError(f"expected Decimal or canonical str, got {type(v).__name__}")


Dec = Annotated[
    Decimal,
    BeforeValidator(_parse_decimal),
    PlainSerializer(to_text, return_type=str, when_used="always"),
]


class FrozenDict(dict[str, str]):
    """A ``dict[str, str]`` that rejects every mutation (for metadata on frozen events)."""

    def _blocked(self, *_a: object, **_k: object) -> NoReturn:
        raise TypeError("metadata is immutable")

    __setitem__ = __delitem__ = _blocked  # type: ignore[assignment]
    clear = pop = popitem = setdefault = update = _blocked  # type: ignore[assignment]
    __ior__ = _blocked  # type: ignore[assignment]

    def __reduce__(self) -> tuple[type[FrozenDict], tuple[dict[str, str]]]:
        return (FrozenDict, (dict(self),))

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> FrozenDict:
        return self


class Stage(StrEnum):
    """Point in the bar protocol at which an event was recorded."""

    OPEN_FILL = "OPEN_FILL"
    CLOSE = "CLOSE"
    SUBMIT = "SUBMIT"


class EventType(StrEnum):
    """Ledger event types. The last group are zero-cash annotations."""

    DEPOSIT = "DEPOSIT"
    FILL = "FILL"
    FEE = "FEE"
    CASH_TRANSFER_OUT = "CASH_TRANSFER_OUT"
    CASH_TRANSFER_IN = "CASH_TRANSFER_IN"
    KILL_SWITCH_TRIPPED = "KILL_SWITCH_TRIPPED"
    KILL_SWITCH_RESET = "KILL_SWITCH_RESET"
    REBALANCE_PLANNED = "REBALANCE_PLANNED"
    REBALANCE_INCOMPLETE = "REBALANCE_INCOMPLETE"
    STOP_TRIGGERED = "STOP_TRIGGERED"


ANNOTATION_TYPES = frozenset(
    {
        EventType.KILL_SWITCH_TRIPPED,
        EventType.KILL_SWITCH_RESET,
        EventType.REBALANCE_PLANNED,
        EventType.REBALANCE_INCOMPLETE,
        EventType.STOP_TRIGGERED,
    }
)


class FillSide(StrEnum):
    """Side of a FILL event (kept local so this module does not import sim.orders)."""

    BUY = "BUY"
    SELL = "SELL"


class LedgerEvent(BaseModel):
    """One immutable ledger entry. ``amount`` is cents; its sign is implied by ``type``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    asof: date
    stage: Stage
    sleeve_id: str
    type: EventType
    amount: Dec = Decimal("0.00")
    symbol: str | None = None
    side: FillSide | None = None
    qty: Dec | None = None
    price: Dec | None = None
    order_id: str | None = None
    transfer_id: str | None = None
    slippage_cost: Dec | None = None
    realized_pnl: Dec | None = None
    metadata: Mapping[str, str] = Field(default_factory=FrozenDict)

    @field_validator("metadata", mode="after")
    @classmethod
    def _freeze_metadata(cls, v: Mapping[str, str]) -> Mapping[str, str]:
        return FrozenDict(v)

    @model_validator(mode="after")
    def _check_shape(self) -> LedgerEvent:
        for name in ("amount", "slippage_cost", "realized_pnl"):
            v = getattr(self, name)
            if v is not None and q2(v) != v:
                raise ValueError(f"{name} must be on the cent grid, got {v}")
        if self.type in ANNOTATION_TYPES:
            if self.amount != 0:
                raise ValueError("annotation events carry amount 0")
            return self
        if self.amount < 0:
            raise ValueError("amount must be >= 0 (sign is implied by type)")
        if self.type is EventType.FILL:
            if None in (self.symbol, self.side, self.qty, self.price, self.order_id):
                raise ValueError("FILL needs symbol, side, qty, price, order_id")
            assert self.qty is not None and self.price is not None
            if self.qty <= 0 or self.price <= 0:
                raise ValueError("FILL qty and price must be > 0")
            with money_context():
                expected = q2(self.qty * self.price)
            if self.amount != expected:
                raise ValueError("FILL amount must equal q2(qty * price)")
            if self.side is FillSide.BUY and self.realized_pnl is not None:
                raise ValueError("BUY fills carry no realized_pnl")
        elif self.type is EventType.FEE:
            if self.order_id is None:
                raise ValueError("FEE needs the order_id of its FILL")
        elif self.type in (EventType.CASH_TRANSFER_OUT, EventType.CASH_TRANSFER_IN):
            if self.transfer_id is None:
                raise ValueError("transfers need a transfer_id")
        return self


class Position(BaseModel):
    """Open position. ``cost_basis`` is total cost in cents, excluding fees."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    qty: Dec
    cost_basis: Dec
    entry_asof: date | None
    high_water_close: Dec | None = None
    stop_level: Dec | None = None

    @model_validator(mode="after")
    def _check(self) -> Position:
        if self.qty <= 0:
            raise ValueError("a retained position has qty > 0")
        if self.cost_basis < 0 or q2(self.cost_basis) != self.cost_basis:
            raise ValueError("cost_basis must be non-negative cents")
        if self.entry_asof is None:
            raise ValueError("entry_asof is set whenever qty > 0 (I18)")
        return self


class PositionSnapshot(BaseModel):
    """A marked position inside a sleeve snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    qty: Dec
    mv: Dec
    cost_basis: Dec
    unrealized: Dec
    entry_asof: date | None
    high_water_close: Dec | None = None
    stop_level: Dec | None = None


class SleeveSnapshot(BaseModel):
    """Per-sleeve slice of an equity snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sleeve_id: str
    cash: Dec
    equity: Dec
    weight: Dec
    positions: list[PositionSnapshot]


class PortfolioSnapshot(BaseModel):
    """End-of-bar equity snapshot (spec §9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asof: date
    equity: Dec
    cash: Dec
    drawdown: Dec
    peak_equity: Dec
    killed: bool
    last_seq: int
    sleeves: list[SleeveSnapshot]
    exposures: dict[str, Dec]
    crypto_fraction: Dec
