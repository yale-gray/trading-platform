"""Per-symbol slippage tiers (ADR-0001 A23).

Figures are basis points per side. They come from research report 03 (some are
estimates) and are pinned by ``tests/sim/test_cost_tiers.py``; changing one is an ADR edit.
The mega-cap list is a 2026 snapshot of the largest S&P 500 weights.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

CostProfile = Literal["flat", "tiered", "tiered_stress"]

CORE_ETF_BPS = Decimal("1")
LEVERAGED_ETF_BPS = Decimal("3")
MEGA_CAP_BPS = Decimal("1.5")
OTHER_EQUITY_BPS = Decimal("3")
MAJOR_CRYPTO_BPS = Decimal("15")
OTHER_CRYPTO_BPS = Decimal("30")
STRESS_MULTIPLIER = Decimal(2)
SELL_REG_FEE_BPS = Decimal("0.25")  # equities, SELL fills only, tiered profiles

CORE_ETFS: frozenset[str] = frozenset({"SPY", "QQQ", "DIA", "IWM"})
LEVERAGED_ETFS: frozenset[str] = frozenset(
    {
        "TQQQ",
        "SQQQ",
        "SPXL",
        "SPXS",
        "UPRO",
        "SPXU",
        "SSO",
        "SDS",
        "SH",
        "QLD",
        "PSQ",
        "SOXL",
        "SOXS",
    }  # fmt: skip
)
MEGA_CAPS: frozenset[str] = frozenset(
    {
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "GOOG",
        "TSLA",
        "AVGO",
        "BRK.B",
        "JPM",
        "LLY",
        "V",
        "XOM",
        "UNH",
        "MA",
        "COST",
        "HD",
        "PG",
        "JNJ",
        "ABBV",
        "WMT",
        "NFLX",
        "BAC",
        "CRM",
        "ORCL",
        "CVX",
        "KO",
        "AMD",
        "MRK",
        "PEP",
        "TMO",
        "ADBE",
        "CSCO",
        "ACN",
        "LIN",
        "MCD",
        "ABT",
        "WFC",
        "DIS",
        "INTC",
        "QCOM",
        "GE",
        "CAT",
        "IBM",
        "TXN",
        "PFE",
        "T",
        "VZ",
        "AMGN",
    }  # fmt: skip
)
MAJOR_CRYPTO: frozenset[str] = frozenset({"BTC/USD", "ETH/USD"})


def tier_slip_bps(symbol: str, profile: CostProfile) -> Decimal:
    """Slippage in bps per side for a normalized symbol under a tiered profile."""
    if "/" in symbol:
        bps = MAJOR_CRYPTO_BPS if symbol in MAJOR_CRYPTO else OTHER_CRYPTO_BPS
    elif symbol in CORE_ETFS:
        bps = CORE_ETF_BPS
    elif symbol in LEVERAGED_ETFS:
        bps = LEVERAGED_ETF_BPS
    elif symbol in MEGA_CAPS:
        bps = MEGA_CAP_BPS
    else:
        bps = OTHER_EQUITY_BPS
    return bps * STRESS_MULTIPLIER if profile == "tiered_stress" else bps


def tier_extra_fee_bps(symbol: str, is_sell: bool, profile: CostProfile) -> Decimal:
    """Extra fee bps on top of ``CostConfig.fee_bps``: regulatory fee, equity SELL only."""
    if profile != "flat" and is_sell and "/" not in symbol:
        return SELL_REG_FEE_BPS
    return Decimal(0)
