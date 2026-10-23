"""Money module: the single conversion boundary between floats and ``Decimal``.

Every float enters the engine through :func:`to_price` / :func:`to_ratio`, and
every ``Decimal`` is rounded through the helpers here. All arithmetic runs in
a fully specified ``prec=28`` context (never a copy of the caller's) with an
explicit ``rounding=`` on every quantize, so results never depend on the
ambient decimal context.
"""

from __future__ import annotations

import math
import numbers
import re
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

from tradebot.data.types import AssetClass

PRICE_Q = Decimal("1E-8")
CENT_Q = Decimal("0.01")
QTY8_Q = Decimal("1E-8")
SHARE_Q = Decimal("1")
WEIGHT_Q = Decimal("1E-6")
RATIO_Q = Decimal("1E-10")

_PREC = 28

# Fully specified context: nothing is inherited from the caller's ambient context.
_MONEY_CONTEXT = Context(
    prec=_PREC,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow],
)

_CANONICAL_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?(E[+-][0-9]+)?", re.ASCII)


class MoneyError(ValueError):
    """Base error for money conversion and rounding failures."""


class BadPriceError(MoneyError):
    """A price was non-finite, non-positive or quantized to zero."""


class BadRatioError(MoneyError):
    """A ratio or weight was non-finite or negative."""


class BadQuantityError(MoneyError):
    """A quantity was negative, non-positive or off its grid."""


@contextmanager
def money_context() -> Iterator[None]:
    """Explicit ``prec=28`` decimal context for all money arithmetic.

    Precision, rounding, traps and exponent limits are all fixed here; the
    caller's ambient context is ignored (and restored on exit).
    """
    with localcontext(_MONEY_CONTEXT):
        yield


def _unsigned_zero(d: Decimal) -> Decimal:
    """Turn ``-0`` into ``+0`` keeping the exponent; other values unchanged."""
    if d.is_zero() and d.is_signed():
        return d.copy_abs()
    return d


def _quantize(
    d: Decimal, quantum: Decimal, rounding: str, error: type[MoneyError] = MoneyError
) -> Decimal:
    """Validated quantize: Decimal-only, finite-only, overflow wrapped, no ``-0``."""
    if not isinstance(d, Decimal):
        raise TypeError(f"expected Decimal, got {type(d).__name__}")
    if not d.is_finite():
        raise error(f"value must be finite, got {d}")
    try:
        with money_context():
            result = d.quantize(quantum, rounding=rounding)
    except InvalidOperation as exc:
        raise error(f"value out of range for quantum {quantum}: {d}") from exc
    return _unsigned_zero(result)


def _normalize_number(x: object) -> float | int:
    """Accept float/int and numpy real scalars; return a builtin float or int."""
    if isinstance(x, bool) or not isinstance(x, numbers.Real):
        raise TypeError(f"expected float or int, got {type(x).__name__}")
    if isinstance(x, numbers.Integral):
        return int(x)
    return float(x)


def _show(n: float | int) -> str:
    """Safe display of a number (huge ints exceed the int->str digit limit)."""
    if isinstance(n, int) and n.bit_length() > 256:
        return f"<int of {n.bit_length()} bits>"
    return repr(n)


def _from_number(x: float | int, quantum: Decimal) -> Decimal | None:
    """Convert via ``repr`` (never ``Decimal(float)``); None if unrepresentable."""
    try:
        with money_context():
            exact = Decimal(x) if isinstance(x, int) else Decimal(repr(x))
            return _unsigned_zero(exact.quantize(quantum, rounding=ROUND_HALF_EVEN))
    except InvalidOperation:
        return None


def to_price(x: float | int) -> Decimal:
    """Convert a float/int price to ``Decimal`` at ``PRICE_Q`` (HALF_EVEN)."""
    n = _normalize_number(x)
    if (isinstance(n, float) and not math.isfinite(n)) or n <= 0:
        raise BadPriceError(f"price must be finite and > 0, got {_show(n)}")
    result = _from_number(n, PRICE_Q)
    if result is None or result <= 0:
        raise BadPriceError(f"price not representable or quantizes to zero: {_show(n)}")
    return result


def to_ratio(x: float | int, quantum: Decimal = RATIO_Q) -> Decimal:
    """Convert a float/int ratio (vol, ATR ratio, weight) to ``Decimal``."""
    n = _normalize_number(x)
    if (isinstance(n, float) and not math.isfinite(n)) or n < 0:
        raise BadRatioError(f"ratio must be finite and >= 0, got {_show(n)}")
    result = _from_number(n, quantum)
    if result is None:
        raise BadRatioError(f"ratio not representable: {_show(n)}")
    return result


def to_weight(x: float | int) -> Decimal:
    """Convert a strategy weight to ``Decimal`` at ``WEIGHT_Q``."""
    return to_ratio(x, WEIGHT_Q)


def q_price(d: Decimal, rounding: str) -> Decimal:
    """Quantize ``d`` to ``PRICE_Q`` with the given rounding mode."""
    return _quantize(d, PRICE_Q, rounding, BadPriceError)


