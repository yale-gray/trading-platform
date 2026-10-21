"""Engine config examples (STRATEGY §4-6, audit F13). Expected values are hand-derived."""

from decimal import Decimal

import pytest

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import (
    CostConfig,
    EngineConfig,
    SleeveConfig,
    default_config,
    sleeve_deposits,
)
from tradebot.risk.config import RiskConfig

pytestmark = pytest.mark.unit

D = Decimal
_EQUITY = frozenset({AssetClass.EQUITY})
_COSTS = {
    AssetClass.EQUITY: CostConfig(asset_class=AssetClass.EQUITY, fee_bps=D(0), slip_bps=D(5)),
    AssetClass.CRYPTO: CostConfig(asset_class=AssetClass.CRYPTO, fee_bps=D(10), slip_bps=D(10)),
}


def _sleeves(weights: dict[str, str]) -> tuple[SleeveConfig, ...]:
    return tuple(
        SleeveConfig(
            sleeve_id=sid,
            name=sid,
            target_weight=D(w),
            vol_scaled=False,
            allowed_asset_classes=_EQUITY,
        )
        for sid, w in weights.items()
    )


def _engine(capital: str, weights: dict[str, str]) -> EngineConfig:
    return EngineConfig(
        initial_capital=D(capital),
        sleeves=_sleeves(weights),
        risk=RiskConfig(),
        costs=_COSTS,
    )


# --- default_config matches STRATEGY §4-6 -------------------------------------------------


def test_default_config_capital_and_sleeves_match_strategy_section_4() -> None:
    cfg = default_config()
    assert cfg.initial_capital == D("100000.00")
    weights = {s.sleeve_id: s.target_weight for s in cfg.sleeves}
    # §4 table: 50% / 20% / 20% / 10%, in this order.
    assert weights == {
        "core_momentum": D("0.50"),
        "mean_reversion": D("0.20"),
        "crypto_trend": D("0.20"),
        "cash": D("0.10"),
    }
    assert sum(weights.values(), D(0)) == 1
    vol_scaled = {s.sleeve_id: s.vol_scaled for s in cfg.sleeves}
    assert vol_scaled == {
        "core_momentum": True,
        "mean_reversion": True,
        "crypto_trend": True,
        "cash": False,
    }
    allowed = {s.sleeve_id: s.allowed_asset_classes for s in cfg.sleeves}
    assert allowed["core_momentum"] == frozenset({AssetClass.EQUITY})
    assert allowed["mean_reversion"] == frozenset({AssetClass.EQUITY})
    assert allowed["crypto_trend"] == frozenset({AssetClass.CRYPTO})
    assert allowed["cash"] == frozenset()


def test_default_config_costs_match_strategy_section_6() -> None:
    cfg = default_config()
    # §6: stocks/ETFs $0 commission and 5 bps slippage; crypto 10 bps fee and 10 bps slippage.
    assert cfg.costs[AssetClass.EQUITY].fee_bps == D("0")
    assert cfg.costs[AssetClass.EQUITY].slip_bps == D("5")
    assert cfg.costs[AssetClass.CRYPTO].fee_bps == D("10")
    assert cfg.costs[AssetClass.CRYPTO].slip_bps == D("10")


def test_default_config_risk_matches_strategy_section_5() -> None:
    risk = default_config().risk
    assert risk.max_asset_fraction == D("0.25")  # 25% of equity per asset
    assert risk.max_crypto_fraction == D("0.30")  # 30% of equity in crypto
    assert risk.kill_switch_drawdown == D("0.20")  # 20% drawdown from peak


# --- validation -------------------------------------------------------------------------


def test_weights_summing_to_less_than_one_are_rejected() -> None:
    with pytest.raises(ValueError, match="sum to exactly 1"):
        _engine("100000.00", {"a": "0.5", "b": "0.2", "c": "0.2", "d": "0.0999"})


def test_weights_summing_to_more_than_one_are_rejected() -> None:
    with pytest.raises(ValueError, match="sum to exactly 1"):
        _engine("100000.00", {"a": "0.5", "b": "0.2", "c": "0.2", "d": "0.11"})


def test_float_capital_is_rejected() -> None:
    with pytest.raises(TypeError):
        EngineConfig(
            initial_capital=100000.0,  # type: ignore[arg-type]
            sleeves=_sleeves({"a": "1"}),
            risk=RiskConfig(),
            costs=_COSTS,
        )


def test_float_target_weight_is_rejected() -> None:
    with pytest.raises(TypeError):
        SleeveConfig(
            sleeve_id="a",
            name="a",
            target_weight=0.5,  # type: ignore[arg-type]
            vol_scaled=False,
            allowed_asset_classes=_EQUITY,
        )


def test_capital_off_the_cent_grid_is_rejected() -> None:
    with pytest.raises(ValueError, match="cent grid"):
        _engine("100000.001", {"a": "1"})


