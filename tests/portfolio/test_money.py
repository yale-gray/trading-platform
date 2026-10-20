"""Unit tests for ``tradebot.portfolio.money`` (SPEC-003 T1 acceptance criteria 1-10 and §2).

Every expected value below is derived by hand from the spec, not taken from running the code.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import (
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    getcontext,
    localcontext,
)

import numpy as np
import pytest

from tradebot.portfolio.money import (
    AssetClass,
    BadPriceError,
    BadQuantityError,
    BadRatioError,
    MoneyError,
    buy_qty,
    ceil8,
    check_sell_qty,
    floor8,
    floor_qty8,
    floor_shares,
    from_text,
    money_context,
    q2,
    q2_half_up,
    q_price,
    q_ratio,
    q_weight,
    to_price,
    to_ratio,
    to_text,
    to_weight,
)

pytestmark = pytest.mark.unit

D = Decimal


# --- Criterion 1: to_price representation ---------------------------------------------------


def test_to_price_float_100_is_exact_eight_dp() -> None:
    # repr(100.0) == "100.0" -> 100.00000000
    result = to_price(100.0)
    assert result == D("100.00000000")
    assert result.as_tuple().exponent == -8


def test_to_price_uses_repr_not_binary_expansion() -> None:
    # repr(0.1) == "0.1" -> 0.10000000 (the binary expansion would show 0.1000000000000000055...)
    assert to_price(0.1) == D("0.10000000")


def test_to_price_int_seven() -> None:
    assert to_price(7) == D("7.00000000")


# --- Criterion 2: HALF_EVEN ties at 1e-8 -----------------------------------------------------


def test_to_price_tie_rounds_to_even_zero() -> None:
    # 1.000000005: the 9th decimal is a 5 with nothing after; neighbours 0 and 1 -> even is 0
    assert to_price(1.000000005) == D("1.00000000")


def test_to_price_tie_rounds_to_even_two() -> None:
    # 1.000000015: neighbours 1 and 2 -> even is 2
    assert to_price(1.000000015) == D("1.00000002")


# --- Criterion 3: to_price rejections --------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1.0, 0.0])
def test_to_price_rejects_non_finite_or_non_positive(bad: float) -> None:
    with pytest.raises(BadPriceError):
        to_price(bad)


def test_to_price_rejects_value_quantizing_to_zero() -> None:
    # 1e-9 -> 0E-8 after HALF_EVEN quantize to 1e-8
    with pytest.raises(BadPriceError):
        to_price(1e-9)


def test_to_price_rejects_bool() -> None:
    with pytest.raises(TypeError):
        to_price(True)  # type: ignore[arg-type]


def test_to_price_rejects_str() -> None:
    with pytest.raises(TypeError):
        to_price("1")  # type: ignore[arg-type]


def test_to_price_rejects_decimal_input() -> None:
    # Spec: TypeError for "Decimal or other"; Decimal never re-enters through to_price.
    with pytest.raises(TypeError):
        to_price(D("1.5"))  # type: ignore[arg-type]


def test_price_errors_are_money_errors_and_value_errors() -> None:
    assert issubclass(BadPriceError, MoneyError)
    assert issubclass(MoneyError, ValueError)


# --- Criterion 4: weights and ratios ---------------------------------------------------------


def test_to_weight_quantizes_to_six_dp() -> None:
    # 0.6666667 -> 0.666666|7 -> 0.666667 at 1e-6
    assert to_weight(0.6666667) == D("0.666667")


def test_to_ratio_quantizes_to_ten_dp_by_default() -> None:
    # 0.0123456789012 -> 0.0123456789|012 -> 0.0123456789 at 1e-10
    assert to_ratio(0.0123456789012) == D("0.0123456789")


def test_to_ratio_zero_is_allowed() -> None:
    assert to_ratio(0.0) == 0


def test_to_ratio_negative_rejected() -> None:
    with pytest.raises(BadRatioError):
        to_ratio(-0.1)


def test_to_ratio_nan_rejected() -> None:
    with pytest.raises(BadRatioError):
        to_ratio(float("nan"))


def test_to_ratio_inf_rejected() -> None:
    with pytest.raises(BadRatioError):
        to_ratio(float("inf"))


def test_to_ratio_accepts_explicit_quantum() -> None:
    # 0.0123456789012 at 1e-8: 0.01234567|89012 -> 0.01234568
    assert to_ratio(0.0123456789012, D("1E-8")) == D("0.01234568")


def test_to_ratio_rejects_str_and_bool() -> None:
    with pytest.raises(TypeError):
        to_ratio("0.5")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        to_ratio(False)  # type: ignore[arg-type]


def test_ratio_errors_are_money_errors() -> None:
    assert issubclass(BadRatioError, MoneyError)


# --- Criterion 5: fill prices CEILING (buy) / FLOOR (sell) -----------------------------------


def test_buy_fill_price_ceiling_at_five_bps() -> None:
    # open 123.45678901 * 1.0005 = 123.5185174045050 exactly -> ceil to 1e-8 = 123.51851741
    exact = D("123.45678901") * D("1.0005")
    assert exact == D("123.5185174045050")
    assert ceil8(exact) == D("123.51851741")


def test_sell_fill_price_floor_at_five_bps() -> None:
    # open 123.45678901 * 0.9995 = 123.395060615495 exactly -> floor to 1e-8 = 123.39506061
    exact = D("123.45678901") * D("0.9995")
    assert exact == D("123.395060615495")
    assert floor8(exact) == D("123.39506061")


def test_ceil8_already_on_grid_unchanged() -> None:
    assert ceil8(D("100.05")) == D("100.05000000")


def test_ceil8_rounds_up_even_when_next_digit_is_tiny() -> None:
    # 1.000000001 is above 1.00000000 by 1e-9 -> CEILING gives 1.00000001
    assert ceil8(D("1.000000001")) == D("1.00000001")


def test_floor8_rounds_down_even_when_next_digit_is_tiny() -> None:
    assert floor8(D("1.000000009")) == D("1.00000000")


def test_ceil8_and_floor8_are_directional_on_negative_values() -> None:
    # CEILING towards +inf and FLOOR towards -inf; both only apply to prices but the mode is pure.
    assert ceil8(D("-1.000000009")) == D("-1.00000000")
    assert floor8(D("-1.000000001")) == D("-1.00000001")


def test_q_price_passes_rounding_mode_through() -> None:
    assert q_price(D("1.000000005"), ROUND_CEILING) == D("1.00000001")
    assert q_price(D("1.000000005"), ROUND_FLOOR) == D("1.00000000")
    assert q_price(D("1.000000005"), ROUND_HALF_EVEN) == D("1.00000000")


# --- Criterion 6: cents rounding ---------------------------------------------------------------


def test_q2_half_even_tie_down_to_even() -> None:
    # 2.345 -> 2.34 (4 is even)
    assert q2(D("2.345")) == D("2.34")


def test_q2_half_even_tie_up_to_even() -> None:
    # 2.355 -> 2.36 (6 is even)
    assert q2(D("2.355")) == D("2.36")


def test_q2_half_even_negative_tie() -> None:
    assert q2(D("-2.345")) == D("-2.34")


def test_q2_half_up_tie_away_from_zero() -> None:
    assert q2_half_up(D("2.345")) == D("2.35")


def test_q2_half_up_negative_tie_away_from_zero() -> None:
    assert q2_half_up(D("-2.345")) == D("-2.35")


def test_fee_example_1234_56_at_10_bps() -> None:
    # 1234.56 * 10 / 10000 = 1.23456 -> 1.23
    assert q2_half_up(D("1234.56") * D("10") / D("10000")) == D("1.23")


def test_fee_example_100_05_at_10_bps_is_not_a_tie() -> None:
    # 100.05 * 10 / 10000 = 0.10005 exactly; cents digit after is 0 -> 0.10 (not a half-cent case)
    assert q2_half_up(D("100.05") * D("10") / D("10000")) == D("0.10")


def test_fee_half_up_tie_at_half_cent_rounds_up() -> None:
    # 0.005 is an exact cents tie: HALF_UP -> 0.01, HALF_EVEN would give 0.00
    assert q2_half_up(D("0.005")) == D("0.01")
    assert q2(D("0.005")) == D("0.00")


def test_q2_result_has_cent_exponent() -> None:
    assert q2(D("7")).as_tuple().exponent == -2


def test_q2_helpers_use_explicit_rounding_not_ambient() -> None:
    with localcontext() as ctx:
        ctx.rounding = ROUND_DOWN
        assert q2(D("2.345")) == D("2.34")
        assert q2_half_up(D("2.345")) == D("2.35")


# --- Criterion 7: whole shares and 8-dp crypto -----------------------------------------------


def test_floor_shares_truncates_toward_zero() -> None:
    result = floor_shares(D("12.9999"))
    assert result == D("12")
    assert result.as_tuple().exponent == 0


def test_floor_shares_sub_one_is_zero() -> None:
    assert floor_shares(D("0.99")) == D("0")


def test_floor_shares_sizing_example() -> None:
    # 5000.00 / 123.51851741 = 40.4799... -> 40
    assert floor_shares(D("5000.00") / D("123.51851741")) == D("40")


def test_floor_shares_rejects_negative() -> None:
    with pytest.raises(BadQuantityError):
        floor_shares(D("-1"))


def test_floor_qty8_truncates_at_eight_dp() -> None:
    # 0.123456789 -> 0.12345678|9 -> 0.12345678
    assert floor_qty8(D("0.123456789")) == D("0.12345678")


def test_floor_qty8_rejects_negative() -> None:
    with pytest.raises(BadQuantityError):
        floor_qty8(D("-0.1"))


def test_buy_qty_equity_dispatches_to_whole_shares() -> None:
    assert buy_qty(D("40.99"), AssetClass.EQUITY) == D("40")


def test_buy_qty_crypto_dispatches_to_eight_dp() -> None:
    assert buy_qty(D("0.123456789"), AssetClass.CRYPTO) == D("0.12345678")


def test_buy_qty_rejects_negative_for_both_classes() -> None:
    with pytest.raises(BadQuantityError):
        buy_qty(D("-1"), AssetClass.EQUITY)
    with pytest.raises(BadQuantityError):
        buy_qty(D("-0.5"), AssetClass.CRYPTO)


# --- Criterion 8: sell quantities are checked, never rounded ---------------------------------


def test_check_sell_qty_equity_integral_unchanged() -> None:
    assert check_sell_qty(D("3"), AssetClass.EQUITY) == D("3")


def test_check_sell_qty_equity_fractional_rejected() -> None:
    with pytest.raises(BadQuantityError):
        check_sell_qty(D("3.5"), AssetClass.EQUITY)


def test_check_sell_qty_crypto_smallest_grid_step_unchanged() -> None:
    assert check_sell_qty(D("0.00000001"), AssetClass.CRYPTO) == D("0.00000001")


def test_check_sell_qty_crypto_below_grid_rejected() -> None:
    with pytest.raises(BadQuantityError):
        check_sell_qty(D("0.000000001"), AssetClass.CRYPTO)


@pytest.mark.parametrize("qty", [D("0"), D("-1"), D("NaN"), D("Infinity")])
def test_check_sell_qty_rejects_non_positive_and_non_finite(qty: Decimal) -> None:
    with pytest.raises(BadQuantityError):
        check_sell_qty(qty, AssetClass.CRYPTO)


def test_check_sell_qty_preserves_exponent_of_on_grid_input() -> None:
    qty = D("3.00000000")
    assert check_sell_qty(qty, AssetClass.CRYPTO).as_tuple().exponent == -8


# --- Criterion 9: text persistence -----------------------------------------------------------


def test_to_text_keeps_trailing_zeros() -> None:
    assert to_text(D("100.00000000")) == "100.00000000"


def test_from_text_zero_with_exponent() -> None:
    assert from_text("0E-8") == 0


@pytest.mark.parametrize(
    "value",
    [
        D("100.00000000"),
        D("0.10000000"),
        D("0E-8"),
        D("40"),
        D("-2.34"),
        D("123.51851741"),
        D("0.00000001"),
        D("1E+3"),
    ],
)
def test_text_round_trip_preserves_value_and_exponent(value: Decimal) -> None:
    back = from_text(to_text(value))
    assert back == value
    assert back.as_tuple().exponent == value.as_tuple().exponent


def test_to_text_rejects_float() -> None:
    with pytest.raises(TypeError):
        to_text(1.5)  # type: ignore[arg-type]


def test_from_text_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        from_text(D("1"))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-inf", "sNaN"])
def test_from_text_rejects_non_finite(bad: str) -> None:
    with pytest.raises(MoneyError):
        from_text(bad)


def test_from_text_rejects_garbage() -> None:
    with pytest.raises(MoneyError):
        from_text("abc")


# --- Criterion 10: ambient context independence ----------------------------------------------


def test_q2_ignores_ambient_precision() -> None:
    # The 8-digit result cannot be quantized under prec 4; money_context must use 28 instead.
    with localcontext() as ctx:
        ctx.prec = 4
        assert getcontext().prec == 4
        assert q2(D("123456.785")) == D("123456.78")


def test_q2_ignores_ambient_rounding_mode() -> None:
    with localcontext() as ctx:
        ctx.rounding = ROUND_DOWN
        assert q2(D("123456.785")) == D("123456.78")
        assert q2(D("2.355")) == D("2.36")


@pytest.mark.parametrize("prec", [1, 4, 6])
def test_price_conversion_ignores_ambient_precision(prec: int) -> None:
    with localcontext() as ctx:
        ctx.prec = prec
        assert to_price(100.0) == D("100.00000000")
        assert ceil8(D("123.5185174045050")) == D("123.51851741")
        assert floor8(D("123.395060615495")) == D("123.39506061")


def test_ceiling_and_floor_ignore_ambient_rounding() -> None:
    with localcontext() as ctx:
        ctx.rounding = ROUND_FLOOR
        assert ceil8(D("1.000000001")) == D("1.00000001")
    with localcontext() as ctx:
        ctx.rounding = ROUND_CEILING
        assert floor8(D("1.000000009")) == D("1.00000000")


def test_money_context_sets_prec_28_and_restores() -> None:
    with localcontext() as outer:
        outer.prec = 5
        with money_context():
            assert getcontext().prec == 28
        assert getcontext().prec == 5


def test_q_ratio_and_q_weight_half_even() -> None:
    # RATIO_Q 1e-10: 0.00000000005 is a tie between 0.0000000000 and 0.0000000001 -> even 0
    assert q_ratio(D("0.00000000005")) == D("0E-10")
    # 0.00000000015 is a tie between ...1 and ...2 -> even 2
    assert q_ratio(D("0.00000000015")) == D("0.0000000002")
    # WEIGHT_Q 1e-6: 0.1234565 tie between 0.123456 and 0.123457 -> even 6
    assert q_weight(D("0.1234565")) == D("0.123456")


# --- Parametrized rounding table (spec §2) ----------------------------------------------------


@pytest.mark.parametrize(
    ("func", "value", "expected"),
    [
        # Price: buy CEILING, sell FLOOR at 1e-8
        (ceil8, D("0.100000001"), D("0.10000001")),
        (floor8, D("0.100000009"), D("0.10000000")),
        # Cents HALF_EVEN ties
        (q2, D("0.125"), D("0.12")),
        (q2, D("0.135"), D("0.14")),
        # Fee HALF_UP ties (away from zero)
        (q2_half_up, D("0.125"), D("0.13")),
        (q2_half_up, D("-0.125"), D("-0.13")),
        # Whole-share DOWN
        (floor_shares, D("7.999"), D("7")),
        (floor_shares, D("7"), D("7")),
        # Crypto 1e-8 DOWN
        (floor_qty8, D("1.999999999"), D("1.99999999")),
        (floor_qty8, D("0.000000019"), D("0.00000001")),
    ],
)
def test_rounding_table(
    func: Callable[[Decimal], Decimal], value: Decimal, expected: Decimal
) -> None:
    assert func(value) == expected


# --- Error hierarchy --------------------------------------------------------------------------


def test_quantity_error_is_money_error() -> None:
    assert issubclass(BadQuantityError, MoneyError)


# === Audit fixes (003-T1-money-audit F1-F5, G1) ================================================
# Expected values are derived by hand from the SPEC-003 §2 quanta and rounding modes.


def _exp(d: Decimal) -> int:
    exp = d.as_tuple().exponent
    assert isinstance(exp, int)
    return exp


# --- G1: ambient-context independence for every public helper --------------------------------

_CALLS: list[tuple[str, Callable[[], Decimal], Decimal, int]] = [
    # (id, call, expected value, expected exponent)
    ("to_price", lambda: to_price(1.000000015), D("1.00000002"), -8),
    ("to_price_tie0", lambda: to_price(1.000000005), D("1.00000000"), -8),
    ("to_price_int", lambda: to_price(7), D("7.00000000"), -8),
    ("to_ratio", lambda: to_ratio(0.0123456789012), D("0.0123456789"), -10),
    ("to_ratio_tie", lambda: to_ratio(0.00000000015), D("0.0000000002"), -10),
    ("to_weight", lambda: to_weight(0.1234565), D("0.123456"), -6),
    ("q_price_ceil", lambda: q_price(D("1.000000001"), ROUND_CEILING), D("1.00000001"), -8),
    ("ceil8", lambda: ceil8(D("123.5185174045050")), D("123.51851741"), -8),
    ("floor8", lambda: floor8(D("123.395060615495")), D("123.39506061"), -8),
    ("q2_tie_even", lambda: q2(D("2.345")), D("2.34"), -2),
    ("q2_tie_up_even", lambda: q2(D("2.355")), D("2.36"), -2),
    ("q2_half_up", lambda: q2_half_up(D("2.345")), D("2.35"), -2),
    ("q2_big", lambda: q2(D("123456.785")), D("123456.78"), -2),
    ("floor_shares", lambda: floor_shares(D("12.9999")), D("12"), 0),
    ("floor_qty8", lambda: floor_qty8(D("0.123456789")), D("0.12345678"), -8),
    ("buy_qty_equity", lambda: buy_qty(D("40.99"), AssetClass.EQUITY), D("40"), 0),
    ("buy_qty_crypto", lambda: buy_qty(D("0.123456789"), AssetClass.CRYPTO), D("0.12345678"), -8),
    ("check_sell_qty", lambda: check_sell_qty(D("3"), AssetClass.EQUITY), D("3"), 0),
    ("q_ratio", lambda: q_ratio(D("0.00000000015")), D("0.0000000002"), -10),
    ("q_weight", lambda: q_weight(D("0.1234565")), D("0.123456"), -6),
    ("from_text", lambda: from_text("0E-8"), D("0E-8"), -8),
]


_TEXT_CALLS: list[tuple[str, Callable[[], str], str]] = [
    ("to_text_e_plus", lambda: to_text(D("1E+3")), "1E+3"),
    ("to_text_zero_e", lambda: to_text(D("0E-10")), "0E-10"),
    ("to_text_plain", lambda: to_text(D("12.50")), "12.50"),
    ("to_text_neg_zero", lambda: to_text(D("-0E-8")), "0E-8"),
]


def _ambient_low_precision(ctx: object) -> None:
    ctx.prec = 3  # type: ignore[attr-defined]


def _ambient_small_emax(ctx: object) -> None:
    ctx.Emax = 3  # type: ignore[attr-defined]
    ctx.Emin = -3  # type: ignore[attr-defined]


def _ambient_inexact_trapped(ctx: object) -> None:
    ctx.traps[Inexact] = True  # type: ignore[attr-defined]
    ctx.traps[Rounded] = True  # type: ignore[attr-defined]


def _ambient_invalid_untrapped(ctx: object) -> None:
    ctx.traps[InvalidOperation] = False  # type: ignore[attr-defined]


def _ambient_rounding(mode: str) -> Callable[[object], None]:
    def setter(ctx: object) -> None:
        ctx.rounding = mode  # type: ignore[attr-defined]

    return setter


def _ambient_lower_e(ctx: object) -> None:
    ctx.capitals = 0  # type: ignore[attr-defined]


_AMBIENTS: list[tuple[str, Callable[[object], None]]] = [
    ("capitals0", _ambient_lower_e),
    ("prec3", _ambient_low_precision),
    ("small_emax", _ambient_small_emax),
    ("inexact_trapped", _ambient_inexact_trapped),
    ("invalid_untrapped", _ambient_invalid_untrapped),
    *[
        (f"rounding_{mode}", _ambient_rounding(mode))
        for mode in (
            ROUND_UP,
            ROUND_DOWN,
            ROUND_CEILING,
            ROUND_FLOOR,
            ROUND_HALF_UP,
            ROUND_HALF_DOWN,
            ROUND_HALF_EVEN,
            ROUND_05UP,
        )
    ],
]


@pytest.mark.parametrize("ambient", [a[1] for a in _AMBIENTS], ids=[a[0] for a in _AMBIENTS])
@pytest.mark.parametrize(
    ("call", "expected", "exponent"), [c[1:] for c in _CALLS], ids=[c[0] for c in _CALLS]
)
def test_every_helper_ignores_ambient_context(
    call: Callable[[], Decimal],
    expected: Decimal,
    exponent: int,
    ambient: Callable[[object], None],
) -> None:
    with localcontext() as ctx:
        ambient(ctx)
        result = call()
    assert result == expected
    assert _exp(result) == exponent


@pytest.mark.parametrize("ambient", [a[1] for a in _AMBIENTS], ids=[a[0] for a in _AMBIENTS])
@pytest.mark.parametrize(
    ("call", "expected"), [c[1:] for c in _TEXT_CALLS], ids=[c[0] for c in _TEXT_CALLS]
)
def test_to_text_ignores_ambient_context(
    call: Callable[[], str], expected: str, ambient: Callable[[object], None]
) -> None:
    with localcontext() as ctx:
        ambient(ctx)
        assert call() == expected


@pytest.mark.parametrize("text", ["1E+3", "0E-10", "12.50", "1.5E-7"])
def test_to_text_from_text_round_trip_under_capitals0(text: str) -> None:
    with localcontext() as ctx:
        ctx.capitals = 0
        value = from_text(text)
        out = to_text(value)
        assert out == text
        back = from_text(out)
    assert back == value
    assert _exp(back) == _exp(value)


@pytest.mark.parametrize("ambient", [a[1] for a in _AMBIENTS], ids=[a[0] for a in _AMBIENTS])
def test_error_paths_ignore_ambient_context(ambient: Callable[[object], None]) -> None:
    with localcontext() as ctx:
        ambient(ctx)
        with pytest.raises(BadPriceError):
            to_price(1e30)  # 39 digits needed > prec 28: overflow, not a silent NaN
        with pytest.raises(BadPriceError):
            to_price(1e-9)
        with pytest.raises(MoneyError):
            q2(D("1E+30"))
        with pytest.raises(BadQuantityError):
            check_sell_qty(D("1E+30"), AssetClass.EQUITY)
        with pytest.raises(BadQuantityError):
            check_sell_qty(D("3.5"), AssetClass.EQUITY)


def test_ambient_context_is_left_untouched() -> None:
    with localcontext() as ctx:
        ctx.prec = 5
        ctx.rounding = ROUND_FLOOR
        q2(D("1.005"))
        to_price(1.5)
        assert ctx.prec == 5
        assert ctx.rounding == ROUND_FLOOR


def test_money_context_sets_explicit_context_regardless_of_ambient() -> None:
    with localcontext() as outer:
        outer.prec = 5
        outer.rounding = ROUND_FLOOR
        outer.Emax = 10
        with money_context():
            ctx = getcontext()
            assert ctx.prec == 28
            assert ctx.rounding == ROUND_HALF_EVEN
            assert ctx.Emax == 999999
            assert ctx.Emin == -999999
            assert ctx.traps[InvalidOperation]
            assert ctx.traps[Overflow]
            assert not ctx.traps[Inexact]
        assert outer.prec == 5


# --- F1: numpy scalars -----------------------------------------------------------------------


def test_to_price_numpy_float64() -> None:
    result = to_price(np.float64(1.5))
    assert result == D("1.50000000")
    assert _exp(result) == -8


def test_to_price_numpy_float32_exactly_representable() -> None:
    # 0.25 is exact in binary32, so float() gives 0.25 -> 0.25000000
    result = to_price(np.float32(0.25))
    assert result == D("0.25000000")
    assert _exp(result) == -8


def test_to_price_numpy_int64() -> None:
    result = to_price(np.int64(7))
    assert result == D("7.00000000")
    assert _exp(result) == -8


def test_to_ratio_numpy_scalars() -> None:
    r64 = to_ratio(np.float64(0.5))
    assert r64 == D("0.5000000000")
    assert _exp(r64) == -10
    r32 = to_ratio(np.float32(0.5))
    assert r32 == D("0.5000000000")
    assert to_ratio(np.int64(2)) == D("2.0000000000")


def test_to_weight_numpy_scalars() -> None:
    w = to_weight(np.float64(0.6666667))
    assert w == D("0.666667")
    assert _exp(w) == -6
    assert to_weight(np.float32(0.5)) == D("0.500000")
    assert to_weight(np.int64(1)) == D("1.000000")


def test_numpy_non_finite_and_bad_values_rejected() -> None:
    with pytest.raises(BadPriceError):
        to_price(np.float64("nan"))
    with pytest.raises(BadPriceError):
        to_price(np.float32("inf"))
    with pytest.raises(BadPriceError):
        to_price(np.int64(0))
    with pytest.raises(BadRatioError):
        to_ratio(np.float64(-0.1))
    with pytest.raises(TypeError):
        to_price(np.bool_(True))  # type: ignore[arg-type]


def test_numpy_types_still_reject_bool_str_decimal() -> None:
    for bad in (True, "1", D("1"), None, [1.0]):
        with pytest.raises(TypeError):
            to_price(bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            to_ratio(bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            to_weight(bad)  # type: ignore[arg-type]


def test_huge_int_is_overflow_not_raw_error() -> None:
    with pytest.raises(BadPriceError):
        to_price(10**40)
    with pytest.raises(BadRatioError):
        to_ratio(10**5000)


# --- G3/G4: ratio edge cases -------------------------------------------------------------------


def test_to_ratio_quantizing_to_zero_is_allowed() -> None:
    # 1e-12 at 1e-10 -> 0.01 of a unit in the last place -> 0E-10
    result = to_ratio(1e-12)
    assert result == 0
    assert _exp(result) == -10


def test_to_ratio_int_inputs() -> None:
    assert to_ratio(5) == D("5.0000000000")
    with pytest.raises(BadRatioError):
        to_ratio(-3)


# --- Exponent assertions on persisted results --------------------------------------------------


@pytest.mark.parametrize(
    ("result", "exponent"),
    [
        (ceil8(D("1")), -8),
        (floor8(D("1")), -8),
        (ceil8(D("100.05")), -8),
        (q2(D("7")), -2),
        (q2(D("7.123")), -2),
        (q2_half_up(D("7")), -2),
        (q_weight(D("1")), -6),
        (q_ratio(D("1")), -10),
        (to_ratio(0.5), -10),
        (to_weight(0.5), -6),
        (floor_qty8(D("2")), -8),
        (floor_qty8(D("0.123456789")), -8),
        (floor_shares(D("2.5")), 0),
        (floor_shares(D("2.50000000")), 0),
        (buy_qty(D("2.7"), AssetClass.EQUITY), 0),
        (buy_qty(D("2.7"), AssetClass.CRYPTO), -8),
    ],
)
def test_persisted_results_have_canonical_exponent(result: Decimal, exponent: int) -> None:
    assert _exp(result) == exponent


def test_check_sell_qty_equity_keeps_trailing_zero_exponent() -> None:
    # G5: on-grid input is returned unchanged, so "3.00" stays "3.00" (not floor_shares' "3").
    assert _exp(check_sell_qty(D("3.00"), AssetClass.EQUITY)) == -2
    assert _exp(floor_shares(D("3.00"))) == 0


# --- F3: NaN / Infinity / overflow / float into Decimal helpers --------------------------------

_NON_FINITE = [D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity")]


@pytest.mark.parametrize("bad", _NON_FINITE)
@pytest.mark.parametrize("func", [ceil8, floor8, lambda d: q_price(d, ROUND_CEILING)])
def test_price_helpers_reject_non_finite(func: Callable[[Decimal], Decimal], bad: Decimal) -> None:
    with pytest.raises(BadPriceError):
        func(bad)


@pytest.mark.parametrize("bad", _NON_FINITE)
@pytest.mark.parametrize("func", [q2, q2_half_up])
def test_cent_helpers_reject_non_finite(func: Callable[[Decimal], Decimal], bad: Decimal) -> None:
    with pytest.raises(MoneyError):
        func(bad)


@pytest.mark.parametrize("bad", _NON_FINITE)
@pytest.mark.parametrize("func", [floor_shares, floor_qty8])
def test_qty_helpers_reject_non_finite(func: Callable[[Decimal], Decimal], bad: Decimal) -> None:
    with pytest.raises(BadQuantityError):
        func(bad)


@pytest.mark.parametrize("bad", _NON_FINITE)
@pytest.mark.parametrize("func", [q_ratio, q_weight])
def test_ratio_helpers_reject_non_finite(func: Callable[[Decimal], Decimal], bad: Decimal) -> None:
    with pytest.raises(MoneyError):
        func(bad)


def test_non_finite_buy_qty_rejected_for_both_classes() -> None:
    for asset in (AssetClass.EQUITY, AssetClass.CRYPTO):
        with pytest.raises(BadQuantityError):
            buy_qty(D("NaN"), asset)


@pytest.mark.parametrize(
    ("func", "huge", "error"),
    [
        # prec 28: quantizing needs more than 28 digits for these
        (q2, D("1E+27"), MoneyError),  # 1e27 at 2 dp = 30 digits
        (q2_half_up, D("1E+27"), MoneyError),
        (ceil8, D("1E+21"), BadPriceError),  # 1e21 at 8 dp = 30 digits
        (floor8, D("1E+21"), BadPriceError),
        (floor_shares, D("1E+29"), BadQuantityError),  # 30 digits
        (floor_qty8, D("1E+21"), BadQuantityError),
        (q_ratio, D("1E+19"), MoneyError),  # 1e19 at 10 dp = 30 digits
        (q_weight, D("1E+23"), MoneyError),
    ],
)
def test_overflow_raises_money_error_not_invalid_operation(
    func: Callable[[Decimal], Decimal], huge: Decimal, error: type[MoneyError]
) -> None:
    with pytest.raises(error) as info:
        func(huge)
    assert not isinstance(info.value, InvalidOperation)


def test_check_sell_qty_overflow_is_bad_quantity_error() -> None:
    with pytest.raises(BadQuantityError):
        check_sell_qty(D("1E+30"), AssetClass.EQUITY)


def test_just_below_overflow_still_works() -> None:
    # 27 digits + 2 dp = 29 > 28 overflows, but 26 integer digits + 2 dp = 28 is fine.
    assert q2(D("1E+25")) == D("10000000000000000000000000.00")


@pytest.mark.parametrize(
    "func", [ceil8, floor8, q2, q2_half_up, floor_shares, floor_qty8, q_ratio, q_weight]
)
def test_float_into_decimal_helper_is_type_error(func: Callable[[Decimal], Decimal]) -> None:
    with pytest.raises(TypeError):
        func(1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        func(1)  # type: ignore[arg-type]


def test_float_into_other_decimal_helpers_is_type_error() -> None:
    with pytest.raises(TypeError):
        q_price(1.5, ROUND_CEILING)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        check_sell_qty(3.0, AssetClass.EQUITY)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        buy_qty(3.0, AssetClass.EQUITY)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        buy_qty(3.0, AssetClass.CRYPTO)  # type: ignore[arg-type]


# --- F4: negative zero ----------------------------------------------------------------------------


def test_q2_negative_tiny_is_positive_zero() -> None:
    result = q2(D("-0.001"))
    assert result == D("0.00")
    assert not result.is_signed()
    assert _exp(result) == -2
    assert to_text(result) == "0.00"


def test_q2_half_up_negative_tiny_is_positive_zero() -> None:
    result = q2_half_up(D("-0.004"))
    assert not result.is_signed()
    assert to_text(result) == "0.00"


def test_q2_negative_zero_input_is_positive_zero() -> None:
    assert to_text(q2(D("-0"))) == "0.00"


def test_to_ratio_negative_zero_is_positive_zero() -> None:
    result = to_ratio(-0.0)
    assert result == D("0E-10")
    assert not result.is_signed()
    assert to_text(result) == "0E-10"


def test_to_weight_negative_zero_is_positive_zero() -> None:
    assert to_text(to_weight(-0.0)) == "0.000000"
    assert to_text(to_weight(np.float64(-0.0))) == "0.000000"


def test_q_ratio_q_weight_negative_tiny_is_positive_zero() -> None:
    assert to_text(q_ratio(D("-0.00000000001"))) == "0E-10"
    assert to_text(q_weight(D("-0.0000001"))) == "0.000000"


def test_price_helpers_negative_tiny_is_positive_zero() -> None:
    # CEILING of -1e-9 at 1e-8 is -0 -> +0; FLOOR of +1e-9 is +0
    assert to_text(ceil8(D("-0.000000001"))) == "0E-8"
    assert to_text(floor8(D("0.000000001"))) == "0E-8"


def test_floor_shares_negative_zero_is_positive_zero() -> None:
    result = floor_shares(D("-0"))
    assert not result.is_signed()
    assert to_text(result) == "0"
    assert to_text(floor_qty8(D("-0.000"))) == "0E-8"


def test_to_text_normalizes_negative_zero() -> None:
    assert to_text(D("-0.00")) == "0.00"
    assert to_text(D("-0E-10")) == "0E-10"
    assert to_text(D("-2.34")) == "-2.34"


def test_from_text_normalizes_negative_zero() -> None:
    result = from_text("-0.00")
    assert not result.is_signed()
    assert _exp(result) == -2


# --- F5: strict from_text ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        " 1.5",
        "1.5 ",
        " 1.5 ",
        "1.5\n",
        "\t1",
        "1_0",
        "1_000.5",
        "NaN",
        "nan",
        "Infinity",
        "-Infinity",
        "inf",
        "+1",
        "+1.5",
        "1.5E+3x",
        "1e3",  # lowercase exponent is not what str(Decimal) emits
        "1E3",  # str(Decimal) always writes the exponent sign
        ".5",
        "5.",
        "",
        "-",
        "1.2.3",
        "١٢",  # Arabic-indic digits parse in Decimal but are not canonical
    ],
)
def test_from_text_strict_rejections(bad: str) -> None:
    with pytest.raises(MoneyError):
        from_text(bad)


@pytest.mark.parametrize(
    ("text", "value", "exponent"),
    [
        ("0E-8", D("0"), -8),
        ("0.00", D("0"), -2),
        ("100.00000000", D("100"), -8),
        ("-2.34", D("-2.34"), -2),
        ("40", D("40"), 0),
        ("1E+3", D("1000"), 3),
        ("1.23E+5", D("123000"), 3),
        ("1.5E-7", D("0.00000015"), -8),
    ],
)
def test_from_text_accepts_canonical_forms(text: str, value: Decimal, exponent: int) -> None:
    result = from_text(text)
    assert result == value
    assert _exp(result) == exponent
    assert to_text(result) == text


def test_from_text_unrepresentable_exponent_is_money_error() -> None:
    with pytest.raises(MoneyError):
        from_text("1E+99999999999999999999")
