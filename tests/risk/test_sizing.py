"""Volatility-scaled sizing examples (SPEC-003 section 5, AC 2 and AC 7).

Every expected number is derived by hand in a comment; none is read back from code.
"""

from decimal import Decimal

import pytest

from tradebot.data.types import AssetClass
from tradebot.risk.sizing import (
    VOL_FLOOR,
    plain_weights,
    target_quantity,
    target_value,
    vol_scaled_weights,
)

pytestmark = pytest.mark.unit

D = Decimal
TOL = D("1e-20")


def _close(actual: Decimal, expected: Decimal, tol: Decimal = TOL) -> None:
    assert abs(actual - expected) <= tol, f"{actual} != {expected}"


# --- Inverse-vol weights ---------------------------------------------------------------


def test_ac2_inverse_vol_two_assets_gives_two_thirds_and_one_third() -> None:
    # s_A = 0.5 / 0.01 = 50, s_B = 0.5 / 0.02 = 25, sum = 75.
    # W = 0.5 + 0.5 = 1.0, so w'_A = 1.0 * 50 / 75 = 2/3 and w'_B = 1.0 * 25 / 75 = 1/3.
    out, dropped = vol_scaled_weights(
        {"A": D("0.5"), "B": D("0.5")}, {"A": D("0.01"), "B": D("0.02")}
    )
    assert dropped == ()
    assert out["A"].quantize(D("0.001")) == D("0.667")
    assert out["B"].quantize(D("0.001")) == D("0.333")
    _close(out["A"], D(2) / D(3))
    _close(out["B"], D(1) / D(3))
    _close(sum(out.values(), D(0)), D("1"))
    # Deployed capital: a 30,000 sleeve puts 2/3 * 30000 = 20000 into A and 10000 into B.
    _close(target_value(out["A"], D("30000")), D("20000"), D("1e-15"))
    _close(target_value(out["B"], D("30000")), D("10000"), D("1e-15"))


@pytest.mark.parametrize("vol", ["0", "0.00001", "0.0001"])
def test_vol_at_or_below_floor_is_clamped_to_vol_floor(vol: str) -> None:
    # VOL_FLOOR = 1e-4. sigma_A = 0 (or below/at the floor) is clamped to 1e-4:
    # s_A = 0.5 / 0.0001 = 5000; s_B = 0.5 / 0.01 = 50; sum = 5050.
    # w'_A = 1.0 * 5000 / 5050 = 100/101; w'_B = 50 / 5050 = 1/101.
    assert VOL_FLOOR == D("0.0001")
    out, dropped = vol_scaled_weights({"A": D("0.5"), "B": D("0.5")}, {"A": D(vol), "B": D("0.01")})
    assert dropped == ()
    _close(out["A"], D(100) / D(101))
    _close(out["B"], D(1) / D(101))


def test_no_vol_data_symbol_is_dropped_and_rest_renormalize_to_kept_weight() -> None:
    # A has no vol20 (warm-up): dropped. Kept weights B=0.3, C=0.2, so W = 0.5.
    # s_B = 0.3 / 0.01 = 30; s_C = 0.2 / 0.02 = 10; sum = 40.
    # w'_B = 0.5 * 30 / 40 = 0.375; w'_C = 0.5 * 10 / 40 = 0.125. Total stays 0.5.
    out, dropped = vol_scaled_weights(
        {"A": D("0.5"), "B": D("0.3"), "C": D("0.2")},
        {"A": None, "B": D("0.01"), "C": D("0.02")},
    )
    assert dropped == ("A",)
    assert out == {"B": D("0.375"), "C": D("0.125")}
    _close(sum(out.values(), D(0)), D("0.5"))


def test_single_kept_symbol_keeps_its_weight() -> None:
    # One symbol: w' = W * s / s = W = 0.4, whatever its vol.
    out, dropped = vol_scaled_weights({"A": D("0.4")}, {"A": D("0.03")})
    assert dropped == ()
    _close(out["A"], D("0.4"))

    # B is dropped for missing vol; A is the only kept symbol and keeps 0.4.
    out, dropped = vol_scaled_weights({"A": D("0.4"), "B": D("0.1")}, {"A": D("0.03"), "B": None})
    assert dropped == ("B",)
    _close(out["A"], D("0.4"))
    assert "B" not in out


