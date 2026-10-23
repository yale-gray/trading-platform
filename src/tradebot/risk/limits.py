"""Exposure and cash measurement for the risk checks (SPEC-003 section 6).

Exposure is measured across **all sleeves** at last-close marks (snapshot ``mv``)
plus pending BUY notional valued at the reference close. Pending SELLs are never
credited. Cash reservation is ``qty * ref * (1 + fee + slip)``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import CostConfig
from tradebot.portfolio.models import PortfolioSnapshot
from tradebot.portfolio.money import EQUITY_QTY_QUANTUM, buy_qty, money_context, q2
from tradebot.risk.config import RiskConfig
from tradebot.risk.sizing import SymbolMarket
from tradebot.sim.orders import Side

_BPS = Decimal(10000)
_ZERO = Decimal(0)


@dataclass(frozen=True)
class PendingOrder:
    """A pending (unfilled) order as the limits see it."""

    sleeve_id: str
    symbol: str
    side: Side
    qty: Decimal


def _market(markets: Mapping[str, SymbolMarket], symbol: str) -> SymbolMarket:
    try:
        return markets[symbol]
    except KeyError:
        raise ValueError(f"no market input for {symbol}") from None


def _ref(markets: Mapping[str, SymbolMarket], symbol: str) -> Decimal:
    ref = _market(markets, symbol).ref_price
    if ref is None or ref <= 0:
        raise ValueError(f"no reference price for {symbol}")
    return ref


def held_value(snapshot: PortfolioSnapshot, symbol: str) -> Decimal:
    """Market value of ``symbol`` summed across all sleeves."""
    with money_context():
        return sum(
            (p.mv for s in snapshot.sleeves for p in s.positions if p.symbol == symbol), _ZERO
        )


def pending_buy_notional(
    pending: Sequence[PendingOrder], markets: Mapping[str, SymbolMarket], symbol: str
) -> Decimal:
    """Pending BUY notional of ``symbol`` (all sleeves) at the reference close."""
    with money_context():
        qty = sum((o.qty for o in pending if o.side is Side.BUY and o.symbol == symbol), _ZERO)
        return q2(qty * _ref(markets, symbol)) if qty else _ZERO


def symbol_exposure(
    snapshot: PortfolioSnapshot,
    pending: Sequence[PendingOrder],
    markets: Mapping[str, SymbolMarket],
    symbol: str,
) -> Decimal:
    """Exposure of one symbol across all sleeves including pending BUYs."""
    with money_context():
        return held_value(snapshot, symbol) + pending_buy_notional(pending, markets, symbol)


def crypto_exposure(
    snapshot: PortfolioSnapshot,
    pending: Sequence[PendingOrder],
    markets: Mapping[str, SymbolMarket],
) -> Decimal:
    """Exposure of every crypto symbol across all sleeves including pending BUYs."""
    with money_context():
        symbols = {p.symbol for s in snapshot.sleeves for p in s.positions}
        symbols |= {o.symbol for o in pending if o.side is Side.BUY}
        crypto = sorted(s for s in symbols if _market(markets, s).asset_class is AssetClass.CRYPTO)
        return sum((symbol_exposure(snapshot, pending, markets, s) for s in crypto), _ZERO)


def asset_room(equity: Decimal, risk: RiskConfig, exposure: Decimal) -> Decimal:
    """``0.25 * equity - exposure`` (may be <= 0)."""
    with money_context():
        return risk.max_asset_fraction * equity - exposure


def crypto_room(equity: Decimal, risk: RiskConfig, exposure: Decimal) -> Decimal:
    """``0.30 * equity - crypto exposure`` (may be <= 0)."""
    with money_context():
        return risk.max_crypto_fraction * equity - exposure


def qty_within_notional(
    room: Decimal,
    ref: Decimal,
    asset_class: AssetClass,
    equity_quantum: Decimal = EQUITY_QTY_QUANTUM,
) -> Decimal:
    """Largest grid quantity with ``qty * ref <= room`` (0 if ``room <= 0``)."""
    if room <= 0:
        return _ZERO
    with money_context():
        return buy_qty(room / ref, asset_class, equity_quantum)


def unit_cash_cost(ref: Decimal, cost: CostConfig) -> Decimal:
    """Cash reserved per unit: ``ref * (1 + fee + slip)``."""
    with money_context():
        return ref * (1 + (cost.fee_bps + cost.slip_bps) / _BPS)


def reserved_cash(
    sleeve_id: str,
    pending: Sequence[PendingOrder],
    markets: Mapping[str, SymbolMarket],
    costs: Mapping[AssetClass, CostConfig],
) -> Decimal:
    """Cash reserved by the sleeve's pending BUYs."""
    with money_context():
        total = _ZERO
        for o in pending:
            if o.side is Side.BUY and o.sleeve_id == sleeve_id:
                m = _market(markets, o.symbol)
                total += o.qty * unit_cash_cost(_ref(markets, o.symbol), costs[m.asset_class])
        return total


def available_cash(
    snapshot: PortfolioSnapshot,
    sleeve_id: str,
    pending: Sequence[PendingOrder],
    markets: Mapping[str, SymbolMarket],
    costs: Mapping[AssetClass, CostConfig],
) -> Decimal:
    """``sleeve_cash - reserved_cash`` (no borrowing from other sleeves)."""
    with money_context():
        cash = next(s.cash for s in snapshot.sleeves if s.sleeve_id == sleeve_id)
        return cash - reserved_cash(sleeve_id, pending, markets, costs)
