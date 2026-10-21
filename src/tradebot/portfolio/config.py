"""Engine configuration: sleeves, costs, risk (SPEC-003 Interfaces, STRATEGY §4-6)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tradebot.data.types import AssetClass
from tradebot.risk.config import RiskConfig, reject_float


class SleeveConfig(BaseModel):
    """One sleeve of the paper fund: its share of capital and what it may trade."""

    model_config = ConfigDict(frozen=True)

    sleeve_id: str
    name: str
    target_weight: Decimal
    vol_scaled: bool
    trailing_stop_atr_mult: Decimal | None = None  # set by BarDriver from the strategy's stops
    allowed_asset_classes: frozenset[AssetClass]
    min_trade_fraction: Decimal = Decimal("0.005")

    @field_validator("target_weight", "trailing_stop_atr_mult", "min_trade_fraction", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        return reject_float(value)

    @field_validator("sleeve_id")
    @classmethod
    def _non_empty_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sleeve_id must be non-empty")
        return value

    @field_validator("target_weight")
    @classmethod
    def _weight_in_unit_interval(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0 or value > 1:
            raise ValueError(f"target_weight must be in [0, 1], got {value}")
        return value

    @field_validator("trailing_stop_atr_mult")
    @classmethod
    def _positive_mult(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError(f"trailing_stop_atr_mult must be > 0, got {value}")
        return value

    @field_validator("min_trade_fraction")
    @classmethod
    def _min_trade_positive(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0 or value > 1:
            raise ValueError(f"min_trade_fraction must be > 0 and <= 1, got {value}")
        return value


class CostConfig(BaseModel):
    """Simulated costs for one asset class, in basis points (1 bp = 0.01%)."""

    model_config = ConfigDict(frozen=True)

    asset_class: AssetClass
    fee_bps: Decimal
    slip_bps: Decimal

    @field_validator("fee_bps", "slip_bps", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        return reject_float(value)

    @field_validator("fee_bps", "slip_bps")
    @classmethod
    def _non_negative_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError(f"bps rate must be finite and >= 0, got {value}")
        return value


class EngineConfig(BaseModel):
    """Everything the PortfolioEngine needs to start. Validated on construction."""

    model_config = ConfigDict(frozen=True)

    initial_capital: Decimal
    sleeves: tuple[SleeveConfig, ...]
    risk: RiskConfig
    costs: dict[AssetClass, CostConfig]

    @field_validator("initial_capital", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        return reject_float(value)

    @field_validator("initial_capital")
    @classmethod
    def _capital_positive(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError(f"initial_capital must be > 0, got {value}")
        return value

    @model_validator(mode="after")
    def _check_engine(self) -> EngineConfig:
        if not self.sleeves:
            raise ValueError("at least one sleeve is required")
        ids = [s.sleeve_id for s in self.sleeves]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate sleeve_id in {ids}")
        total = sum((s.target_weight for s in self.sleeves), Decimal(0))
        if total != 1:
            raise ValueError(f"sleeve target_weights must sum to exactly 1, got {total}")
        missing = set(AssetClass) - set(self.costs)
        if missing:
            raise ValueError(f"costs missing for asset classes {sorted(c.value for c in missing)}")
        for asset_class, cost in self.costs.items():
            if cost.asset_class != asset_class:
                raise ValueError(f"costs[{asset_class}] has asset_class {cost.asset_class}")
        return self


def default_config() -> EngineConfig:
    """STRATEGY §4-6 starting parameters: 50/20/20/10 sleeves, $100,000 paper fund."""
    equity = frozenset({AssetClass.EQUITY})
    crypto = frozenset({AssetClass.CRYPTO})
    return EngineConfig(
        initial_capital=Decimal("100000.00"),
        sleeves=(
            SleeveConfig(
                sleeve_id="core_momentum",
                name="Core: trend-filtered ETF momentum",
                target_weight=Decimal("0.50"),
                vol_scaled=True,
                allowed_asset_classes=equity,
            ),
            SleeveConfig(
                sleeve_id="mean_reversion",
                name="Mean reversion",
                target_weight=Decimal("0.20"),
                vol_scaled=True,
                allowed_asset_classes=equity,
            ),
            SleeveConfig(
                sleeve_id="crypto_trend",
                name="Crypto trend",
                target_weight=Decimal("0.20"),
                vol_scaled=True,
                allowed_asset_classes=crypto,
            ),
            SleeveConfig(
                sleeve_id="cash",
                name="Cash buffer",
                target_weight=Decimal("0.10"),
                vol_scaled=False,
                allowed_asset_classes=frozenset(),
            ),
        ),
        risk=RiskConfig(),
        costs={
            AssetClass.EQUITY: CostConfig(
                asset_class=AssetClass.EQUITY, fee_bps=Decimal("0"), slip_bps=Decimal("5")
            ),
            AssetClass.CRYPTO: CostConfig(
                asset_class=AssetClass.CRYPTO, fee_bps=Decimal("10"), slip_bps=Decimal("10")
            ),
        },
    )
