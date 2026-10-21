"""Offline unit tests for the ccxt crypto adapter (SPEC-001 T6, mocked exchange)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import ccxt
import pandas as pd
import pytest

from tradebot.clock import FixedClock
from tradebot.data.errors import ProviderError
from tradebot.data.providers.ccxt_provider import CcxtProvider
from tradebot.data.types import Symbol

BTC = Symbol("BTC/USD")
DAY_MS = 86_400_000
PAGE = 300
# 2020-01-01T00:00:00Z = 1577836800 s (hand-derived epoch constant).
D2020_01_01_MS = 1_577_836_800_000
# 2019-01-01T00:00:00Z = 1546300800 s.
D2019_01_01_MS = 1_546_300_800_000


def _ms(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)


def _candle(
    ts: int, o: float = 10.0, h: float = 12.0, lo: float = 9.0, c: float = 11.0, v: float = 1000.0
) -> list[float]:
    return [float(ts), o, h, lo, c, v]


class ServedExchange:
    """Fake exchange that serves candles with ts >= since, capped at limit, and records calls."""

    def __init__(self, candles: list[list[float]]) -> None:
        self._candles = sorted(candles, key=lambda c: c[0])
        self.calls: list[tuple[str, str, int, int]] = []

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int, limit: int) -> list[Any]:
        self.calls.append((symbol, timeframe, since, limit))
        return [c for c in self._candles if c[0] >= since][:limit]


class ScriptedExchange:
    """Fake exchange that returns or raises the next scripted item on each call."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[int] = []

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int, limit: int) -> list[Any]:
        self.calls.append(since)
        if not self._script:
            raise AssertionError("unexpected fetch_ohlcv call")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _provider(
    exchange: Any, sleeps: list[float], clock: FixedClock | None = None, **kwargs: Any
) -> CcxtProvider:
    return CcxtProvider(
        clock=clock or FixedClock(datetime(2024, 12, 31, 23, 0, tzinfo=UTC)),
        sleep=sleeps.append,
        exchange=exchange,
        **kwargs,
    )


@pytest.mark.unit
def test_pagination_across_more_than_300_candles_advances_since_by_last_candle():
    # 400 daily candles from 2020-01-01 (day 0) to 2021-02-03 (day 399).
    # Page 1: since=day0, limit=300 -> days 0..299. nxt = day299 + 1 day = day300.
    # Page 2: since=day300 -> days 300..399 (100 rows). nxt = day400 > end (day399): stop.
    # Two calls, one 0.5 s page interval between them, 400 rows in total.
    candles = [_candle(D2020_01_01_MS + i * DAY_MS, c=float(i)) for i in range(400)]
    exchange = ServedExchange(candles)
    sleeps: list[float] = []
    frame = _provider(exchange, sleeps).fetch_bars(BTC, date(2020, 1, 1), date(2021, 2, 3))

    assert exchange.calls == [
        ("BTC/USD", "1d", D2020_01_01_MS, PAGE),
        ("BTC/USD", "1d", D2020_01_01_MS + 300 * DAY_MS, PAGE),
    ]
    assert sleeps == [0.5]
    assert len(frame) == 400
    assert frame.index[0] == pd.Timestamp("2020-01-01", tz="UTC")
    assert frame.index[-1] == pd.Timestamp("2021-02-03", tz="UTC")
    assert frame["close"].iloc[300] == 300.0


@pytest.mark.unit
def test_empty_pre_listing_pages_advance_since_by_300_days():
    # Start 2019-01-01 (day 0). Listing starts at day 600 (2020-08-23).
    # Pages at day0 and day300 are empty, so since advances by 300 days each time.
    # Page at day600 returns days 600..620 (21 rows); end is day620, so the loop stops.
    listed = [_candle(D2019_01_01_MS + i * DAY_MS) for i in range(600, 621)]
    exchange = ScriptedExchange([[], [], listed])
    sleeps: list[float] = []
    start = date(2019, 1, 1)
    end = date(2020, 9, 12)  # day 620
    frame = _provider(exchange, sleeps).fetch_bars(BTC, start, end)

    assert exchange.calls == [
        D2019_01_01_MS,
        D2019_01_01_MS + 300 * DAY_MS,
        D2019_01_01_MS + 600 * DAY_MS,
    ]
    assert sleeps == [0.5, 0.5]
    assert len(frame) == 21
    assert frame.index[0] == pd.Timestamp("2020-08-23", tz="UTC")


