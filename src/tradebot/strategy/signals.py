"""Signal types produced by :meth:`CompiledStrategy.evaluate` (SPEC-002 interfaces)."""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from tradebot.strategy.schema import StopRequest


class HeldPosition(BaseModel):
    """A position the sleeve holds; built by ``BarDriver`` from ``engine.snapshot()``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    symbol: str
    entry_ts: pd.Timestamp  # entry fill bar = pd.Timestamp(entry_asof, tz="UTC")


class Intent(BaseModel):
    """One symbol's desired state on a decision bar."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    symbol: str
    target: Literal["long", "flat"]
    weight: float  # fraction of SLEEVE capital; a hint (execution may scale down, never up)
    reason: str
    stop: StopRequest | None = None


class SignalSet(BaseModel):
    """Result of one ``evaluate`` call. ``decision=False`` means no opinion (no intents)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    sleeve_id: str
    asof: pd.Timestamp
    decision: bool
    intents: list[Intent]
    diagnostics: dict[str, Any] = Field(default_factory=dict)
