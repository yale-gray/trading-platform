"""Hand-calculated money and rounding cases (SPEC-005 section 6.3-6.5, T5).

Every expected value below is worked out by hand in a comment. The helpers
under test are ceil8, floor8, q2, q2_half_up, floor_shares, floor_qty8 and
buy_qty. Fill and fee formulas are composed in this file from those helpers,
per SPEC-003 section 2's rounding table:

- buy fill   ceil8(open x (1 + slip_bps/10000))      quantum 1e-8, CEILING
- sell fill  floor8(open x (1 - slip_bps/10000))     quantum 1e-8, FLOOR
- notional   q2(qty x price)                          quantum 0.01, HALF_EVEN
- fee        q2_half_up(notional x fee_bps/10000)     quantum 0.01, HALF_UP
- equity buy sizing  floor_shares                     quantum 1 share, DOWN
- crypto buy sizing  floor_qty8                       quantum 1e-8, DOWN
"""

from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from tradebot.data.types import AssetClass
from tradebot.portfolio.money import (
    PRICE_Q,
    buy_qty,
    ceil8,
    floor8,
    floor_qty8,
    floor_shares,
    money_context,
    q2,
    q2_half_up,
    to_price,
)

pytestmark = pytest.mark.unit

BPS = Decimal("10000")


def _exp(d: Decimal) -> int:
    return d.as_tuple().exponent  # type: ignore[return-value]


def _buy_fill(open_: Decimal, slip_bps: int) -> Decimal:
    with money_context():
        return ceil8(open_ * (Decimal("1") + Decimal(slip_bps) / BPS))


def _sell_fill(open_: Decimal, slip_bps: int) -> Decimal:
    with money_context():
        return floor8(open_ * (Decimal("1") - Decimal(slip_bps) / BPS))


def _notional(qty: Decimal, price: Decimal) -> Decimal:
    with money_context():
        return q2(qty * price)


def _fee(notional: Decimal, fee_bps: int) -> Decimal:
    with money_context():
        return q2_half_up(notional * Decimal(fee_bps) / BPS)


# ---------------------------------------------------------------- 6.3 ETF


def test_etf_buy_fill_ceiling_is_exact_at_8dp() -> None:
    # ceil8(100.00 x 1.0005) = ceil8(100.05000000) = 100.05000000 (exact, CEILING no-op)
    fill = _buy_fill(Decimal("100.00"), 5)
    assert fill == Decimal("100.05000000")
    assert _exp(fill) == -8


def test_etf_buy_notional_fee_and_cash_after() -> None:
    # notional = q2(100 x 100.05) = 10005.00; fee = q2_half_up(10005.00 x 0) = 0.00
    # cash after = 100000.00 - 10005.00 = 89995.00
    fill = _buy_fill(Decimal("100.00"), 5)
    notional = _notional(Decimal("100"), fill)
    fee = _fee(notional, 0)
    assert notional == Decimal("10005.00")
    assert _exp(notional) == -2
    assert fee == Decimal("0.00")
    assert _exp(fee) == -2
    assert Decimal("100000.00") - (notional + fee) == Decimal("89995.00")


def test_etf_sell_fill_floor_is_exact_at_8dp() -> None:
    # floor8(110.00 x 0.9995) = floor8(109.94500000) = 109.94500000 (exact at 8 dp)
    fill = _sell_fill(Decimal("110.00"), 5)
    assert fill == Decimal("109.94500000")
    assert _exp(fill) == -8


def test_etf_sell_notional_fee_and_cash_after() -> None:
    # notional = q2(100 x 109.945) = q2(10994.500) = 10994.50; fee 0.00
    # cash after = 89995.00 + 10994.50 = 100989.50
    fill = _sell_fill(Decimal("110.00"), 5)
    notional = _notional(Decimal("100"), fill)
    fee = _fee(notional, 0)
    assert notional == Decimal("10994.50")
    assert _exp(notional) == -2
    assert fee == Decimal("0.00")
    assert Decimal("89995.00") + (notional - fee) == Decimal("100989.50")


def test_etf_round_trip_net_pnl_is_989_50() -> None:
    # P&L = 100989.50 - 100000.00 = +989.50
    buy = _notional(Decimal("100"), _buy_fill(Decimal("100.00"), 5))
    sell = _notional(Decimal("100"), _sell_fill(Decimal("110.00"), 5))
    cash_after = Decimal("100000.00") - buy + sell
    assert cash_after - Decimal("100000.00") == Decimal("989.50")


