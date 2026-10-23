"""Unit tests for devconsole/lib/sandbox.py: the cost sandbox (SPEC-006 AC 9)."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

import pytest

from devconsole.lib.sandbox import compute_trade, parse_decimal
from tradebot.data.types import AssetClass
from tradebot.portfolio.config import default_config
from tradebot.portfolio.money import BadQuantityError

pytestmark = pytest.mark.unit

_BPS = Decimal(10000)
_CFG = default_config().costs


def _hand_buy(asset: AssetClass, qty: Decimal, buy_open: Decimal) -> dict[str, Decimal]:
    """Hand arithmetic from the configured bps, using only Decimal and explicit rounding."""
    slip = _CFG[asset].slip_bps
    fee_bps = _CFG[asset].fee_bps
    fill = (buy_open * (1 + slip / _BPS)).quantize(Decimal("1E-8"), rounding=ROUND_CEILING)
    notional = (qty * fill).quantize(Decimal("0.01"))
    fee = (notional * fee_bps / _BPS).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"fill": fill, "notional": notional, "fee": fee, "cash_out": notional + fee}


def _hand_sell(asset: AssetClass, qty: Decimal, sell_open: Decimal) -> dict[str, Decimal]:
    slip = _CFG[asset].slip_bps
    fee_bps = _CFG[asset].fee_bps
    fill = (sell_open * (1 - slip / _BPS)).quantize(Decimal("1E-8"), rounding=ROUND_FLOOR)
    notional = (qty * fill).quantize(Decimal("0.01"))
    fee = (notional * fee_bps / _BPS).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"fill": fill, "notional": notional, "fee": fee, "cash_in": notional - fee}


# --------------------------------------------------------------------------- equity example


def test_equity_100_shares_100_to_110_pnl_is_989_50() -> None:
    # Config: equity fee 0 bps, slippage 5 bps.
    # Buy fill = 100 * 1.0005 = 100.05; notional 100 * 100.05 = 10005.00; fee 0; cash out 10005.00.
    # Sell fill = 110 * 0.9995 = 109.945; notional 100 * 109.945 = 10994.50; fee 0;
    # cash in 10994.50.
    # P&L = 10994.50 - 10005.00 = 989.50.
    # Costs = slippage 100 * 0.05 = 5.00 on the buy, 100 * 0.055 = 5.50 on the sell -> 10.50.
    t = compute_trade(AssetClass.EQUITY, "100", "100", "110")

    assert t.qty == Decimal("100")
    assert t.buy_fill == Decimal("100.05000000")
    assert t.buy_notional == Decimal("10005.00")
    assert t.buy_fee == Decimal("0.00")
    assert t.buy_cash_out == Decimal("10005.00")
    assert t.sell_fill == Decimal("109.94500000")
    assert t.sell_notional == Decimal("10994.50")
    assert t.sell_cash_in == Decimal("10994.50")
    assert t.round_trip_pnl == Decimal("989.50")
    assert t.total_costs == Decimal("10.50")


def test_equity_example_matches_hand_arithmetic_from_config() -> None:
    qty, buy, sell = Decimal("100"), Decimal("100"), Decimal("110")
    b = _hand_buy(AssetClass.EQUITY, qty, buy)
    s = _hand_sell(AssetClass.EQUITY, qty, sell)

    t = compute_trade(AssetClass.EQUITY, qty, buy, sell)

    assert t.buy_fill == b["fill"]
    assert t.buy_cash_out == b["cash_out"]
    assert t.sell_fill == s["fill"]
    assert t.sell_cash_in == s["cash_in"]
    assert t.round_trip_pnl == s["cash_in"] - b["cash_out"]


def test_equity_pnl_percent_is_quantized_to_ten_places() -> None:
    # 989.50 / 10005.00 = 0.09890054972... -> 10 dp, HALF_EVEN -> 0.0989005497
    t = compute_trade(AssetClass.EQUITY, "100", "100", "110")
    assert t.round_trip_pnl_pct == Decimal("0.0989005497")


def test_round_trip_pnl_is_cash_in_minus_cash_out_with_decimal_inputs() -> None:
    t = compute_trade(AssetClass.EQUITY, Decimal("7"), Decimal("123.45"), Decimal("120"))
    assert t.round_trip_pnl == t.sell_cash_in - t.buy_cash_out


# --------------------------------------------------------------------------- crypto example


def test_crypto_half_btc_at_20000_buy_side_matches_spec() -> None:
    # Config: crypto fee 10 bps, slippage 10 bps.
    # Buy fill = 20000 * 1.001 = 20020.00; notional 0.5 * 20020 = 10010.00;
    # fee 10010 * 0.001 = 10.01; cash out 10010.00 + 10.01 = 10020.01.
    t = compute_trade(AssetClass.CRYPTO, "0.5", "20000", "20000")

    assert t.qty == Decimal("0.50000000")
    assert t.buy_fill == Decimal("20020.00000000")
    assert t.buy_notional == Decimal("10010.00")
    assert t.buy_fee == Decimal("10.01")
    assert t.buy_cash_out == Decimal("10020.01")


def test_crypto_round_trip_at_same_open_loses_the_costs() -> None:
    # Sell fill = 20000 * 0.999 = 19980.00; notional 9990.00; fee 9.99; cash in 9980.01.
    # P&L = 9980.01 - 10020.01 = -40.00. Costs = fees 10.01 + 9.99 plus slippage 10.00 + 10.00.
    t = compute_trade(AssetClass.CRYPTO, "0.5", "20000", "20000")

    assert t.sell_fill == Decimal("19980.00000000")
    assert t.sell_notional == Decimal("9990.00")
    assert t.sell_fee == Decimal("9.99")
    assert t.sell_cash_in == Decimal("9980.01")
    assert t.round_trip_pnl == Decimal("-40.00")
    assert t.total_costs == Decimal("40.00")


def test_crypto_example_matches_hand_arithmetic_from_config() -> None:
    qty, buy, sell = Decimal("0.5"), Decimal("20000"), Decimal("21000")
    b = _hand_buy(AssetClass.CRYPTO, qty, buy)
    s = _hand_sell(AssetClass.CRYPTO, qty, sell)

    t = compute_trade(AssetClass.CRYPTO, qty, buy, sell)

    assert t.buy_cash_out == b["cash_out"]
    assert t.sell_cash_in == s["cash_in"]
    assert t.round_trip_pnl == s["cash_in"] - b["cash_out"]


def test_breakdown_reports_the_configured_bps() -> None:
    t = compute_trade(AssetClass.CRYPTO, "0.5", "20000", "20000")
    assert t.fee_bps == _CFG[AssetClass.CRYPTO].fee_bps == Decimal("10")
    assert t.slip_bps == _CFG[AssetClass.CRYPTO].slip_bps == Decimal("10")


# --------------------------------------------------------------------------- parse_decimal


@pytest.mark.parametrize(
    ("text", "expected"),
    [("$1,234.50", Decimal("1234.50")), ("  42 ", Decimal("42")), ("0.10", Decimal("0.10"))],
)
def test_parse_decimal_accepts_money_text_exactly(text: str, expected: Decimal) -> None:
    value = parse_decimal(text)
    assert isinstance(value, Decimal)
    assert value == expected


def test_parse_decimal_keeps_trailing_zeros_no_float() -> None:
    assert str(parse_decimal("0.10")) == "0.10"


@pytest.mark.parametrize("text", ["", "   ", "$", "abc", "NaN", "Infinity", "-3", "0", "$0.00"])
def test_parse_decimal_rejects_bad_text_with_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        parse_decimal(text)


def test_parse_decimal_rejects_non_text() -> None:
    with pytest.raises(ValueError):
        parse_decimal(1.5)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- bad inputs raise


@pytest.mark.parametrize("qty", ["0", "-1", "nan", "inf", ""])
def test_zero_negative_or_nonfinite_quantity_raises(qty: str) -> None:
    with pytest.raises(ValueError):
        compute_trade(AssetClass.EQUITY, qty, "100", "110")


@pytest.mark.parametrize("qty", ["1.5", "0.5"])
def test_fractional_equity_shares_raise_bad_quantity_in_whole_share_mode(qty: str) -> None:
    cfg = default_config().model_copy(update={"equity_qty_quantum": Decimal("1")})
    with pytest.raises(BadQuantityError):
        compute_trade(AssetClass.EQUITY, qty, "100", "110", cfg)


def test_fractional_equity_shares_allowed_by_default_down_to_1e6() -> None:
    assert compute_trade(AssetClass.EQUITY, "1.5", "100", "110").qty == Decimal("1.5")
    with pytest.raises(BadQuantityError):
        compute_trade(AssetClass.EQUITY, "0.0000005", "100", "110")


def test_crypto_quantity_below_eight_decimals_raises() -> None:
    with pytest.raises(BadQuantityError):
        compute_trade(AssetClass.CRYPTO, "0.000000001", "20000", "20000")


@pytest.mark.parametrize(
    ("buy", "sell"), [("0", "110"), ("100", "-5"), ("inf", "110"), ("100", "nan")]
)
def test_zero_negative_or_nonfinite_prices_raise(buy: str, sell: str) -> None:
    with pytest.raises(ValueError):
        compute_trade(AssetClass.EQUITY, "100", buy, sell)
