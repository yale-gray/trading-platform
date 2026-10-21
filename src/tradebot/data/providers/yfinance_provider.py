"""Equity and ETF daily bars from yfinance (unofficial Yahoo scraper, no API keys).

Prices are fetched unadjusted with ``Adj Close`` alongside, and one factor
``adj = Adj Close / Close`` is applied to open, high, low and close alike (ADR-0001 D2).
Volume is left as reported.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from tradebot.clock import Clock, SystemClock
from tradebot.data.calendar import XNYS_END, XNYS_START, last_closed_bar_date, sessions
from tradebot.data.errors import ProviderError
from tradebot.data.symbols import asset_class, to_yahoo
from tradebot.data.types import BAR_COLUMNS, AssetClass, Symbol, Timeframe

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 1.0
_PRICE_FIELDS = ("Open", "High", "Low", "Close", "Adj Close")


class YFinanceProvider:
    """Daily bars for equity/ETF symbols via ``yfinance.download``.

    ``sleep`` and ``download`` are injectable so offline tests never wait or touch
    the network. ``download`` takes the same arguments as ``yfinance.download``.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        download: Callable[..., Any] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_base_s: float = BACKOFF_BASE_S,
        equity_close_buffer_min: int = 60,
    ) -> None:
        self.name = "yfinance"
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._download = download
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._buffer_min = equity_close_buffer_min

    def supports(self, symbol: Symbol) -> bool:
        return asset_class(symbol) is AssetClass.EQUITY

    def _get_download(self) -> Callable[..., Any]:
        if self._download is None:
            import yfinance as yf  # lazy: keeps yfinance out of import time and offline tests

            self._download = yf.download
        return self._download

    def _download_batch(
        self, tickers: list[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        """One batch download (``end`` inclusive), retried on exceptions or empty results."""
        download = self._get_download()
        # yfinance treats ``end`` as exclusive, so ask for one extra day.
        end_excl = end + timedelta(days=1)
        for attempt in range(self._max_attempts):
            try:
                raw = download(
                    tickers,
                    start=start.isoformat(),
                    end=end_excl.isoformat(),
                    auto_adjust=False,
                    repair=False,
                    progress=False,
                    group_by="ticker",
                )
                per_ticker = _split_by_ticker(raw, tickers)
                if any(not frame.empty for frame in per_ticker.values()):
                    return per_ticker
                reason = "empty frame"
            except Exception as exc:
                reason = repr(exc)
            if attempt == self._max_attempts - 1:
                raise ProviderError(
                    f"{self.name}: {','.join(tickers)} failed after {self._max_attempts} "
                    f"attempts: {reason}"
                )
            self._sleep(self._backoff_base_s * 2**attempt)
        raise ProviderError("unreachable")  # pragma: no cover

    def fetch_bars(
        self,
        symbol: Symbol,
        start: date,
        end: date,
        timeframe: Timeframe = Timeframe.D1,
    ) -> pd.DataFrame:
        """Closed daily bars with label date in [start, end], canonical schema."""
        return self.fetch_many([symbol], start, end, timeframe)[symbol]

    def fetch_many(
        self,
        symbols: Sequence[Symbol],
        start: date,
        end: date,
        timeframe: Timeframe = Timeframe.D1,
    ) -> dict[Symbol, pd.DataFrame]:
        """Closed daily bars for many symbols from one batch download."""
        if timeframe is not Timeframe.D1:
            raise ProviderError(f"{self.name}: unsupported timeframe {timeframe}")
        unique = list(dict.fromkeys(symbols))
        for symbol in unique:
            if not self.supports(symbol):
                raise ProviderError(f"{self.name}: unsupported symbol {symbol}")
        if not unique:
            return {}

        if not _has_sessions(start, end):
            return {symbol: _empty_frame() for symbol in unique}

        tickers = [to_yahoo(s) for s in unique]
        per_ticker = self._download_batch(tickers, start, end)
        now = self._clock.now()
        out: dict[Symbol, pd.DataFrame] = {}
        for symbol in unique:
            raw = per_ticker[to_yahoo(symbol)]
            frame = _to_frame(raw, symbol, start, end, now, self._buffer_min)
            if raw.empty:
                message = f"{self.name}: no data returned for {symbol} in batch of {len(tickers)}"
                log.warning(message)
                frame.attrs["provider_warnings"] = [message]
            out[symbol] = frame
        return out


def _has_sessions(start: date, end: date) -> bool:
    """True if the XNYS has at least one session in ``[start, end]`` (clamped to its bounds)."""
    lo = max(start, XNYS_START.date())
    hi = min(end, XNYS_END.date())
    return lo <= hi and len(sessions("SPY", lo, hi)) > 0


def _split_by_ticker(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Map each requested ticker to its own field-level frame (missing ticker -> empty)."""
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return {t: pd.DataFrame() for t in tickers}
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        for t in tickers:
            out[t] = raw[t].copy() if t in level0 else pd.DataFrame()
    else:
        # Flat columns only happen for a single-ticker download.
        for t in tickers:
            out[t] = raw.copy() if len(tickers) == 1 else pd.DataFrame()
    return out


def _to_frame(
    raw: pd.DataFrame,
    symbol: Symbol,
    start: date,
    end: date,
    now: datetime,
    buffer_min: int = 60,
) -> pd.DataFrame:
    """Adjusted OHLCV with UTC-midnight session labels, closed bars only."""
    if raw.empty:
        return _empty_frame()
    missing = [f for f in _PRICE_FIELDS + ("Volume",) if f not in raw.columns]
    if missing:
        raise ProviderError(f"yfinance: {symbol} missing columns {missing}")

    prices = raw[list(_PRICE_FIELDS + ("Volume",))].astype(float).dropna(subset=list(_PRICE_FIELDS))
    if prices.empty:
        return _empty_frame()

    # One factor for all four prices keeps O/H/L/C on the same total-return basis.
    factor = (prices["Adj Close"] / prices["Close"]).to_numpy()
    frame = pd.DataFrame(
        {
            "open": prices["Open"].to_numpy() * factor,
            "high": prices["High"].to_numpy() * factor,
            "low": prices["Low"].to_numpy() * factor,
            "close": prices["Close"].to_numpy() * factor,
            "volume": prices["Volume"].to_numpy(),
        },
        index=_session_labels(prices.index),
        columns=list(BAR_COLUMNS),
    )

    # Drop the in-progress session, then keep the requested label range.
    last_closed = last_closed_bar_date(symbol, now, buffer_min)
    upper = min(end, last_closed)
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(upper, tz="UTC")
    frame = frame[(frame.index >= lo) & (frame.index <= hi)]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame.astype(float)


def _session_labels(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Session date at 00:00 UTC. yfinance stamps daily bars at exchange-local midnight."""
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)  # keep the local wall-clock date
    return pd.DatetimeIndex(idx.normalize()).tz_localize("UTC").rename("ts")


def _empty_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame(columns=list(BAR_COLUMNS), index=index, dtype=float)
