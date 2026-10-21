"""Sizing plus the R0-R6 risk pipeline for one sleeve (SPEC-003 sections 5-6).

Pure: takes plain inputs and returns accepted orders plus :class:`Rejection`
records. Limit breaches (R3, R4, R5) shrink; hard blocks reject. SELLs are never
vetoed by R0 or R3-R5; they are clamped to holdings and subject to R6 only
(full exits exempt). Pending BUYs accumulate within the call.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import CostConfig, SleeveConfig
from tradebot.portfolio.models import PortfolioSnapshot
from tradebot.portfolio.money import check_sell_qty, money_context, q2
from tradebot.risk.config import RiskConfig
from tradebot.risk.limits import (
    PendingOrder,
    asset_room,
    available_cash,
    crypto_exposure,
    crypto_room,
    qty_within_notional,
    symbol_exposure,
    unit_cash_cost,
)
from tradebot.risk.sizing import (
    SymbolMarket,
    plain_weights,
    target_quantity,
    target_value,
    vol_scaled_weights,
)
from tradebot.sim.orders import OrderOrigin, OrderReason, OrderRequest, Side

_ZERO = Decimal(0)


@dataclass(frozen=True)
class Rejection:
    """A veto or shrink: ``accepted`` is 0 for a reject, ``< requested`` for a shrink."""

    symbol: str
    reason: OrderReason
    requested: Decimal
    accepted: Decimal


@dataclass(frozen=True)
class CheckResult:
    """Accepted orders (sells first, then buys, alphabetical within) and rejections."""

    orders: tuple[OrderRequest, ...]
    rejections: tuple[Rejection, ...]
    target_qty: Mapping[str, Decimal] = field(default_factory=dict)
    """Target share quantity per sized symbol (0 for exit/absent symbols)."""


def check_target_weights(
    *,
    sleeve: SleeveConfig,
    weights: Mapping[str, Decimal],
    snapshot: PortfolioSnapshot,
    markets: Mapping[str, SymbolMarket],
    risk: RiskConfig,
    costs: Mapping[AssetClass, CostConfig],
    pending: Sequence[PendingOrder] = (),
    stopped_out: Collection[str] = (),
    sizing_equity: Decimal | None = None,
    origin: OrderOrigin = OrderOrigin.STRATEGY,
) -> CheckResult:
    """Size a sleeve's target weights and run R0-R6 on the resulting deltas.

    ``snapshot`` is the last-close snapshot (equity, per-sleeve cash/positions,
    ``killed``). ``markets`` must cover every weight symbol, held symbol and
    pending-order symbol. ``pending`` holds all unfilled orders of all sleeves
    (earlier accepted orders from other calls included). ``sizing_equity``
    defaults to the sleeve's equity at the last close. Symbols are processed
    alphabetically; absent held symbols have target 0.
    """
    with money_context():
        return _run(
            sleeve,
            weights,
            snapshot,
            markets,
            risk,
            costs,
            pending,
            frozenset(stopped_out),
            sizing_equity,
            origin,
        )


def _run(
    sleeve: SleeveConfig,
    weights: Mapping[str, Decimal],
    snapshot: PortfolioSnapshot,
    markets: Mapping[str, SymbolMarket],
    risk: RiskConfig,
    costs: Mapping[AssetClass, CostConfig],
    pending_in: Sequence[PendingOrder],
    stopped_out: frozenset[str],
    sizing_equity_in: Decimal | None,
    origin: OrderOrigin,
) -> CheckResult:
    sleeve_snap = next((s for s in snapshot.sleeves if s.sleeve_id == sleeve.sleeve_id), None)
    if sleeve_snap is None:
        raise ValueError(f"sleeve {sleeve.sleeve_id} not in snapshot")
    seq_equity = sleeve_snap.equity if sizing_equity_in is None else sizing_equity_in
    held = {p.symbol: p.qty for p in sleeve_snap.positions}
    pending = list(pending_in)
    rejections: list[Rejection] = []
    target_qty: dict[str, Decimal] = {}
    sells: list[OrderRequest] = []
    buys: list[OrderRequest] = []

    for sym, w in weights.items():
        mk = markets.get(sym)
        if mk is not None and w > 0 and mk.asset_class not in sleeve.allowed_asset_classes:
            raise ValueError(f"{sym} is outside sleeve {sleeve.sleeve_id} allowed asset classes")

    # R1 (data part) + R2: drop what cannot be sized, then inverse-vol tilt.
    base = plain_weights(weights)
    no_data = [
        s
        for s, w in base.items()
        if w > 0
        and (
            s not in markets
            or not markets[s].tradable
            or markets[s].ref_price is None
            or markets[s].ref_price <= 0  # type: ignore[operator]
        )
    ]
    sizable = {s: w for s, w in base.items() if s not in no_data}
    if sleeve.vol_scaled:
        scaled, no_vol = vol_scaled_weights(
            sizable, {s: markets[s].vol20 for s in sizable if s in markets}
        )
    else:
        scaled, no_vol = sizable, ()
    for s in no_data:
        rejections.append(Rejection(s, OrderReason.NO_DATA, _ZERO, _ZERO))
    for s in no_vol:
        rejections.append(Rejection(s, OrderReason.NO_VOL_DATA, _ZERO, _ZERO))
    blocked = set(no_data) | set(no_vol)

    symbols = sorted((set(scaled) | set(held) | {o.symbol for o in pending}) - blocked)
    for sym in symbols:
        w2 = scaled.get(sym, _ZERO)
        mine_buy = sum(
            (
                o.qty
                for o in pending
                if o.sleeve_id == sleeve.sleeve_id and o.symbol == sym and o.side is Side.BUY
            ),
            _ZERO,
        )
        mine_sell = sum(
            (
                o.qty
                for o in pending
                if o.sleeve_id == sleeve.sleeve_id and o.symbol == sym and o.side is Side.SELL
            ),
            _ZERO,
        )
        have = held.get(sym, _ZERO)
        proj = have + mine_buy - mine_sell
        sellable = have - mine_sell
        m = markets.get(sym)
        if w2 == 0:
            target_qty[sym] = _ZERO
            # Exit / absent: full exit of what is sellable; no price needed.
            if sellable > 0 and proj > 0:
                qty = min(sellable, proj)
                sells.append(_sell(sleeve, sym, qty, m, origin))
                pending.append(PendingOrder(sleeve.sleeve_id, sym, Side.SELL, qty))
            continue
        assert m is not None and m.ref_price is not None
        ref = m.ref_price
        tval = target_value(w2, seq_equity)
        tqty = target_quantity(tval, ref, m.asset_class)
        target_qty[sym] = tqty
        min_notional = sleeve.min_trade_fraction * seq_equity

        if tqty < proj:
            qty = min(proj - tqty, sellable)
            if qty <= 0:
                continue
            full_exit = qty == proj
            if not full_exit and q2(qty * ref) < min_notional:
                rejections.append(Rejection(sym, OrderReason.BELOW_MIN_SIZE, qty, _ZERO))
                continue
            sells.append(_sell(sleeve, sym, qty, m, origin))
            pending.append(PendingOrder(sleeve.sleeve_id, sym, Side.SELL, qty))
            continue

        requested = tqty - proj
        if requested == 0:
            if proj == 0 and tval > 0:  # sub-share entry
                rejections.append(Rejection(sym, OrderReason.BELOW_MIN_SIZE, _ZERO, _ZERO))
            continue

        # ---- BUY pipeline ----
        qty = requested

        def reject(reason: OrderReason, sym: str = sym, requested: Decimal = requested) -> None:
            rejections.append(Rejection(sym, reason, requested, _ZERO))

        if snapshot.killed:  # R0
            reject(OrderReason.KILL_SWITCH)
            continue
        if sym in stopped_out:  # R1 (stop lock)
            reject(OrderReason.STOP_LOCK)
            continue
        equity = snapshot.equity
        # R3: per-asset cap across all sleeves incl. pending BUYs.
        room = asset_room(equity, risk, symbol_exposure(snapshot, pending, markets, sym))
        if room <= 0:
            reject(OrderReason.MAX_ASSET)
            continue
        if qty * ref > room:
            qty = qty_within_notional(room, ref, m.asset_class)
            rejections.append(Rejection(sym, OrderReason.MAX_ASSET_SHRUNK, requested, qty))
        # R4: crypto cap.
        if m.asset_class is AssetClass.CRYPTO and qty > 0:
            croom = crypto_room(equity, risk, crypto_exposure(snapshot, pending, markets))
            if croom <= 0:
                reject(OrderReason.MAX_CRYPTO)
                continue
            if qty * ref > croom:
                qty = qty_within_notional(croom, ref, m.asset_class)
                rejections.append(Rejection(sym, OrderReason.MAX_CRYPTO_SHRUNK, requested, qty))
        # R5: sleeve cash minus reservations.
        if qty > 0:
            avail = available_cash(snapshot, sleeve.sleeve_id, pending, markets, costs)
            if avail <= 0:
                reject(OrderReason.INSUFFICIENT_CASH)
                continue
            unit = unit_cash_cost(ref, costs[m.asset_class])
            if qty * unit > avail:
                qty = qty_within_notional(avail, unit, m.asset_class)
                rejections.append(Rejection(sym, OrderReason.CASH_SHRUNK, requested, qty))
        # R6: minimum size (qty already on the grid).
        if qty <= 0 or q2(qty * ref) < min_notional:
            reject(OrderReason.BELOW_MIN_SIZE)
            continue
        buys.append(
            OrderRequest(
                sleeve_id=sleeve.sleeve_id, symbol=sym, side=Side.BUY, qty=qty, origin=origin
            )
        )
        pending.append(PendingOrder(sleeve.sleeve_id, sym, Side.BUY, qty))

    return CheckResult(tuple(sells + buys), tuple(rejections), target_qty)


def _sell(
    sleeve: SleeveConfig,
    symbol: str,
    qty: Decimal,
    market: SymbolMarket | None,
    origin: OrderOrigin,
) -> OrderRequest:
    if market is not None:
        check_sell_qty(qty, market.asset_class)
    return OrderRequest(
        sleeve_id=sleeve.sleeve_id, symbol=symbol, side=Side.SELL, qty=qty, origin=origin
    )