def test_zero_weight_passes_through_without_needing_vol() -> None:
    # A has weight 0, so its missing vol is irrelevant: it comes back as 0, not dropped.
    # B is the only kept symbol and keeps its weight 1.0.
    out, dropped = vol_scaled_weights({"A": D("0"), "B": D("1.0")}, {"A": None, "B": D("0.02")})
    assert dropped == ()
    assert out == {"A": D("0"), "B": D("1.0")}


def test_all_symbols_without_vol_yield_no_targets() -> None:
    # Everything with w > 0 is dropped, so nothing is sized (W of kept set is empty).
    out, dropped = vol_scaled_weights({"A": D("0.5")}, {"A": None})
    assert out == {}
    assert dropped == ("A",)


@pytest.mark.parametrize(
    ("weights", "vols"),
    [
        ({"A": D("-0.1")}, {"A": D("0.01")}),
        ({"A": D("0.5")}, {"A": D("-0.01")}),
    ],
)
def test_negative_or_non_finite_inputs_are_rejected(
    weights: dict[str, Decimal], vols: dict[str, Decimal]
) -> None:
    with pytest.raises(ValueError):
        vol_scaled_weights(weights, vols)


def test_plain_weights_pass_through_unchanged() -> None:
    assert plain_weights({"B": D("0.7"), "A": D("0.3")}) == {"A": D("0.3"), "B": D("0.7")}


# --- Target value and quantity ---------------------------------------------------------


def test_target_value_is_weight_times_sizing_equity() -> None:
    # 0.5 * 50000 = 25000.
    _close(target_value(D("0.5"), D("50000")), D("25000"))


def test_target_quantity_rounds_equities_down_to_whole_shares() -> None:
    # 25000 / 100 = 250 exactly.
    assert target_quantity(D("25000"), D("100"), AssetClass.EQUITY, D("1")) == D("250")
    # 250.99 / 100 = 2.5099 -> 2 shares.
    assert target_quantity(D("250.99"), D("100"), AssetClass.EQUITY, D("1")) == D("2")
    # 5 / 100 = 0.05 -> 0 shares (sub-share).
    assert target_quantity(D("5"), D("100"), AssetClass.EQUITY, D("1")) == D("0")


def test_target_quantity_fractional_equity_rounds_down_at_1e6() -> None:
    # SPY 764.00, $2,500 target: 2500 / 764 = 3.27225130890... -> 3.272251 (down at 1e-6).
    assert target_quantity(D("2500"), D("764.00"), AssetClass.EQUITY) == D("3.272251")
    # Quantum 0.1: 2500 / 764 -> 3.2 ; quantum 1 -> 3.
    assert target_quantity(D("2500"), D("764"), AssetClass.EQUITY, D("0.1")) == D("3.2")
    assert target_quantity(D("2500"), D("764"), AssetClass.EQUITY, D("1")) == D("3")
    # Crypto is unaffected by the equity quantum.
    assert target_quantity(D("10000"), D("3"), AssetClass.CRYPTO, D("1")) == D("3333.33333333")


def test_target_quantity_rounds_crypto_down_to_eight_decimals() -> None:
    # 10000 / 3 = 3333.3333333333... -> floor to 8 dp = 3333.33333333.
    assert target_quantity(D("10000"), D("3"), AssetClass.CRYPTO) == D("3333.33333333")


def test_target_quantity_clamps_negative_value_to_zero() -> None:
    assert target_quantity(D("-5"), D("100"), AssetClass.EQUITY) == D("0")


def test_target_quantity_rejects_non_positive_reference_price() -> None:
    with pytest.raises(ValueError):
        target_quantity(D("100"), D("0"), AssetClass.EQUITY)
