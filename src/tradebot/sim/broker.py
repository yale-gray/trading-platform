"""Bar input, the ``Broker`` protocol and the simulated broker (SPEC-003 §1-3).

``SimBroker`` is the only broker implementation (no real-money code exists).
It owns the pending order book and runs step A of the per-bar protocol: fill
pending orders at the symbol's bar *open* (+/- slippage), SELLs first, each in
``order_id`` order. Risk checks happen before orders reach it. Nothing here reads
a wall clock; the only float-to-Decimal conversion is :func:`to_price`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import EngineConfig
from tradebot.portfolio.ledger import LedgerError
from tradebot.portfolio.models import Dec, FillSide, LedgerEvent, PortfolioSnapshot, Stage
from tradebot.portfolio.money import buy_qty, money_context, qty_quantum, to_price
from tradebot.portfolio.portfolio import Portfolio
from tradebot.sim import costs
from tradebot.sim.orders import (
    Order,
    OrderIdGenerator,
    OrderReason,
    OrderRequest,
    OrderStatus,
    Side,
    transition,
)

_BPS = Decimal(10000)


class StaleBarError(ValueError):
    """``process_bar`` was called with an ``asof`` that does not strictly increase (I15)."""


class Bar(BaseModel):
    """One symbol's bar for one date. Floats are allowed here ONLY (see ``to_price``)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    open: float
    high: float
    low: float
    close: float
    vol20: float | None = None
    atr14: float | None = None


class FillResult(BaseModel):
    """Outcome of one ``SimBroker.process_bar`` (step A only)."""

    model_config = ConfigDict(frozen=True)

    asof: date
    fills: list[Order]
    rejected: list[Order]
    expired: list[Order]
    new_ledger_events: list[LedgerEvent]


class BookError(ValueError):
    """A :class:`BrokerBook` is internally inconsistent and cannot be restored."""


