"""Trades, equity and exposure derived from the ledger (SPEC-002 section 6b, ADR-0001 D3).

Pure functions over ``LedgerEvent``, ``Order`` and ``PortfolioSnapshot``: no I/O, no clock.
Money stays ``Decimal`` until the series conversion at the end.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict

from tradebot.portfolio.asof import AsOf, asof_timestamp
from tradebot.portfolio.models import EventType, FillSide, LedgerEvent, PortfolioSnapshot
from tradebot.portfolio.money import PRICE_Q, money_context
from tradebot.sim.orders import Order, OrderOrigin

__all__ = [
    "ClosedTrade",
    "OpenPosition",
    "closed_trades",
    "equity_series",
    "exit_reason_for",
    "exposure_series",
    "open_positions",
]

ExitReason = Literal["signal", "stop", "rebalance", "manual", "flatten"]

_EXIT_REASONS: dict[OrderOrigin, ExitReason] = {
    OrderOrigin.STRATEGY: "signal",
    OrderOrigin.STOP: "stop",
    OrderOrigin.REBALANCE: "rebalance",
    OrderOrigin.MANUAL: "manual",
    OrderOrigin.FLATTEN: "flatten",
}


class ClosedTrade(BaseModel):
    """One completed round trip (flat -> long -> flat) within a sleeve."""

    model_config = ConfigDict(frozen=True)

    sleeve_id: str
    symbol: str
    entry_asof: AsOf
    exit_asof: AsOf
    qty: Decimal  # total bought during the trip
    entry_price: Decimal  # quantity-weighted average buy fill price
    exit_price: Decimal  # quantity-weighted average sell fill price
    fees: Decimal
    net_pnl: Decimal  # sum of realized_pnl on the SELL fills minus fees
    exit_reason: ExitReason


class OpenPosition(BaseModel):
    """A position still open at the last snapshot (never force-closed)."""

    model_config = ConfigDict(frozen=True)

    sleeve_id: str
    symbol: str
    qty: Decimal
    entry_asof: AsOf
    mv: Decimal


def exit_reason_for(origin: OrderOrigin) -> ExitReason:
    """Map the origin of a closing SELL order to an ``exit_reason``."""
    return _EXIT_REASONS[origin]


class _Trip:
    """Accumulator for one open round trip."""

    __slots__ = (
        "bought",
        "buy_value",
        "entry_asof",
        "order_ids",
        "pnl",
        "qty",
        "sell_value",
        "sold",
    )

    def __init__(self, entry_asof: AsOf) -> None:
        self.entry_asof = entry_asof
        self.qty = Decimal(0)  # running position
        self.bought = Decimal(0)
        self.buy_value = Decimal(0)
        self.sold = Decimal(0)
        self.sell_value = Decimal(0)
        self.pnl = Decimal(0)
        self.order_ids: set[str] = set()


def _avg(value: Decimal, qty: Decimal) -> Decimal:
    with money_context():
        return (value / qty).quantize(PRICE_Q, rounding=ROUND_HALF_EVEN)


def closed_trades(ledger: Sequence[LedgerEvent], orders: Mapping[str, Order]) -> list[ClosedTrade]:
    """Closed round trips from the ledger, ordered by (exit_asof, sleeve_id, symbol, seq).

    ``orders`` maps ``order_id`` to its ``Order`` (the source of ``exit_reason``).
    Initial DEPOSIT events carry ``asof = date.min`` and are ignored here.
    """
    events = sorted(ledger, key=lambda e: e.seq)
    fees_by_order: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for ev in events:
        if ev.type is EventType.FEE and ev.order_id is not None:
            with money_context():
                fees_by_order[ev.order_id] += ev.amount
    open_trips: dict[tuple[str, str], _Trip] = {}
    out: list[tuple[AsOf, str, str, int, ClosedTrade]] = []
    with money_context():
        for ev in events:
            if ev.type is not EventType.FILL:
                continue
            assert ev.symbol and ev.qty is not None and ev.price is not None and ev.order_id
            key = (ev.sleeve_id, ev.symbol)
            if ev.side is FillSide.BUY:
                trip = open_trips.get(key)
                if trip is None:
                    trip = open_trips[key] = _Trip(ev.asof)
                trip.qty += ev.qty
                trip.bought += ev.qty
                trip.buy_value += ev.qty * ev.price
                trip.order_ids.add(ev.order_id)
                continue
            trip = open_trips.get(key)
            if trip is None:
                raise ValueError(f"SELL fill seq {ev.seq} with no open position in {key}")
            trip.qty -= ev.qty
            if trip.qty < 0:
                raise ValueError(f"SELL fill seq {ev.seq} oversells {key}")
            trip.sold += ev.qty
            trip.sell_value += ev.qty * ev.price
            trip.pnl += ev.realized_pnl if ev.realized_pnl is not None else Decimal(0)
            trip.order_ids.add(ev.order_id)
            if trip.qty == 0:
                del open_trips[key]
                fees = sum((fees_by_order.get(o, Decimal(0)) for o in trip.order_ids), Decimal(0))
                order = orders.get(ev.order_id)
                if order is None:
                    raise KeyError(f"closing order {ev.order_id} missing from the orders map")
                trade = ClosedTrade(
                    sleeve_id=ev.sleeve_id,
                    symbol=ev.symbol,
                    entry_asof=trip.entry_asof,
                    exit_asof=ev.asof,
                    qty=trip.bought,
                    entry_price=_avg(trip.buy_value, trip.bought),
                    exit_price=_avg(trip.sell_value, trip.sold),
                    fees=fees,
                    net_pnl=trip.pnl - fees,
                    exit_reason=exit_reason_for(order.origin),
                )
                out.append((ev.asof, ev.sleeve_id, ev.symbol, ev.seq, trade))
    out.sort(key=lambda t: t[:4])
    return [t[4] for t in out]


def open_positions(snapshot: PortfolioSnapshot) -> list[OpenPosition]:
    """Positions with qty > 0 in the snapshot, sorted by (sleeve_id, symbol)."""
    rows = [
        OpenPosition(
            sleeve_id=s.sleeve_id,
            symbol=p.symbol,
            qty=p.qty,
            entry_asof=p.entry_asof,  # type: ignore[arg-type]
            mv=p.mv,
        )
        for s in snapshot.sleeves
        for p in s.positions
        if p.qty > 0
    ]
    return sorted(rows, key=lambda r: (r.sleeve_id, r.symbol))


def _index(snapshots: Sequence[PortfolioSnapshot]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([asof_timestamp(s.asof) for s in snapshots])


def equity_series(snapshots: Sequence[PortfolioSnapshot]) -> pd.Series:
    """One float equity point per snapshot, indexed by ``asof`` (UTC midnight, or the bar open)."""
    return pd.Series(
        [float(s.equity) for s in snapshots],
        index=_index(snapshots),
        dtype="float64",
        name="equity",
    )


def exposure_series(snapshots: Sequence[PortfolioSnapshot]) -> pd.Series:
    """Gross position market value over equity, per snapshot."""
    vals: list[float] = []
    for s in snapshots:
        with money_context():
            mv = sum((p.mv for sl in s.sleeves for p in sl.positions), Decimal(0))
            vals.append(float(mv / s.equity) if s.equity > 0 else 0.0)
    return pd.Series(vals, index=_index(snapshots), dtype="float64", name="exposure")
