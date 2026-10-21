"""Hand-calculated cost and fill-price cases (SPEC-003 section 2, T11b).

Every expected value is derived by hand in a comment. Assertions compare the
``str()`` of the result, which encodes both the value and the exponent (scale),
so ``10994.5`` or ``109.945`` would fail where ``10994.50`` or ``109.94500000``
is required.
"""

from decimal import Decimal
from itertools import product

import pytest

from tradebot.sim.costs import (
    buy_cash_out,
    buy_fill_price,
    fee,
    notional,
    sell_cash_in,
    sell_fill_price,
    slippage_cost,
)

pytestmark = pytest.mark.unit

D = Decimal


# --- Equity: 100 shares, open 100.00, slip 5 bps, fee 0 bps ---------------------
# buy fill  = ceil8(100.00 * 1.0005)  = ceil8(100.05)        = 100.05000000
# notional  = q2(100 * 100.05)        = 10005.00
# fee       = q2_half_up(10005 * 0)   = 0.00
def test_equity_buy_fill_notional_and_zero_fee() -> None:
    price = buy_fill_price(D("100.00"), D("5"))
    assert str(price) == "100.05000000"
    n = notional(D("100"), price)
    assert str(n) == "10005.00"
    f = fee(n, D("0"))
    assert str(f) == "0.00"
    assert str(buy_cash_out(n, f)) == "10005.00"


# sell fill = floor8(110.00 * 0.9995) = floor8(109.945) = 109.94500000
# notional  = q2(100 * 109.945)       = 10994.50 (exact, no tie)
# fee       = 0.00
def test_equity_sell_fill_and_notional() -> None:
    price = sell_fill_price(D("110.00"), D("5"))
    assert str(price) == "109.94500000"
    n = notional(D("100"), price)
    assert str(n) == "10994.50"
    f = fee(n, D("0"))
    assert str(f) == "0.00"
    assert str(sell_cash_in(n, f)) == "10994.50"


# --- Crypto: 0.5 BTC, open 20000, 10/10 bps -------------------------------------
# buy fill  = ceil8(20000 * 1.001)    = 20020.00000000
# notional  = q2(0.5 * 20020)         = 10010.00
# fee       = q2_half_up(10010 * 10 / 10000) = q2_half_up(10.01) = 10.01
# cash out  = 10010.00 + 10.01        = 10020.01
def test_crypto_buy_fill_fee_and_cash_out() -> None:
    price = buy_fill_price(D("20000"), D("10"))
    assert str(price) == "20020.00000000"
    n = notional(D("0.5"), price)
    assert str(n) == "10010.00"
    f = fee(n, D("10"))
    assert str(f) == "10.01"
    assert str(buy_cash_out(n, f)) == "10020.01"


# sell fill = floor8(20000 * 0.999)   = 19980.00000000
# notional  = q2(0.5 * 19980)         = 9990.00
# fee       = q2_half_up(9990 * 0.001) = 9.99
# cash in   = 9990.00 - 9.99          = 9980.01
def test_crypto_sell_fill_fee_and_cash_in() -> None:
    price = sell_fill_price(D("20000"), D("10"))
    assert str(price) == "19980.00000000"
    n = notional(D("0.5"), price)
    assert str(n) == "9990.00"
    f = fee(n, D("10"))
    assert str(f) == "9.99"
    assert str(sell_cash_in(n, f)) == "9980.01"


# --- Non-round open: 123.45678901 at 5 bps --------------------------------------
# open * 0.0005 = 0.061728394505
# buy  raw = 123.45678901 + 0.061728394505 = 123.518517404505 -> ceil8 -> 123.51851741
# sell raw = 123.45678901 - 0.061728394505 = 123.395060615495 -> floor8 -> 123.39506061
def test_non_round_open_rounds_against_trader() -> None:
    assert str(buy_fill_price(D("123.45678901"), D("5"))) == "123.51851741"
    assert str(sell_fill_price(D("123.45678901"), D("5"))) == "123.39506061"


# --- HALF_UP fee tie -------------------------------------------------------------
# notional 125.00 * 10 / 10000 = 0.125 exactly. HALF_UP -> 0.13
# (HALF_EVEN would give 0.12, so this distinguishes the two modes.)
def test_fee_half_up_tie_rounds_away_from_zero() -> None:
    f = fee(D("125.00"), D("10"))
    assert str(f) == "0.13"


# --- HALF_EVEN notional tie ------------------------------------------------------
# 1 * 10.125 = 10.125 exactly. HALF_EVEN -> 10.12 (2 is even).
# 1 * 10.135 = 10.135 exactly. HALF_EVEN -> 10.14 (4 is even).
# (HALF_UP would give 10.13 for the first, so this distinguishes the two modes.)
@pytest.mark.parametrize(
    ("price", "expected"),
    [("10.125", "10.12"), ("10.135", "10.14")],
)
def test_notional_half_even_tie(price: str, expected: str) -> None:
    assert str(notional(D("1"), D(price))) == expected


# --- Slippage (informational): |100.05 - 100.00| * 100 = 5.00 ----------------------
# |100.05 - 100.00| * 100 = 5.00; |109.945 - 110| * 100 = 5.5 -> 5.50 (cents, HALF_EVEN).
def test_slippage_cost_is_absolute_informational_amount() -> None:
    assert str(slippage_cost(D("100.05000000"), D("100.00"), D("100"))) == "5.00"
    assert str(slippage_cost(D("109.94500000"), D("110.00"), D("100"))) == "5.50"


# --- Invariant I9: buy >= open, sell <= open ------------------------------------
_OPENS = ["0.00000001", "0.5", "1", "99.99", "100.00", "123.45678901", "20000", "1234567.12345678"]
_SLIPS = ["0", "1", "5", "10", "250"]


@pytest.mark.parametrize(("open_text", "slip_text"), list(product(_OPENS, _SLIPS)))
def test_buy_never_below_open_and_sell_never_above_open(open_text: str, slip_text: str) -> None:
    open_price = D(open_text)
    slip = D(slip_text)
    buy = buy_fill_price(open_price, slip)
    sell = sell_fill_price(open_price, slip)
    assert buy >= open_price
    assert sell <= open_price
    # Both prices sit on the 1e-8 grid (exponent -8).
    assert buy.as_tuple().exponent == -8
    assert sell.as_tuple().exponent == -8
