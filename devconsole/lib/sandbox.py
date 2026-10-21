"""Cost sandbox: an illustrative buy-then-sell calculator. No ledger, no state.

All arithmetic is delegated to ``tradebot.sim.costs`` and
``tradebot.portfolio.money`` using the costs from ``default_config()``.
Nothing here touches ``float``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import EngineConfig, default_config
from tradebot.portfolio.money import BadQuantityError, buy_qty, money_context, q_ratio
from tradebot.sim import costs


@dataclass(frozen=True)
class TradeBreakdown:
    """Every step of one buy followed by one sell, at the configured costs."""

    asset_class: AssetClass
    qty: Decimal
    fee_bps: Decimal
    slip_bps: Decimal
    buy_open: Decimal
    buy_fill: Decimal
    buy_notional: Decimal
    buy_fee: Decimal
    buy_cash_out: Decimal
    sell_open: Decimal
    sell_fill: Decimal
    sell_notional: Decimal
    sell_fee: Decimal
    sell_cash_in: Decimal
    round_trip_pnl: Decimal
    round_trip_pnl_pct: Decimal
    total_costs: Decimal


def parse_decimal(text: str) -> Decimal:
    """Parse user text such as ``"$1,234.50"`` into a positive finite ``Decimal``.

    Never goes through ``float``. Raises ``ValueError`` for empty text, NaN,
    infinity, zero or negative values.
    """
    if not isinstance(text, str):
        raise ValueError(f"expected text, got {type(text).__name__}")
    cleaned = text.strip().replace("$", "").replace(",", "").strip()
    if not cleaned:
        raise ValueError("enter a number")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"not a number: {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"must be a finite number, got {text!r}")
    if value <= 0:
        raise ValueError(f"must be greater than zero, got {text!r}")
    return value


def _positive(value: Decimal | str, name: str) -> Decimal:
    d = parse_decimal(value) if isinstance(value, str) else value
    if not isinstance(d, Decimal):
        raise ValueError(f"{name} must be a Decimal or text, got {type(d).__name__}")
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{name} must be finite and greater than zero, got {d}")
    return d


def compute_trade(
    asset_class: AssetClass,
    qty: Decimal | str,
    buy_open: Decimal | str,
    sell_open: Decimal | str,
    config: EngineConfig | None = None,
) -> TradeBreakdown:
    """Buy ``qty`` at ``buy_open`` then sell it at ``sell_open`` (illustrative).

    Fill prices, notionals and fees come from ``tradebot.sim.costs``. Slippage is
    already inside the fill prices, so ``round_trip_pnl`` is exactly
    ``sell_cash_in - buy_cash_out``; ``slippage_cost`` is informational and is
    only added into ``total_costs`` (with both fees), never subtracted twice.

    Raises ``ValueError``/``MoneyError`` on bad input (zero, negative, NaN,
    fractional equity shares); no result is returned in that case.
    """
    cfg = config if config is not None else default_config()
    cost = cfg.costs[asset_class]
    q_in = _positive(qty, "quantity")
    q = buy_qty(q_in, asset_class)
    if q != q_in:
        raise BadQuantityError(f"quantity {q_in} is not allowed for {asset_class}: use {q}")
    if q <= 0:
        raise BadQuantityError(f"quantity must be greater than zero, got {q_in}")
    b_open = _positive(buy_open, "buy open price")
    s_open = _positive(sell_open, "sell open price")

    buy_fill = costs.buy_fill_price(b_open, cost.slip_bps)
    buy_notional = costs.notional(q, buy_fill)
    buy_fee = costs.fee(buy_notional, cost.fee_bps)
    cash_out = costs.buy_cash_out(buy_notional, buy_fee)

    sell_fill = costs.sell_fill_price(s_open, cost.slip_bps)
    sell_notional = costs.notional(q, sell_fill)
    sell_fee = costs.fee(sell_notional, cost.fee_bps)
    cash_in = costs.sell_cash_in(sell_notional, sell_fee)

    with money_context():
        pnl = cash_in - cash_out
        pnl_pct = q_ratio(pnl / cash_out)
        total = (
            buy_fee
            + sell_fee
            + costs.slippage_cost(buy_fill, b_open, q)
            + costs.slippage_cost(sell_fill, s_open, q)
        )

    return TradeBreakdown(
        asset_class=asset_class,
        qty=q,
        fee_bps=cost.fee_bps,
        slip_bps=cost.slip_bps,
        buy_open=b_open,
        buy_fill=buy_fill,
        buy_notional=buy_notional,
        buy_fee=buy_fee,
        buy_cash_out=cash_out,
        sell_open=s_open,
        sell_fill=sell_fill,
        sell_notional=sell_notional,
        sell_fee=sell_fee,
        sell_cash_in=cash_in,
        round_trip_pnl=pnl,
        round_trip_pnl_pct=pnl_pct,
        total_costs=total,
    )
