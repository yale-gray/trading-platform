"""Portfolio-wide risk limits (STRATEGY §5, SPEC-003 §6-7)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def reject_float(value: Any) -> Any:
    """Decimal config values must come in as str/int/Decimal, never float."""
    if isinstance(value, float):
        raise TypeError(f"float {value!r} not allowed; pass a Decimal or a string")
    return value


class RiskConfig(BaseModel):
    """Limits enforced by the risk checks and the kill switch.

    Fractions are of portfolio equity (or of drawdown for the kill switch).
    """

    model_config = ConfigDict(frozen=True)

    max_asset_fraction: Decimal = Decimal("0.25")
    max_crypto_fraction: Decimal = Decimal("0.30")
    kill_switch_drawdown: Decimal = Decimal("0.20")
    max_pending_bars: int = 5

    @field_validator(
        "max_asset_fraction", "max_crypto_fraction", "kill_switch_drawdown", mode="before"
    )
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        return reject_float(value)

    @field_validator("max_asset_fraction", "max_crypto_fraction", "kill_switch_drawdown")
    @classmethod
    def _positive_at_most_one(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0 or value > 1:
            raise ValueError(f"limit must be > 0 and <= 1, got {value}")
        return value

    @field_validator("max_pending_bars")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"max_pending_bars must be >= 1, got {value}")
        return value