class BrokerBook(BaseModel):
    """The broker's complete order book as plain data (JSON-safe, Decimals as text).

    ``orders`` holds every order ever created (terminal ones included) sorted by
    ``order_id``; ``pending_ids`` the PENDING subset (sorted); ``waited`` the bars
    each pending order has waited for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    orders: list[Order]
    pending_ids: list[str]
    waited: dict[str, int]
    last_asof: date | None = None
    last_close: dict[str, Dec]


class Broker(Protocol):
    """The broker interface; ``SimBroker`` is the only implementation."""

    def submit_orders(self, requests: Sequence[OrderRequest], asof: date) -> list[Order]: ...

    def cancel_order(self, order_id: str) -> Order: ...

    def process_bar(self, asof: date, bars: Mapping[str, Bar]) -> FillResult: ...

    def snapshot(self) -> PortfolioSnapshot: ...


def default_asset_class(symbol: str) -> AssetClass:
    """Asset class via the data layer: normalize (``btc-usd`` -> ``BTC/USD``), then classify.

    Raises ``UnknownSymbolError`` for a symbol the data layer does not know.
    """
    from tradebot.data.symbols import asset_class, normalize_symbol  # lazy: keep import light

    return asset_class(normalize_symbol(symbol))


class SimBroker:
    """Deterministic simulated broker over a :class:`Portfolio` (ledger fold)."""

    def __init__(
        self,
        config: EngineConfig,
        portfolio: Portfolio,
        *,
        id_gen: OrderIdGenerator | None = None,
        asset_class_of: Callable[[str], AssetClass] = default_asset_class,
    ) -> None:
        self._config = config
        self.portfolio = portfolio
        self._ids = id_gen if id_gen is not None else OrderIdGenerator()
        self._asset_class_of = asset_class_of
        self._pending: dict[str, Order] = {}
        self._waited: dict[str, int] = {}
        self._orders: dict[str, Order] = {}
        self._last_asof: date | None = None
        self._last_close: dict[str, Decimal] = {}

    @property
    def pending_orders(self) -> list[Order]:
        """Pending orders in ``order_id`` order."""
        return [self._pending[k] for k in sorted(self._pending)]

    def export_book(self) -> BrokerBook:
        """Snapshot the order book (deterministic: everything sorted by key)."""
        return BrokerBook(
            orders=[self._orders[k] for k in sorted(self._orders)],
            pending_ids=sorted(self._pending),
            waited={k: self._waited[k] for k in sorted(self._waited)},
            last_asof=self._last_asof,
            last_close=dict(sorted(self._last_close.items())),
        )

    def restore_book(self, book: BrokerBook) -> None:
        """Replace the order book with ``book`` after validating it.

        Raises :class:`BookError` (leaving the broker untouched) if order ids repeat,
        a pending id is unknown, repeated or not PENDING, a PENDING order is missing
        from ``pending_ids``, or ``waited`` is not exactly the pending set with
        non-negative counts.
        """
        orders: dict[str, Order] = {}
        for o in book.orders:
            if o.order_id in orders:
                raise BookError(f"duplicate order {o.order_id}")
            if o.status is OrderStatus.NEW:
                raise BookError(f"order {o.order_id} is NEW (never persisted)")
            orders[o.order_id] = o
        if len(set(book.pending_ids)) != len(book.pending_ids):
            raise BookError("duplicate pending id")
        for oid in book.pending_ids:
            if oid not in orders:
                raise BookError(f"pending id {oid} is not a known order")
            if orders[oid].status is not OrderStatus.PENDING:
                raise BookError(f"pending id {oid} is {orders[oid].status.value}, not PENDING")
        status_pending = {k for k, o in orders.items() if o.status is OrderStatus.PENDING}
        if status_pending != set(book.pending_ids):
            raise BookError("PENDING orders do not match pending_ids")
        if set(book.waited) != status_pending:
            raise BookError("waited does not match the pending orders")
        if any(n < 0 for n in book.waited.values()):
            raise BookError("negative wait counter")
        self._orders = orders
        self._pending = {k: orders[k] for k in sorted(book.pending_ids)}
        self._waited = {k: book.waited[k] for k in sorted(book.waited)}
        self._last_asof = book.last_asof
        self._last_close = dict(book.last_close)

    def get_order(self, order_id: str) -> Order:
        """Latest known state of an order (any status)."""
        return self._orders[order_id]

    def submit_orders(self, requests: Sequence[OrderRequest], asof: date) -> list[Order]:
        """Create PENDING orders (created at ``asof``, fill at a later bar's open).

        Raises ``ValueError`` (before creating anything) for an unknown sleeve or
        a SELL above the sleeve's holding minus qty reserved by pending SELLs.
        """
        state = self.portfolio.ledger.state
        reserved: dict[tuple[str, str], Decimal] = {}
        for o in self._pending.values():
            if o.side is Side.SELL:
                key = (o.sleeve_id, o.symbol)
                reserved[key] = reserved.get(key, Decimal(0)) + o.qty_requested
        for r in requests:
            if r.sleeve_id not in state.cash:
                raise ValueError(f"unknown sleeve {r.sleeve_id}")
            if r.side is Side.SELL:
                key = (r.sleeve_id, r.symbol)
                pos = state.positions.get(r.sleeve_id, {}).get(r.symbol)
                held = pos.qty if pos is not None else Decimal(0)
                reserved[key] = reserved.get(key, Decimal(0)) + r.qty
                if reserved[key] > held:
                    raise ValueError(f"SELL {r.qty} {r.symbol} exceeds available holding {held}")
        out: list[Order] = []
        for r in requests:
            order = Order(
                order_id=self._ids.next_id(),
                sleeve_id=r.sleeve_id,
                symbol=r.symbol,
                side=r.side,
                origin=r.origin,
                qty_requested=r.qty,
                created_asof=asof,
            )
            order = transition(order, OrderStatus.PENDING, qty_accepted=r.qty)
            self._pending[order.order_id] = order
            self._waited[order.order_id] = 0
            self._orders[order.order_id] = order
            out.append(order)
        return out

    def cancel_order(self, order_id: str) -> Order:
        """Cancel a PENDING order. Raises ``IllegalTransition`` if already terminal."""
        order = self._orders[order_id]
        done = transition(order, OrderStatus.CANCELLED)
        self._finish(done)
        return done

    def _finish(self, order: Order) -> None:
        self._pending.pop(order.order_id, None)
        self._waited.pop(order.order_id, None)
        self._orders[order.order_id] = order

    def process_bar(self, asof: date, bars: Mapping[str, Bar]) -> FillResult:
        """Step A: fill pending orders at ``asof`` opens; SELLs first, then BUYs."""
        if self._last_asof is not None and asof <= self._last_asof:
            raise StaleBarError(f"asof {asof} is not after last processed {self._last_asof}")
        # Validate and convert everything first: a bad bar must change no state.
        opens: dict[str, Decimal] = {}
        closes: dict[str, Decimal] = {}
        for sym, bar in bars.items():
            opens[sym] = to_price(bar.open)
            closes[sym] = to_price(bar.close)
            to_price(bar.high)
            to_price(bar.low)
            self._asset_class_of(sym)
        for o in self._pending.values():
            self._asset_class_of(o.symbol)
        self._last_asof = asof
        self._last_close.update(closes)

        fills: list[Order] = []
        rejected: list[Order] = []
        expired: list[Order] = []
        events: list[LedgerEvent] = []
        candidates = [o for o in self.pending_orders if o.created_asof < asof]
        candidates.sort(key=lambda o: (o.side is not Side.SELL, o.order_id))
        for order in candidates:
            self._waited[order.order_id] += 1
            if order.symbol not in opens:
                if self._waited[order.order_id] >= self._config.risk.max_pending_bars:
                    done = transition(order, OrderStatus.EXPIRED)
                    self._finish(done)
                    expired.append(done)
                continue
            done, evs = self._fill(order, asof, opens[order.symbol])
            self._finish(done)
            events.extend(evs)
            (fills if done.status is OrderStatus.FILLED else rejected).append(done)
        return FillResult(
            asof=asof, fills=fills, rejected=rejected, expired=expired, new_ledger_events=events
        )

    def _fill(
        self, order: Order, asof: date, open_price: Decimal
    ) -> tuple[Order, list[LedgerEvent]]:
        ac = self._asset_class_of(order.symbol)
        cost = self._config.costs[ac]
        qty = order.qty_requested
        reason: OrderReason | None = None
        state = self.portfolio.ledger.state
        if order.side is Side.BUY:
            price = costs.buy_fill_price(open_price, cost.slip_bps)
            cash = state.cash[order.sleeve_id]
            if not self._buy_affordable(qty, price, cost.fee_bps, cash):
                qty = self._shrink_buy(
                    qty, price, cost.fee_bps, cash, ac, self._config.equity_qty_quantum
                )
                reason = OrderReason.SHRUNK_CASH
        else:
            price = costs.sell_fill_price(open_price, cost.slip_bps)
            pos = state.positions.get(order.sleeve_id, {}).get(order.symbol)
            held = pos.qty if pos is not None else Decimal(0)
            qty = min(qty, held)  # defensive; submit_orders already checks holdings
        if qty <= 0:
            zero_reason = (
                OrderReason.INSUFFICIENT_CASH
                if order.side is Side.BUY
                else OrderReason.BELOW_MIN_SIZE
            )
            return transition(order, OrderStatus.REJECTED, reason=zero_reason), []
        notional = costs.notional(qty, price)
        fee = costs.fee(notional, cost.fee_bps)
        try:
            evs = self.portfolio.ledger.append_fill(
                asof=asof,
                stage=Stage.OPEN_FILL,
                sleeve_id=order.sleeve_id,
                symbol=order.symbol,
                side=FillSide(order.side.value),
                qty=qty,
                price=price,
                order_id=order.order_id,
                fee=fee,
                slippage_cost=costs.slippage_cost(price, open_price, qty),
            )
        except LedgerError:
            return transition(order, OrderStatus.REJECTED, reason=OrderReason.INSUFFICIENT_CASH), []
        done = transition(
            order,
            OrderStatus.FILLED,
            qty_filled=qty,
            fill_asof=asof,
            fill_price=price,
            fee=fee,
            reason=reason,
        )
        return done, evs

    @staticmethod
    def _buy_affordable(qty: Decimal, price: Decimal, fee_bps: Decimal, cash: Decimal) -> bool:
        notional = costs.notional(qty, price)
        return costs.buy_cash_out(notional, costs.fee(notional, fee_bps)) <= cash

    @classmethod
    def _shrink_buy(
        cls,
        qty: Decimal,
        price: Decimal,
        fee_bps: Decimal,
        cash: Decimal,
        ac: AssetClass,
        equity_quantum: Decimal,
    ) -> Decimal:
        """Largest on-grid qty below ``qty`` whose notional + fee fits in ``cash``."""
        quantum = qty_quantum(ac, equity_quantum)
        with money_context():
            guess = cash / (price * (1 + fee_bps / _BPS))
        q = min(buy_qty(guess, ac, equity_quantum), qty)
        while q > 0 and not cls._buy_affordable(q, price, fee_bps, cash):
            q -= quantum
        return max(q, Decimal(0))

    def snapshot(self) -> PortfolioSnapshot:
        """Equity snapshot marked at the last seen closes (kill-switch state not tracked here)."""
        asof = self._last_asof if self._last_asof is not None else date.min
        crypto = [s for s in self._last_close if self._asset_class_of(s) is AssetClass.CRYPTO]
        return self.portfolio.snapshot(asof, self._last_close, crypto_symbols=crypto)