def ceil8(d: Decimal) -> Decimal:
    """Round up to ``PRICE_Q`` (buy fill price)."""
    return q_price(d, ROUND_CEILING)


def floor8(d: Decimal) -> Decimal:
    """Round down to ``PRICE_Q`` (sell fill price)."""
    return q_price(d, ROUND_FLOOR)


def q2(d: Decimal) -> Decimal:
    """Quantize to cents, HALF_EVEN (notional, market value, basis, P&L)."""
    return _quantize(d, CENT_Q, ROUND_HALF_EVEN)


def q2_half_up(d: Decimal) -> Decimal:
    """Quantize to cents, HALF_UP (fees; ties away from zero)."""
    return _quantize(d, CENT_Q, ROUND_HALF_UP)


def _floor_qty(d: Decimal, quantum: Decimal) -> Decimal:
    if not isinstance(d, Decimal):
        raise TypeError(f"expected Decimal, got {type(d).__name__}")
    if not d.is_finite():
        raise BadQuantityError(f"quantity must be finite, got {d}")
    if d < 0:
        raise BadQuantityError(f"quantity must be >= 0, got {d}")
    return _quantize(d, quantum, ROUND_DOWN, BadQuantityError)


def floor_shares(d: Decimal) -> Decimal:
    """Whole shares for equity BUY sizing (round toward zero)."""
    return _floor_qty(d, SHARE_Q)


def floor_qty8(d: Decimal) -> Decimal:
    """Crypto BUY sizing at 8 decimals (round toward zero)."""
    return _floor_qty(d, QTY8_Q)


# Default fractional-share grid (owner decision 2026-10-22).
EQUITY_QTY_QUANTUM = Decimal("0.000001")


def qty_quantum(asset_class: AssetClass, equity_quantum: Decimal = EQUITY_QTY_QUANTUM) -> Decimal:
    """Quantity grid step of ``asset_class`` (equity: configurable; crypto: 1e-8)."""
    return equity_quantum if asset_class is AssetClass.EQUITY else QTY8_Q


def buy_qty(
    d: Decimal, asset_class: AssetClass, equity_quantum: Decimal = EQUITY_QTY_QUANTUM
) -> Decimal:
    """Round a BUY quantity down onto the grid of ``asset_class``."""
    return _floor_qty(d, qty_quantum(asset_class, equity_quantum))


def check_sell_qty(
    qty: Decimal, asset_class: AssetClass, equity_quantum: Decimal = EQUITY_QTY_QUANTUM
) -> Decimal:
    """Validate a SELL quantity; it is never rounded, only checked."""
    if not isinstance(qty, Decimal):
        raise TypeError(f"expected Decimal, got {type(qty).__name__}")
    if not qty.is_finite() or qty <= 0:
        raise BadQuantityError(f"sell quantity must be finite and > 0, got {qty}")
    quantum = qty_quantum(asset_class, equity_quantum)
    try:
        with money_context():
            on_grid = qty == qty.quantize(quantum, rounding=ROUND_DOWN)
    except InvalidOperation as exc:
        raise BadQuantityError(f"sell quantity {qty} is out of range") from exc
    if not on_grid:
        raise BadQuantityError(f"sell quantity {qty} is off the {asset_class} grid")
    return qty


def q_ratio(d: Decimal) -> Decimal:
    """Quantize to ``RATIO_Q``, HALF_EVEN."""
    return _quantize(d, RATIO_Q, ROUND_HALF_EVEN, BadRatioError)


def q_weight(d: Decimal) -> Decimal:
    """Quantize to ``WEIGHT_Q``, HALF_EVEN."""
    return _quantize(d, WEIGHT_Q, ROUND_HALF_EVEN, BadRatioError)


def to_text(d: Decimal) -> str:
    """Canonical persistence form of a ``Decimal`` (``str(d)``; ``-0`` becomes ``0``).

    Rendered under :func:`money_context` (``capitals=1``) so the ambient
    context cannot change the exponent marker (always ``E``, never ``e``).
    """
    if not isinstance(d, Decimal):
        raise TypeError(f"expected Decimal, got {type(d).__name__}")
    with money_context():
        return str(_unsigned_zero(d))


def from_text(s: str) -> Decimal:
    """Parse the canonical persistence form back into a ``Decimal``.

    Strict: only ``-?digits(.digits)?(E[+-]digits)?`` is accepted, i.e. what
    ``str(Decimal)`` emits. Whitespace, underscores, ``+``, NaN and Infinity
    are rejected with :class:`MoneyError`.
    """
    if not isinstance(s, str):
        raise TypeError(f"expected str, got {type(s).__name__}")
    if _CANONICAL_TEXT.fullmatch(s) is None:
        raise MoneyError(f"not a canonical decimal: {s!r}")
    try:
        with money_context():
            d = Decimal(s)
    except InvalidOperation as exc:
        raise MoneyError(f"not a decimal: {s!r}") from exc
    return _unsigned_zero(d)