@pytest.mark.unit
def test_duplicate_timestamps_within_and_across_pages_give_one_row_each():
    # Page 1: day0, day1, day1' (duplicate, different close), day2 -> nxt = day3.
    # Page 2 (since=day3): day2' (duplicate of day2 across pages), day3 -> nxt = day4 > end.
    # Four unique days. The dict keeps the last occurrence, so day1 has close 21 and day2 close 32.
    d = D2020_01_01_MS
    script = [
        [
            _candle(d + 0 * DAY_MS, c=10.0),
            _candle(d + 1 * DAY_MS, c=11.0),
            _candle(d + 1 * DAY_MS, c=21.0),
            _candle(d + 2 * DAY_MS, c=12.0),
        ],
        [_candle(d + 2 * DAY_MS, c=32.0), _candle(d + 3 * DAY_MS, c=13.0)],
    ]
    exchange = ScriptedExchange(script)
    sleeps: list[float] = []
    frame = _provider(exchange, sleeps).fetch_bars(BTC, date(2020, 1, 1), date(2020, 1, 4))

    assert exchange.calls == [d, d + 3 * DAY_MS]
    assert sleeps == [0.5]
    assert len(frame) == 4
    assert frame.index.is_unique
    assert list(frame["close"]) == [10.0, 21.0, 32.0, 13.0]


@pytest.mark.unit
def test_unclosed_bar_for_today_utc_is_dropped_with_fixed_clock(fixed_clock):
    # Clock is 2024-12-31 23:00 UTC, so today (UTC) is 2024-12-31 and its bar is unclosed.
    # Kept: 2024-12-29 and 2024-12-30. Dropped: 2024-12-31 (today) and 2025-01-01 (future).
    candles = [
        _candle(_ms(2024, 12, 29), c=1.0),
        _candle(_ms(2024, 12, 30), c=2.0),
        _candle(_ms(2024, 12, 31), c=3.0),
        _candle(_ms(2025, 1, 1), c=4.0),
    ]
    frame = _provider(ServedExchange(candles), [], clock=fixed_clock).fetch_bars(
        BTC, date(2024, 12, 29), date(2025, 1, 1)
    )
    assert list(frame.index) == [
        pd.Timestamp("2024-12-29", tz="UTC"),
        pd.Timestamp("2024-12-30", tz="UTC"),
    ]
    assert list(frame["close"]) == [1.0, 2.0]


@pytest.mark.unit
def test_bar_for_previous_utc_day_is_kept_just_after_midnight():
    # At 2025-01-01 00:00 UTC the 2024-12-31 bar has closed, so it is kept.
    candles = [_candle(_ms(2024, 12, 31), c=3.0), _candle(_ms(2025, 1, 1), c=4.0)]
    clock = FixedClock(datetime(2025, 1, 1, 0, 0, tzinfo=UTC))
    frame = _provider(ServedExchange(candles), [], clock=clock).fetch_bars(
        BTC, date(2024, 12, 31), date(2025, 1, 1)
    )
    assert list(frame.index) == [pd.Timestamp("2024-12-31", tz="UTC")]


@pytest.mark.unit
def test_non_midnight_timestamp_raises_provider_error():
    # 2020-01-01T01:00Z is not a 00:00 UTC bucket. One call, then the frame build rejects it.
    exchange = ScriptedExchange([[_candle(D2020_01_01_MS + 3_600_000)]])
    provider = _provider(exchange, [])
    with pytest.raises(ProviderError, match="not aligned to 00:00 UTC"):
        provider.fetch_bars(BTC, date(2020, 1, 1), date(2020, 1, 1))


