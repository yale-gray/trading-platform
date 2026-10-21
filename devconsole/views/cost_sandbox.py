"""Money & costs sandbox page: an illustrative buy-then-sell calculator.

All money math comes from ``devconsole.lib.sandbox.compute_trade``. This page only
reads the inputs, shows the results as exact decimal strings and reports errors.
Nothing is saved, no ledger is touched and nothing is traded.
"""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from devconsole.lib.sandbox import TradeBreakdown, compute_trade
from devconsole.ui import explainer, term
from tradebot.data.types import AssetClass
from tradebot.portfolio.config import default_config

_ASSET_LABELS = {
    AssetClass.EQUITY: "Stock / ETF",
    AssetClass.CRYPTO: "Crypto",
}


def _text(value: Decimal) -> str:
    """Exact decimal string with no exponent notation. Never goes through float."""
    return format(value, "f")


def _percent(ratio: Decimal) -> str:
    """Show a ratio as a percent. ``scaleb(2)`` is an exact shift of the decimal point."""
    return _text(ratio.scaleb(2)) + "%"


def _results_table(trade: TradeBreakdown) -> list[dict[str, str]]:
    """Buy and sell side by side. Every value is a string taken from ``trade``."""
    return [
        {
            "Step": "Quote (open) price",
            "Buy": _text(trade.buy_open),
            "Sell": _text(trade.sell_open),
        },
        {
            "Step": "Fill price (after slippage)",
            "Buy": _text(trade.buy_fill),
            "Sell": _text(trade.sell_fill),
        },
        {
            "Step": "Notional (quantity x fill)",
            "Buy": _text(trade.buy_notional),
            "Sell": _text(trade.sell_notional),
        },
        {"Step": "Fee", "Buy": _text(trade.buy_fee), "Sell": _text(trade.sell_fee)},
        {
            "Step": "Slippage (rate, in bps)",
            "Buy": _text(trade.slip_bps),
            "Sell": _text(trade.slip_bps),
        },
        {
            "Step": "Cash out (buy) / cash in (sell)",
            "Buy": _text(trade.buy_cash_out),
            "Sell": _text(trade.sell_cash_in),
        },
    ]


def _show_costs() -> None:
    """Current simulator costs, read from the default config (display only)."""
    config = default_config()
    lines = []
    for asset_class, label in _ASSET_LABELS.items():
        cost = config.costs[asset_class]
        lines.append(
            f"- **{label}**: fee {_text(cost.fee_bps)} bps, slippage {_text(cost.slip_bps)} bps"
        )
    st.markdown("**Current simulator costs**\n" + "\n".join(lines))
    term("bp")
    term("slippage")


def render() -> None:
    st.title("Money & costs sandbox")
    explainer(
        "Every simulated trade pays a small fee and gets a slightly worse price than the quote "
        "(slippage). Try a buy and a sell to see exactly where the money goes. This is a "
        "calculator: nothing is saved or traded."
    )
    st.info("Prices here are numbers you type. Nothing is downloaded or saved.")

    left, right = st.columns(2)
    with left:
        asset_class = st.radio(
            "Asset type",
            list(_ASSET_LABELS),
            format_func=_ASSET_LABELS.__getitem__,
            key="sandbox_asset_class",
            help="Stocks and ETFs trade in whole shares. Crypto can trade fractions.",
        )
        qty_text = st.text_input(
            "Quantity",
            value="10",
            key="sandbox_qty",
            help="How many shares or coins to buy, then sell.",
        )
    with right:
        buy_text = st.text_input(
            "Buy open price",
            value="100",
            key="sandbox_buy_open",
            help="The price you pay per unit before fees and slippage.",
        )
        sell_text = st.text_input(
            "Sell open price",
            value="110",
            key="sandbox_sell_open",
            help="The price you sell at per unit before fees and slippage.",
        )

    _show_costs()

    try:
        trade = compute_trade(asset_class, qty_text, buy_text, sell_text)
        table = _results_table(trade)
        pnl_text = _text(trade.round_trip_pnl)
        pct_text = _percent(trade.round_trip_pnl_pct)
        costs_text = _text(trade.total_costs)
    except ValueError as exc:
        st.error(f"Check your inputs: {exc}")
        st.caption(_footer())
        return
    except Exception:  # decimal overflow/underflow and similar: show plain words, not class names
        st.error("That number is too small or too large to price; try a normal price like 100.")
        st.caption(_footer())
        return

    st.subheader("Buy and sell, step by step")
    st.table(table)

    st.subheader("Round trip")
    col_pnl, col_pct, col_costs = st.columns(3)
    col_pnl.metric("Round-trip P&L ($)", pnl_text)
    col_pct.metric("Round-trip P&L (%)", pct_text)
    col_costs.metric("Total costs ($)", costs_text)
    st.caption("Total costs = both fees plus both slippage amounts. P&L = cash in minus cash out.")
    st.caption(_footer())


def _footer() -> str:
    return (
        "Illustrative only. Starting prices are the day's open because the simulator fills at "
        "the next bar's open (no peeking ahead). These numbers touch no ledger and place no trade."
    )
