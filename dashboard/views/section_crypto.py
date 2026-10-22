"""Crypto section page."""

from __future__ import annotations

from dashboard.views.section_page import render_section

EXPLAINER = """
The crypto strategy is **trend-following** on three coins: **BTC** (bitcoin), **ETH** (ether)
and **SOL** (solana). When a coin's price has been rising over a while it may hold it, and
when the trend turns down it sells. Crypto prices swing hard, so expect large ups and downs.
"""


def render() -> None:
    render_section("Crypto", EXPLAINER)
