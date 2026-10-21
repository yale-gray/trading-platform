"""Cost model and fill pricing (spec 003 section 2).

All functions take ``Decimal`` inputs; basis-point rates are plain ``Decimal``
arguments. Rounding always works against the trader and is delegated to
``tradebot.portfolio.money``.
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.portfolio.money import ceil8, floor8, money_context, q2, q2_half_up

_BPS = Decimal(10000)
_ONE = Decimal(1)


def buy_fill_price(open_price: Decimal, slip_bps: Decimal) -> Decimal:
    """BUY fill price: ``ceil8(open * (1 + slip_bps / 10000))``."""
    with money_context():
        return ceil8(open_price * (_ONE + slip_bps / _BPS))


def sell_fill_price(open_price: Decimal, slip_bps: Decimal) -> Decimal:
    """SELL fill price: ``floor8(open * (1 - slip_bps / 10000))``."""
    with money_context():
        return floor8(open_price * (_ONE - slip_bps / _BPS))


def notional(qty: Decimal, price: Decimal) -> Decimal:
    """Trade notional: ``q2(qty * price)``."""
    with money_context():
        return q2(qty * price)


def fee(notional_amount: Decimal, fee_bps: Decimal) -> Decimal:
    """Trading fee: ``q2_half_up(notional * fee_bps / 10000)``."""
    with money_context():
        return q2_half_up(notional_amount * fee_bps / _BPS)


def buy_cash_out(notional_amount: Decimal, fee_amount: Decimal) -> Decimal:
    """Cash leaving the sleeve on a BUY: ``notional + fee``."""
    with money_context():
        return notional_amount + fee_amount


def sell_cash_in(notional_amount: Decimal, fee_amount: Decimal) -> Decimal:
    """Cash entering the sleeve on a SELL: ``notional - fee``."""
    with money_context():
        return notional_amount - fee_amount


def slippage_cost(fill_price: Decimal, open_price: Decimal, qty: Decimal) -> Decimal:
    """Informational slippage ``q2(|fill_price - open| * qty)`` in cents (not a cash line)."""
    with money_context():
        return q2(abs(fill_price - open_price) * qty)
