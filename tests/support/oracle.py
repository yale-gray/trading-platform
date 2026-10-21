"""Independent reference ledger (SPEC-005 section 4, T13).

Replays fills with plain ``Decimal`` arithmetic and shares **no code** with
``tradebot.portfolio``: every rounding rule (SPEC-003 section 2) is re-stated here.
The engine's cash, positions and equity are compared against this, so a bug cannot
hide by being mirrored in both places.

Rounding table: fill price BUY ceil / SELL floor at 1e-8; notional ``q2`` HALF_EVEN;
fee ``q2`` HALF_UP; mark-to-market ``q2(qty * close)`` HALF_EVEN; cost basis in cents,
partial sells reduce it by ``q2(basis * qty / pos_qty)``, a full exit by all of it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    localcontext,
)
from fractions import Fraction

D = Decimal
CENT = D("0.01")
PRICE_Q = D("1E-8")
BPS = D(10000)


def _q(x: Decimal, quantum: Decimal, rounding: str) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 60
        return x.quantize(quantum, rounding=rounding)


def cents(x: Decimal) -> Decimal:
    """Quantize to cents, HALF_EVEN (notional, market value, cost basis, P&L)."""
    return _q(x, CENT, ROUND_HALF_EVEN)


def fee_cents(x: Decimal) -> Decimal:
    """Quantize to cents, HALF_UP (fees)."""
    return _q(x, CENT, ROUND_HALF_UP)


def price_from_float(x: float) -> Decimal:
    """The only float entry: ``Decimal(repr(x))`` rounded HALF_EVEN at 1e-8."""
    return _q(D(repr(x)), PRICE_Q, ROUND_HALF_EVEN)


def buy_price(open_: Decimal, slip_bps: Decimal) -> Decimal:
    """``ceil8(open * (1 + slip/1e4))``."""
    with localcontext() as ctx:
        ctx.prec = 60
        return _q(open_ * (1 + slip_bps / BPS), PRICE_Q, ROUND_CEILING)


def sell_price(open_: Decimal, slip_bps: Decimal) -> Decimal:
    """``floor8(open * (1 - slip/1e4))``."""
    with localcontext() as ctx:
        ctx.prec = 60
        return _q(open_ * (1 - slip_bps / BPS), PRICE_Q, ROUND_FLOOR)


def is_crypto(symbol: str) -> bool:
    """Crypto symbols are the ``BASE/QUOTE`` ones."""
    return "/" in symbol


class OracleError(AssertionError):
    """The reference ledger refused an event (would be negative cash, a short, ...)."""


class RefLedger:
    """Cash, positions, realized P&L and fees folded from fills, deposits and transfers."""

    def __init__(self, fee_bps_equity: Decimal, fee_bps_crypto: Decimal) -> None:
        self.fee_bps = {False: fee_bps_equity, True: fee_bps_crypto}
        self.cash: dict[str, Decimal] = {}
        self.pos: dict[str, dict[str, list[Decimal]]] = {}  # sleeve -> sym -> [qty, basis]
        self.entry: dict[tuple[str, str], object] = {}
        self.closes: dict[str, Decimal] = {}
        self.deposits = D("0.00")
        self.realized = D("0.00")
        self.fees = D("0.00")
        self.expected_fee: dict[str, Decimal] = {}
        self.fills_seen: set[str] = set()

    # ---------------------------------------------------------------- events

    def deposit(self, sleeve: str, amount: Decimal) -> None:
        """Initial funding."""
        self.cash[sleeve] = self.cash.get(sleeve, D("0.00")) + amount
        self.deposits += amount

    def cash_out(self, sleeve: str, amount: Decimal) -> None:
        """Remove cash (transfer OUT leg, FEE)."""
        if amount > self.cash[sleeve]:
            raise OracleError(f"{sleeve}: cash {self.cash[sleeve]} cannot cover {amount}")
        self.cash[sleeve] -= amount

    def cash_in(self, sleeve: str, amount: Decimal) -> None:
        """Add cash (transfer IN leg)."""
        self.cash[sleeve] += amount

    def fill(
        self,
        sleeve: str,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        order_id: str,
        asof: object = None,
    ) -> Decimal:
        """Apply a FILL; returns its notional. The fee is booked by :meth:`fee`."""
        if order_id in self.fills_seen:
            raise OracleError(f"order {order_id} filled twice")
        self.fills_seen.add(order_id)
        with localcontext() as ctx:
            ctx.prec = 60
            notional = cents(qty * price)
        self.expected_fee[order_id] = fee_cents(notional * self.fee_bps[is_crypto(symbol)] / BPS)
        book = self.pos.setdefault(sleeve, {})
        if side == "BUY":
            if notional > self.cash[sleeve]:
                raise OracleError(f"{sleeve}: BUY notional {notional} exceeds cash")
            self.cash[sleeve] -= notional
            cur = book.get(symbol)
            if cur is None:
                book[symbol] = [qty, notional]
                self.entry[(sleeve, symbol)] = asof
            else:
                cur[0] += qty
                cur[1] += notional
        else:
            cur = book.get(symbol)
            if cur is None or qty > cur[0]:
                raise OracleError(f"{sleeve}: SELL {qty} {symbol} exceeds holdings")
            if qty == cur[0]:
                reduce = cur[1]
                del book[symbol]
                self.entry.pop((sleeve, symbol), None)
            else:
                with localcontext() as ctx:
                    ctx.prec = 60
                    reduce = cents(cur[1] * qty / cur[0])
                cur[0] -= qty
                cur[1] -= reduce
            self.cash[sleeve] += notional
            self.realized += notional - reduce
        return notional

    def fee(self, sleeve: str, order_id: str, amount: Decimal) -> None:
        """Book a FEE; it must equal the independently computed fee for that fill."""
        want = self.expected_fee.pop(order_id, None)
        if want is None or want != amount:
            raise OracleError(f"fee for {order_id}: booked {amount}, expected {want}")
        self.cash_out(sleeve, amount)
        self.fees += amount

    def unmatched_fees(self) -> list[str]:
        """Orders whose expected fee is > 0 but was never booked."""
        return [o for o, f in self.expected_fee.items() if f > 0]

    def replay(self, events: Iterable[object]) -> None:
        """Fold ledger-shaped events (duck-typed: ``type``, ``sleeve_id``, ``amount``, ...)."""
        for ev in events:
            t = str(ev.type)  # type: ignore[attr-defined]
            sid, amt = ev.sleeve_id, ev.amount  # type: ignore[attr-defined]
            if t == "DEPOSIT":
                self.deposit(sid, amt)
            elif t == "FILL":
                notional = self.fill(
                    sid,
                    ev.symbol,  # type: ignore[attr-defined]
                    str(ev.side),  # type: ignore[attr-defined]
                    ev.qty,  # type: ignore[attr-defined]
                    ev.price,  # type: ignore[attr-defined]
                    ev.order_id,  # type: ignore[attr-defined]
                    ev.asof,  # type: ignore[attr-defined]
                )
                if notional != amt:
                    raise OracleError(f"FILL amount {amt} != oracle notional {notional}")
            elif t == "FEE":
                self.fee(sid, ev.order_id, amt)  # type: ignore[attr-defined]
            elif t == "CASH_TRANSFER_OUT":
                self.cash_out(sid, amt)
            elif t == "CASH_TRANSFER_IN":
                self.cash_in(sid, amt)

    # ---------------------------------------------------------------- marks

    def set_closes(self, closes: Mapping[str, Decimal]) -> None:
        """Latest close per symbol (symbols without a bar today keep the old one)."""
        self.closes.update(closes)

    def mv(self, symbol: str, qty: Decimal) -> Decimal:
        """Market value of ``qty`` at the last close, in cents."""
        with localcontext() as ctx:
            ctx.prec = 60
            return cents(qty * self.closes[symbol])

    def sleeve_equity(self, sleeve: str) -> Decimal:
        """Cash plus marked positions."""
        return self.cash[sleeve] + sum(
            (self.mv(s, q) for s, (q, _) in self.pos.get(sleeve, {}).items()), D("0.00")
        )

    def equity(self) -> Decimal:
        """Portfolio equity: sum of sleeve equities."""
        return sum((self.sleeve_equity(s) for s in self.cash), D("0.00"))

    def unrealized(self) -> Decimal:
        """Marked value minus cost basis over all positions."""
        return sum(
            (self.mv(s, q) - b for book in self.pos.values() for s, (q, b) in book.items()),
            D("0.00"),
        )


def drawdown_trips(equity: Decimal, peak: Decimal, threshold: Decimal) -> bool:
    """Exact ``1 - equity/peak >= threshold`` in rational arithmetic."""
    return Fraction(1) - Fraction(equity) / Fraction(peak) >= Fraction(threshold)