def test_etf_bar_end_equity_while_holding() -> None:
    # market value = q2(100 x 105.00) = 10500.00
    # equity = cash 89995.00 + 10500.00 = 100495.00
    mv = _notional(Decimal("100"), Decimal("105.00"))
    assert mv == Decimal("10500.00")
    assert _exp(mv) == -2
    assert Decimal("89995.00") + mv == Decimal("100495.00")


def test_etf_buy_sizing_keeps_whole_hundred_shares() -> None:
    # 100 is already on the 1-share grid; floor_shares leaves it unchanged, exponent 0.
    qty = buy_qty(Decimal("100"), AssetClass.EQUITY)
    assert qty == Decimal("100")
    assert _exp(qty) == 0


# ---------------------------------------------------------- 6.4 crypto


def test_crypto_buy_fill_ceiling_at_10_bps() -> None:
    # ceil8(20000 x 1.001) = ceil8(20020.00000000) = 20020.00000000
    fill = _buy_fill(Decimal("20000"), 10)
    assert fill == Decimal("20020.00000000")
    assert _exp(fill) == -8


def test_crypto_buy_notional_fee_and_cash_after() -> None:
    # notional = q2(0.5 x 20020) = 10010.00
    # fee = q2_half_up(10010.00 x 0.001) = q2_half_up(10.010) = 10.01
    # cash out = 10010.00 + 10.01 = 10020.01; cash after = 100000.00 - 10020.01 = 89979.99
    fill = _buy_fill(Decimal("20000"), 10)
    notional = _notional(Decimal("0.5"), fill)
    fee = _fee(notional, 10)
    assert notional == Decimal("10010.00")
    assert fee == Decimal("10.01")
    assert _exp(fee) == -2
    assert notional + fee == Decimal("10020.01")
    assert Decimal("100000.00") - (notional + fee) == Decimal("89979.99")


def test_crypto_sell_fill_floor_at_10_bps() -> None:
    # floor8(22000 x 0.999) = floor8(21978.000) = 21978.00000000
    fill = _sell_fill(Decimal("22000"), 10)
    assert fill == Decimal("21978.00000000")
    assert _exp(fill) == -8


def test_crypto_sell_notional_fee_and_cash_after() -> None:
    # notional = q2(0.5 x 21978) = 10989.00
    # fee = q2_half_up(10989.00 x 0.001) = q2_half_up(10.989) = 10.99
    # cash in = 10989.00 - 10.99 = 10978.01; cash after = 89979.99 + 10978.01 = 100958.00
    fill = _sell_fill(Decimal("22000"), 10)
    notional = _notional(Decimal("0.5"), fill)
    fee = _fee(notional, 10)
    assert notional == Decimal("10989.00")
    assert fee == Decimal("10.99")
    assert notional - fee == Decimal("10978.01")
    assert Decimal("89979.99") + (notional - fee) == Decimal("100958.00")


def test_crypto_round_trip_pnl_and_fees() -> None:
    # gross 0.5 x (22000 - 20000) = 1000.00
    # slippage: buy 0.5 x (20020 - 20000) = 10.00; sell 0.5 x (22000 - 21978) = 11.00
    # fees: 10.01 + 10.99 = 21.00
    # net = 1000.00 - 10.00 - 11.00 - 21.00 = 958.00
    buy_fill = _buy_fill(Decimal("20000"), 10)
    sell_fill = _sell_fill(Decimal("22000"), 10)
    buy_n = _notional(Decimal("0.5"), buy_fill)
    sell_n = _notional(Decimal("0.5"), sell_fill)
    fees = _fee(buy_n, 10) + _fee(sell_n, 10)
    assert fees == Decimal("21.00")
    assert (sell_n - buy_n) - fees == Decimal("958.00")
    assert Decimal("1000.00") - Decimal("10.00") - Decimal("11.00") - fees == Decimal("958.00")


