"""Performance metrics (SPEC-002 section 7). Floats are fine here (analytics, not ledger)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from tradebot.backtest.trades import ClosedTrade
from tradebot.data.symbols import asset_class, normalize_symbol
from tradebot.data.types import AssetClass

__all__ = ["Metrics", "compute_metrics", "periods_per_year"]

NAN = float("nan")


class Metrics(BaseModel):
    """Headline numbers for one equity curve. NaN means "undefined" (never inf)."""

    model_config = ConfigDict(frozen=True)

    total_return: float
    cagr: float
    max_drawdown: float  # <= 0
    sharpe: float
    sortino: float
    win_rate: float
    trade_count: int
    exposure_avg: float
    time_in_market: float
    periods_per_year: int


BARS_PER_SESSION_15M = 26
"""Regular-session 15-minute bars per day (09:30-16:00 ET); early closes have fewer."""


def periods_per_year(symbols_or_classes: Iterable[str], timeframe: str = "1d") -> int:
    """365 if any item is crypto (a symbol like ``BTC/USD`` or the class name), else 252.

    ``timeframe="15m"`` (equities only): 252 sessions x 26 bars = 6552.
    """
    if timeframe == "15m":
        return 252 * BARS_PER_SESSION_15M
    for item in symbols_or_classes:
        if item.upper() == AssetClass.CRYPTO.value:
            return 365
        if item.upper() == AssetClass.EQUITY.value:
            continue
        if asset_class(normalize_symbol(item)) is AssetClass.CRYPTO:
            return 365
    return 252


def _max_drawdown(e: np.ndarray) -> float:
    if len(e) == 0:
        return NAN
    peak = np.maximum.accumulate(e)
    return float(np.min(e / peak - 1.0))


def compute_metrics(
    equity: pd.Series,
    trades: Sequence[ClosedTrade],
    exposure: pd.Series,
    periods_per_year: int,
    rf_annual: float = 0.0,
) -> Metrics:
    """Compute the SPEC-002 section 7 metrics.

    ``equity`` is E_0..E_N on a UTC DatetimeIndex; ``trades`` are closed round trips.
    """
    p = periods_per_year
    e = equity.to_numpy(dtype="float64")
    n = len(e) - 1
    total_return = float(e[-1] / e[0] - 1.0) if n >= 0 and e[0] > 0 else NAN
    cagr = NAN
    if n >= 1 and e[0] > 0:
        years = (equity.index[-1] - equity.index[0]).days / 365.25
        if years > 0:
            cagr = float(max(e[-1], 0.0) / e[0]) ** (1.0 / years) - 1.0
    sharpe = sortino = NAN
    if n >= 1:
        r = e[1:] / e[:-1] - 1.0
        rf_p = (1.0 + rf_annual) ** (1.0 / p) - 1.0
        excess = r - rf_p
        if n >= 2:
            sd = float(np.std(r, ddof=1))
            if sd > 0 and math.isfinite(sd):
                sharpe = float(excess.mean()) / sd * math.sqrt(p)
        dd = math.sqrt(float(np.sum(np.minimum(excess, 0.0) ** 2)) / n)
        if dd > 0:
            sortino = float(excess.mean()) / dd * math.sqrt(p)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    win_rate = wins / len(trades) if trades else NAN
    x = exposure.to_numpy(dtype="float64")
    return Metrics(
        total_return=total_return,
        cagr=cagr,
        max_drawdown=_max_drawdown(e),
        sharpe=sharpe,
        sortino=sortino,
        win_rate=win_rate,
        trade_count=len(trades),
        exposure_avg=float(x.mean()) if len(x) else NAN,
        time_in_market=float((x > 0).mean()) if len(x) else NAN,
        periods_per_year=p,
    )
