"""Persisted engine state (SPEC-003 section 10, ADR-0001 D7/D8).

``EngineState`` is a pydantic model that round-trips through JSON: every
``Decimal`` is canonical text (``Dec``), dates are ISO strings, collections are
emitted in sorted order, so two identical engines produce byte-identical JSON.
Cash and positions are *derived* (replayed from the ledger); they are stored too
so a restore can verify the replay against the stored values.
"""

from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    field_serializer,
    model_serializer,
)

from tradebot.portfolio.asof import AsOf
from tradebot.portfolio.config import EngineConfig
from tradebot.portfolio.models import Dec, LedgerEvent
from tradebot.portfolio.rebalance import RebalancePlan
from tradebot.sim.orders import Order

STATE_VERSION = 1


class StateCorruptionError(ValueError):
    """Stored or replayed state does not match (I5): the checkpoint cannot be trusted."""


class PositionState(BaseModel):
    """One open position as stored (derived values, checked against the replay)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    qty: Dec
    cost_basis: Dec
    entry_asof: AsOf
    high_water_close: Dec | None = None
    stop_level: Dec | None = None


class EngineState(BaseModel):
    """Everything that must persist between bars so a restore continues identically."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = STATE_VERSION
    config: EngineConfig
    ledger: list[LedgerEvent]
    orders: list[Order]
    pending_waited: dict[str, int]
    next_order_n: int
    next_transfer_n: int
    next_seq: int
    last_processed_asof: AsOf | None
    kill_state: dict[str, Any]
    stop_state: dict[str, Any]
    last_rebalance_month: str | None
    rebalance_plan: RebalancePlan | None
    last_close: dict[str, Dec]
    last_vol20: dict[str, Dec]
    cash: dict[str, Dec]
    positions: dict[str, list[PositionState]]
    # T+1 accounts only: SELL proceeds still unsettled at ``last_processed_asof`` (derived from the
    # ledger; stored so a restore can cross-check). Omitted from the JSON while empty, so daily
    # states are byte-identical to those written before A21.
    unsettled: dict[str, Dec] = {}

    @field_serializer("config")
    def _serialize_config(self, config: EngineConfig) -> dict[str, Any]:
        """Config as JSON with set/dict-ordered fields sorted (byte-stable output)."""
        data = config.model_dump(mode="json")
        for sleeve in data["sleeves"]:
            sleeve["allowed_asset_classes"] = sorted(sleeve["allowed_asset_classes"])
        data["costs"] = dict(sorted(data["costs"].items()))
        return data  # type: ignore[no-any-return]

    @model_serializer(mode="wrap")
    def _omit_empty_unsettled(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.unsettled:
            data.pop("unsettled", None)
        return data

    def to_json(self) -> str:
        """Canonical JSON text of the state."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> EngineState:
        """Inverse of :meth:`to_json`."""
        return cls.model_validate_json(text)