def test_both_legs_fee_positive_and_slippage_adverse() -> None:
    # buy fill 20020 > open 20000 (pays up); sell fill 21978 < open 22000 (receives less)
    buy_fill = _buy_fill(Decimal("20000"), 10)
    sell_fill = _sell_fill(Decimal("22000"), 10)
    assert buy_fill > Decimal("20000")
    assert sell_fill < Decimal("22000")
    assert _fee(_notional(Decimal("0.5"), buy_fill), 10) > 0
    assert _fee(_notional(Decimal("0.5"), sell_fill), 10) > 0


def test_crypto_on_grid_quantity_kept_at_exponent_minus_8() -> None:
    # 0.5 is already on the 1e-8 grid, so floor_qty8 leaves it unchanged at exponent -8.
    qty = buy_qty(Decimal("0.5"), AssetClass.CRYPTO)
    assert qty == Decimal("0.5")
    assert _exp(qty) == -8


# ------------------------------------------------- 6.5 rounding edges


def test_fill_direction_input_price_is_8dp() -> None:
    # to_price(float 10.00000001): repr is "10.00000001", already at 8 dp, HALF_EVEN no-op
    price = to_price(10.00000001)
    assert price == Decimal("10.00000001")
    assert _exp(price) == -8


def test_fill_direction_buy_ceiling_beats_half_even() -> None:
    # 10.00000001 x 1.0005 = 10.00000001 + 0.005000000005 = 10.005000010005
    # CEILING at 1e-8 -> 10.00500002 (HALF_EVEN would give 10.00500001: tail 0.000000000005 < half)
    price = to_price(10.00000001)
    with money_context():
        exact = price * (Decimal("1") + Decimal("5") / BPS)
        assert exact == Decimal("10.005000010005")
        half_even = exact.quantize(PRICE_Q, rounding=ROUND_HALF_EVEN)
    fill = _buy_fill(price, 5)
    assert fill == Decimal("10.00500002")
    assert _exp(fill) == -8
    assert half_even == Decimal("10.00500001")
    assert fill != half_even


def test_fill_direction_sell_floor_beats_half_even() -> None:
    # 10.00000001 x 0.9995 = 10.00000001 - 0.000005000005 = 9.995000009995
    # FLOOR at 1e-8 -> 9.99500000 (HALF_EVEN would give 9.99500001: tail 0.000000009995 > half)
    price = to_price(10.00000001)
    with money_context():
        exact = price * (Decimal("1") - Decimal("5") / BPS)
        assert exact == Decimal("9.995000009995")
        half_even = exact.quantize(PRICE_Q, rounding=ROUND_HALF_EVEN)
    fill = _sell_fill(price, 5)
    assert fill == Decimal("9.99500000")
    assert _exp(fill) == -8
    assert half_even == Decimal("9.99500001")
    assert fill != half_even


def test_notional_at_cents_from_ceiling_fill() -> None:
    # 10 x 10.00500002 = 100.0500002 -> q2 = 100.05 (tail 0.0000002 is below half a cent)
    notional = _notional(Decimal("10"), Decimal("10.00500002"))
    assert notional == Decimal("100.05")
    assert _exp(notional) == -2


def test_notional_at_cents_from_floor_fill() -> None:
    # 10 x 9.995 = 99.95000000 -> q2 = 99.95 (exact)
    notional = _notional(Decimal("10"), Decimal("9.99500000"))
    assert notional == Decimal("99.95")
    assert _exp(notional) == -2


def test_notional_tie_rounds_half_even_down() -> None:
    # 5 x 20.001 = 100.005 exactly; HALF_EVEN -> 100.00 (last kept digit 0 is even)
    # HALF_UP would give 100.01
    notional = _notional(Decimal("5"), Decimal("20.001"))
    assert notional == Decimal("100.00")
    assert _exp(notional) == -2


def test_notional_tie_rounds_half_even_up_to_even() -> None:
    # 7 x 14.275 = 99.925 exactly; HALF_EVEN -> 99.92 (2 is even)
    # HALF_UP would give 99.93
    notional = _notional(Decimal("7"), Decimal("14.275"))
    assert notional == Decimal("99.92")
    assert _exp(notional) == -2


