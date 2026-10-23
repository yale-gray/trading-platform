"""Engine configuration: sleeves, costs, risk (SPEC-003 Interfaces, STRATEGY §4-6)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_FLOOR, Decimal
from types import MappingProxyType
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from tradebot.data.types import AssetClass
from tradebot.portfolio.cost_tiers import CostProfile
from tradebot.portfolio.money import CENT_Q, EQUITY_QTY_QUANTUM, money_context
from tradebot.risk.config import RiskConfig, reject_float

_BPS_CAP = Decimal(10000)  # slip or fee at or above 100% is nonsensical; keeps sell prices > 0


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
    def _bps_in_range(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0 or value >= _BPS_CAP:
            raise ValueError(f"bps rate must be finite, >= 0 and < 10000, got {value}")
        return value


class EngineConfig(BaseModel):
    """Everything the PortfolioEngine needs to start. Validated on construction."""

    model_config = ConfigDict(frozen=True)

    initial_capital: Decimal
    sleeves: tuple[SleeveConfig, ...]
    risk: RiskConfig
    costs: Mapping[AssetClass, CostConfig]
    # Equity quantity grid: 1 = whole shares; default 1e-6 = fractional shares
    # (owner decision 2026-10-22). Crypto stays at 1e-8.
    equity_qty_quantum: Decimal = EQUITY_QTY_QUANTUM
    # Day-trading account (ADR-0001 A21). Both default to the daily behaviour and are left out
    # of the serialised config while at their defaults, so daily configs/states stay byte-stable.
    # ``intraday_flatten``: on the last bar of each session every open position is sold at that
    # bar's close and no BUY is submitted on it, so nothing is held overnight.
    # ``settlement``: "t1" = cash account, SELL proceeds fund BUYs only from the next trading day.
    intraday_flatten: bool = False
    settlement: Literal["none", "t1"] = "none"
    # Tiered costs (ADR-0001 A23); omitted from the serialised config at "flat".
    cost_profile: CostProfile = "flat"

    @model_serializer(mode="wrap")
    def _omit_default_flags(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.intraday_flatten:
            data.pop("intraday_flatten", None)
        if self.settlement == "none":
            data.pop("settlement", None)
        if self.cost_profile == "flat":
            data.pop("cost_profile", None)
        return data

    @field_validator("equity_qty_quantum", mode="before")
    @classmethod
    def _quantum_no_floats(cls, value: Any) -> Any:
        return reject_float(value)

    @field_validator("equity_qty_quantum")
    @classmethod
    def _quantum_in_range(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0 or value > 1:
            raise ValueError(f"equity_qty_quantum must be > 0 and <= 1, got {value}")
        if value.normalize().as_tuple().digits != (1,):
            # The money helpers quantize by exponent, so the grid must be 10**-k.
            raise ValueError(f"equity_qty_quantum must be a power of ten, got {value}")
        return value

    @field_validator("initial_capital", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        return reject_float(value)

    @field_validator("initial_capital")
    @classmethod
    def _capital_positive_on_cents(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError(f"initial_capital must be > 0, got {value}")
        try:
            with money_context():
                on_grid = value == value.quantize(CENT_Q)
        except ArithmeticError:
            on_grid = False
        if not on_grid:
            raise ValueError(f"initial_capital must be on the cent grid (0.01), got {value}")
        return value

    @field_validator("costs", mode="after")
    @classmethod
    def _costs_read_only(
        cls, value: Mapping[AssetClass, CostConfig]
    ) -> Mapping[AssetClass, CostConfig]:
        # Copy into a read-only view so callers cannot mutate costs after validation.
        return MappingProxyType(dict(value))

    @field_serializer("costs")
    def _costs_as_dict(
        self, value: Mapping[AssetClass, CostConfig]
    ) -> dict[AssetClass, CostConfig]:
        return dict(value)

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


def sleeve_deposits(config: EngineConfig) -> dict[str, Decimal]:
    """Split ``initial_capital`` across sleeves by ``target_weight``, in whole cents.

    Largest remainder: each sleeve gets the floor of its exact cent quota, then the
    leftover cents go one each to the sleeves with the largest fractional remainders,
    ties broken by ``sleeve_id`` ascending. The deposits sum exactly to
    ``initial_capital``. Returned in ``config.sleeves`` order.
    """
    with money_context():
        capital_cents = int(config.initial_capital * 100)  # exact: validated on the cent grid
        quotas = {s.sleeve_id: Decimal(capital_cents) * s.target_weight for s in config.sleeves}
        floors = {sid: int(q.to_integral_value(rounding=ROUND_FLOOR)) for sid, q in quotas.items()}
        leftover = capital_cents - sum(floors.values())
        by_remainder = sorted(quotas, key=lambda sid: (-(quotas[sid] - floors[sid]), sid))
        cents = dict(floors)
        for sid in by_remainder[:leftover]:
            cents[sid] += 1
        return {s.sleeve_id: Decimal(cents[s.sleeve_id]) * CENT_Q for s in config.sleeves}


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
