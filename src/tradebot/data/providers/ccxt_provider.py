"""Crypto daily bars from public ccxt OHLCV endpoints (no API keys)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from tradebot.clock import Clock, SystemClock
from tradebot.data.errors import ProviderError
from tradebot.data.symbols import asset_class
from tradebot.data.types import BAR_COLUMNS, AssetClass, Symbol, Timeframe

PAGE_CANDLES = 300
MIN_PAGE_INTERVAL_S = 0.5
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 1.0
_DAY_MS = 86_400_000


class CcxtProvider:
    """Daily OHLCV for ``*/USD`` crypto symbols via a ccxt exchange.

    ``sleep`` is injectable so offline tests never actually wait. ``exchange``
    may be passed to bypass the lazy ccxt import (tests use a fake object).
    """

    def __init__(
        self,
        exchange_id: str = "coinbaseexchange",
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        exchange: Any | None = None,
        page_interval_s: float = MIN_PAGE_INTERVAL_S,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_base_s: float = BACKOFF_BASE_S,
    ) -> None:
        self.name = f"ccxt:{exchange_id}"
        self.exchange_id = exchange_id
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._exchange = exchange
        self._page_interval_s = max(page_interval_s, MIN_PAGE_INTERVAL_S)
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s

    def supports(self, symbol: Symbol) -> bool:
        return asset_class(symbol) is AssetClass.CRYPTO

    def _get_exchange(self) -> Any:
        if self._exchange is None:
            import ccxt  # lazy: keeps ccxt out of import time and offline tests

            self._exchange = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})
        return self._exchange

    def _fetch_page(self, symbol: str, since_ms: int) -> list[list[float]]:
        """One fetch_ohlcv call with exponential backoff on transient ccxt errors."""
        import ccxt  # lazy

        retryable = (ccxt.NetworkError, ccxt.RateLimitExceeded)
        exchange = self._get_exchange()
        for attempt in range(self._max_attempts):
            try:
                return list(exchange.fetch_ohlcv(symbol, "1d", since_ms, PAGE_CANDLES))
            except retryable as exc:
                if attempt == self._max_attempts - 1:
                    raise ProviderError(
                        f"{self.name}: {symbol} failed after {self._max_attempts} attempts: {exc}"
                    ) from exc
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
        if timeframe is not Timeframe.D1:
            raise ProviderError(f"{self.name}: unsupported timeframe {timeframe}")
        if not self.supports(symbol):
            raise ProviderError(f"{self.name}: unsupported symbol {symbol}")

        start_ms = _date_ms(start)
        end_ms = _date_ms(end)
        rows: dict[int, list[float]] = {}
        since = start_ms
        first_page = True
        while since <= end_ms:
            if not first_page:
                self._sleep(self._page_interval_s)
            first_page = False
            page = self._fetch_page(str(symbol), since)
            for candle in page:
                rows[int(candle[0])] = [float(x) for x in candle[1:6]]
            if page:
                last_ts = max(int(c[0]) for c in page)
                nxt = last_ts + _DAY_MS
            else:
                nxt = since + PAGE_CANDLES * _DAY_MS  # pre-listing gap: skip a window
            since = nxt if nxt > since else since + _DAY_MS

        return self._to_frame(rows, start, end)

    def _to_frame(self, rows: dict[int, list[float]], start: date, end: date) -> pd.DataFrame:
        today = self._clock.now().astimezone(UTC).date()
        for ts in rows:
            if ts % _DAY_MS != 0:
                raise ProviderError(f"{self.name}: daily bucket {ts} not aligned to 00:00 UTC")
        keep = {
            ts: v
            for ts, v in rows.items()
            if start <= _ms_date(ts) <= min(end, today - timedelta(1))
        }
        index = pd.DatetimeIndex(pd.to_datetime(sorted(keep), unit="ms", utc=True), name="ts")
        frame = pd.DataFrame(
            [keep[ts] for ts in sorted(keep)], index=index, columns=list(BAR_COLUMNS), dtype=float
        )
        return frame


def _date_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def _ms_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date()