@pytest.mark.parametrize(
    ("notional", "expected"),
    [
        # 5.00 x 10 bps = 0.005 -> 0.01 (HALF_EVEN would give 0.00)
        (Decimal("5.00"), Decimal("0.01")),
        # 25.00 x 10 bps = 0.025 -> 0.03 (HALF_EVEN would give 0.02)
        (Decimal("25.00"), Decimal("0.03")),
        # 15.00 x 10 bps = 0.015 -> 0.02 (HALF_UP and HALF_EVEN agree)
        (Decimal("15.00"), Decimal("0.02")),
    ],
)
def test_fee_ties_round_half_up(notional: Decimal, expected: Decimal) -> None:
    fee = _fee(notional, 10)
    assert fee == expected
    assert _exp(fee) == -2


# ---- whole-share sizing rounds DOWN


def test_equity_sizing_target_rounds_down_to_300_shares() -> None:
    # 10000 / 33.33 = 300.030003... -> floor to whole share = 300 (not 301, not 300.03)
    with money_context():
        raw = Decimal("10000") / Decimal("33.33")
    qty = floor_shares(raw)
    assert qty == Decimal("300")
    assert _exp(qty) == 0
    assert buy_qty(raw, AssetClass.EQUITY) == Decimal("300")


def test_equity_sized_buy_fill_notional_and_cash_out() -> None:
    # fill = ceil8(33.40 x 1.0005) = ceil8(33.41670000) = 33.41670000
    # notional = q2(300 x 33.4167) = q2(10025.01000) = 10025.01; fee 0.00
    # cash out 10025.01 (may exceed the 10000.00 target: sizing used the ref close)
    fill = _buy_fill(Decimal("33.40"), 5)
    assert fill == Decimal("33.41670000")
    notional = _notional(Decimal("300"), fill)
    fee = _fee(notional, 0)
    assert notional == Decimal("10025.01")
    assert fee == Decimal("0.00")
    assert notional + fee == Decimal("10025.01")


# ---- crypto sizing rounds DOWN at 1e-8


def test_crypto_sizing_target_rounds_down_at_1e8() -> None:
    # 10000 / 30000.12345678 = 0.33333196159... -> floor at 1e-8 = 0.33333196 BTC
    # (the 8-dp tail 0.159... is below the next unit, so DOWN and HALF_EVEN agree here;
    # the case pins the 1e-8 quantum and the DOWN direction)
    with money_context():
        raw = Decimal("10000") / Decimal("30000.12345678")
    qty = floor_qty8(raw)
    assert qty == Decimal("0.33333196")
    assert _exp(qty) == -8
    assert buy_qty(raw, AssetClass.CRYPTO) == Decimal("0.33333196")


def test_crypto_sized_buy_fill_notional_fee_and_cash_out() -> None:
    # fill = ceil8(30000.12345678 x 1.001) = ceil8(30030.12358023678) = 30030.12358024
    # notional = q2(0.33333196 x 30030.12358024) = q2(10009.99995204...) = 10010.00
    # fee = q2_half_up(10010.00 x 0.001) = 10.01; cash out = 10020.01
    fill = _buy_fill(Decimal("30000.12345678"), 10)
    assert fill == Decimal("30030.12358024")
    assert _exp(fill) == -8
    notional = _notional(Decimal("0.33333196"), fill)
    fee = _fee(notional, 10)
    assert notional == Decimal("10010.00")
    assert fee == Decimal("10.01")
    assert notional + fee == Decimal("10020.01")


# ---- DOWN differs from HALF_UP / HALF_EVEN when the fractional tail is >= 0.5


def test_equity_sizing_tail_above_half_rounds_down_to_142_shares() -> None:
    # 1000 / 7 = 142.857142857... -> floor to whole share = 142.
    # HALF_UP and HALF_EVEN would both give 143 (tail 0.857 is above half).
    with money_context():
        raw = Decimal("1000") / Decimal("7")
    qty = floor_shares(raw)
    assert qty == Decimal("142")
    assert _exp(qty) == 0
    assert buy_qty(raw, AssetClass.EQUITY) == Decimal("142")


def test_crypto_sizing_tail_above_half_rounds_down_at_1e8() -> None:
    # 0.123456789 -> 8 dp is 0.12345678 with a tail of 0.9 units of 1e-8.
    # DOWN gives ...78; HALF_UP and HALF_EVEN would both give ...79.
    qty = floor_qty8(Decimal("0.123456789"))
    assert qty == Decimal("0.12345678")
    assert _exp(qty) == -8
    assert buy_qty(Decimal("0.123456789"), AssetClass.CRYPTO) == Decimal("0.12345678")