@pytest.mark.unit
def test_network_and_rate_limit_errors_back_off_with_exact_sleeps():
    # Attempts: NetworkError (sleep 1*2**0 = 1.0), NetworkError (sleep 2.0),
    # RateLimitExceeded (sleep 4.0), then success. No page-interval sleep on the first page.
    page = [_candle(D2020_01_01_MS)]
    exchange = ScriptedExchange(
        [
            ccxt.NetworkError("reset"),
            ccxt.NetworkError("reset again"),
            ccxt.RateLimitExceeded("slow down"),
            page,
        ]
    )
    sleeps: list[float] = []
    frame = _provider(exchange, sleeps).fetch_bars(BTC, date(2020, 1, 1), date(2020, 1, 1))

    assert len(exchange.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert len(frame) == 1


@pytest.mark.unit
def test_persistent_network_error_raises_provider_error_after_five_attempts():
    # Five attempts in total, so four sleeps (1, 2, 4, 8) and no sleep after the last attempt.
    exchange = ScriptedExchange([ccxt.NetworkError("down")] * 5)
    sleeps: list[float] = []
    with pytest.raises(ProviderError, match="failed after 5 attempts"):
        _provider(exchange, sleeps).fetch_bars(BTC, date(2020, 1, 1), date(2020, 1, 1))

    assert len(exchange.calls) == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.unit
def test_persistent_rate_limit_raises_provider_error_after_five_attempts():
    exchange = ScriptedExchange([ccxt.RateLimitExceeded("429")] * 5)
    sleeps: list[float] = []
    with pytest.raises(ProviderError, match="failed after 5 attempts"):
        _provider(exchange, sleeps).fetch_bars(BTC, date(2020, 1, 1), date(2020, 1, 1))

    assert len(exchange.calls) == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.unit
def test_non_retryable_ccxt_error_propagates_without_sleep():
    # BadSymbol is not a NetworkError or RateLimitExceeded, so there is no retry.
    exchange = ScriptedExchange([ccxt.BadSymbol("no such market")])
    sleeps: list[float] = []
    with pytest.raises(ccxt.BadSymbol):
        _provider(exchange, sleeps).fetch_bars(BTC, date(2020, 1, 1), date(2020, 1, 1))

    assert len(exchange.calls) == 1
    assert sleeps == []


@pytest.mark.unit
def test_non_daily_timeframe_raises_before_any_fetch():
    exchange = ServedExchange([_candle(D2020_01_01_MS)])
    provider = _provider(exchange, [])
    with pytest.raises(ProviderError, match="unsupported timeframe"):
        provider.fetch_bars(BTC, date(2020, 1, 1), date(2020, 1, 1), timeframe="1h")  # type: ignore[arg-type]
    assert exchange.calls == []


@pytest.mark.unit
def test_output_columns_dtypes_and_utc_index_with_ohlcv_mapping():
    # Candle [ts, open, high, low, close, volume] maps to the canonical columns in that order.
    candles = [
        _candle(D2020_01_01_MS + DAY_MS, o=20.0, h=25.0, lo=18.0, c=22.0, v=5.0),
        _candle(D2020_01_01_MS, o=10.0, h=12.0, lo=9.0, c=11.0, v=1000.0),
    ]
    frame = _provider(ServedExchange(candles), []).fetch_bars(
        BTC, date(2020, 1, 1), date(2020, 1, 2)
    )

    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert all(str(dtype) == "float64" for dtype in frame.dtypes)
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert str(frame.index.tz) == "UTC"
    assert frame.index.name == "ts"
    assert frame.index.is_monotonic_increasing
    assert list(frame.iloc[0]) == [10.0, 12.0, 9.0, 11.0, 1000.0]
    assert list(frame.iloc[1]) == [20.0, 25.0, 18.0, 22.0, 5.0]
    assert frame.index[0] == pd.Timestamp("2020-01-01T00:00:00", tz="UTC")
