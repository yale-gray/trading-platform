"""Risk limit validation and NaN-weight handling in sizing (STRATEGY §5, audit F13)."""

from decimal import Decimal

import pytest

from tradebot.risk.config import RiskConfig
from tradebot.risk.sizing import plain_weights, vol_scaled_weights

pytestmark = pytest.mark.unit

D = Decimal


def test_risk_defaults_match_strategy_section_5() -> None:
    risk = RiskConfig()
    assert risk.max_asset_fraction == D("0.25")
    assert risk.max_crypto_fraction == D("0.30")
    assert risk.kill_switch_drawdown == D("0.20")
    assert risk.max_pending_bars == 5


@pytest.mark.parametrize(
    "field", ["max_asset_fraction", "max_crypto_fraction", "kill_switch_drawdown"]
)
@pytest.mark.parametrize("bad", ["0", "-0.1", "1.01", "2"])
def test_limit_at_or_below_zero_or_above_one_is_rejected(field: str, bad: str) -> None:
    with pytest.raises(ValueError, match="limit must be > 0 and <= 1"):
        RiskConfig(**{field: D(bad)})


@pytest.mark.parametrize(
    "field", ["max_asset_fraction", "max_crypto_fraction", "kill_switch_drawdown"]
)
def test_limit_of_exactly_one_is_accepted(field: str) -> None:
    assert getattr(RiskConfig(**{field: D("1")}), field) == D("1")


@pytest.mark.parametrize(
    "field", ["max_asset_fraction", "max_crypto_fraction", "kill_switch_drawdown"]
)
def test_float_limit_is_rejected(field: str) -> None:
    with pytest.raises(TypeError):
        RiskConfig(**{field: 0.25})


def test_nan_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        RiskConfig(max_asset_fraction=D("NaN"))


# --- NaN weights raise the documented ValueError, not InvalidOperation -----------------


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-1"])
def test_vol_scaled_weights_rejects_bad_weight_with_value_error(bad: str) -> None:
    with pytest.raises(ValueError, match="finite and >= 0"):
        vol_scaled_weights({"A": D(bad)}, {"A": D("0.01")})


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-1"])
def test_plain_weights_rejects_bad_weight_with_value_error(bad: str) -> None:
    with pytest.raises(ValueError, match="finite and >= 0"):
        plain_weights({"A": D(bad)})


def test_vol_scaled_weights_rejects_nan_vol_with_value_error() -> None:
    with pytest.raises(ValueError, match="vol20"):
        vol_scaled_weights({"A": D("1")}, {"A": D("NaN")})