def test_capital_on_the_cent_grid_is_accepted() -> None:
    assert _engine("100000.01", {"a": "1"}).initial_capital == D("100000.01")


@pytest.mark.parametrize("field", ["fee_bps", "slip_bps"])
def test_bps_at_or_above_10000_is_rejected(field: str) -> None:
    # At 10000 bps a sell fill is open * (1 - 1) = 0, which the ledger cannot accept.
    with pytest.raises(ValueError, match="< 10000"):
        CostConfig(
            asset_class=AssetClass.EQUITY, **{"fee_bps": D(0), "slip_bps": D(0), field: D(10000)}
        )


def test_slip_bps_just_below_10000_is_accepted() -> None:
    cost = CostConfig(asset_class=AssetClass.EQUITY, fee_bps=D(0), slip_bps=D("9999.99"))
    assert cost.slip_bps == D("9999.99")


def test_negative_bps_is_rejected() -> None:
    with pytest.raises(ValueError):
        CostConfig(asset_class=AssetClass.EQUITY, fee_bps=D(0), slip_bps=D("-1"))


# --- sleeve_deposits (largest remainder) -----------------------------------------------


def test_sleeve_deposits_default_config_splits_exactly() -> None:
    # 100000.00 x 0.50/0.20/0.20/0.10 = 50000 / 20000 / 20000 / 10000, no leftover cents.
    deposits = sleeve_deposits(default_config())
    assert deposits == {
        "core_momentum": D("50000.00"),
        "mean_reversion": D("20000.00"),
        "crypto_trend": D("20000.00"),
        "cash": D("10000.00"),
    }


def test_sleeve_deposits_hand_check_100000_01() -> None:
    # Capital = 10000001 cents. Quotas: 5000000.5 / 2000000.2 / 2000000.2 / 1000000.1.
    # Floors sum to 10000000, leaving 1 cent. Largest remainder is .5 (core_momentum).
    # Deposits: 50000.01 / 20000.00 / 20000.00 / 10000.00, which sum to 100000.01.
    cfg = default_config().model_copy(update={"initial_capital": D("100000.01")})
    deposits = sleeve_deposits(cfg)
    assert deposits["core_momentum"] == D("50000.01")
    assert deposits["mean_reversion"] == D("20000.00")
    assert deposits["crypto_trend"] == D("20000.00")
    assert deposits["cash"] == D("10000.00")
    assert sum(deposits.values(), D(0)) == D("100000.01")


def test_sleeve_deposits_ties_break_by_sleeve_id() -> None:
    # 2 cents over four equal 25% sleeves: quotas are 0.5 cent each, remainders all tie.
    # Ties go to the lowest sleeve_ids first: a and b get a cent, c and d get none.
    deposits = sleeve_deposits(
        _engine("0.02", {"d": "0.25", "c": "0.25", "b": "0.25", "a": "0.25"})
    )
    assert deposits == {"d": D("0"), "c": D("0"), "b": D("0.01"), "a": D("0.01")}
    assert sum(deposits.values(), D(0)) == D("0.02")


def test_sleeve_deposits_returns_config_sleeve_order() -> None:
    deposits = sleeve_deposits(_engine("0.01", {"z": "0.5", "a": "0.25", "m": "0.25"}))
    assert list(deposits) == ["z", "a", "m"]
    assert sum(deposits.values(), D(0)) == D("0.01")


# --- costs are read-only -----------------------------------------------------------------


def test_costs_keep_attribute_and_key_lookup_but_reject_writes() -> None:
    cfg = default_config()
    assert cfg.costs[AssetClass.EQUITY].slip_bps == D("5")
    with pytest.raises(TypeError):
        cfg.costs[AssetClass.EQUITY] = _COSTS[AssetClass.CRYPTO]  # type: ignore[index]


def test_costs_do_not_track_later_mutation_of_the_callers_dict() -> None:
    source = dict(_COSTS)
    cfg = EngineConfig(
        initial_capital=D("100000.00"),
        sleeves=_sleeves({"a": "1"}),
        risk=RiskConfig(),
        costs=source,
    )
    source[AssetClass.EQUITY] = _COSTS[AssetClass.CRYPTO]
    assert cfg.costs[AssetClass.EQUITY].slip_bps == D("5")


# --- JSON round-trip ---------------------------------------------------------------------


def test_default_config_json_round_trip() -> None:
    cfg = default_config()
    restored = EngineConfig.model_validate_json(cfg.model_dump_json())
    assert restored == cfg
    assert restored.costs[AssetClass.CRYPTO].fee_bps == D("10")
    with pytest.raises(TypeError):
        restored.costs[AssetClass.EQUITY] = _COSTS[AssetClass.CRYPTO]  # type: ignore[index]


def test_default_config_model_dump_round_trip() -> None:
    cfg = default_config()
    restored = EngineConfig.model_validate(cfg.model_dump())
    assert restored == cfg
