"""Offline unit tests for the yfinance equity adapter (SPEC-001 T7, mocked download)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest

from tradebot.clock import FixedClock
from tradebot.data.errors import ProviderError
from tradebot.data.providers.yfinance_provider import YFinanceProvider
from tradebot.data.types import Symbol

SPY = Symbol("SPY")
QQQ = Symbol("QQQ")
BTC = Symbol("BTC/USD")
_FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
_NY = "America/New_York"


def _raw(index: pd.DatetimeIndex, rows: list[list[float]], ticker: str = "SPY") -> pd.DataFrame:
    """yfinance-shaped frame: (ticker, field) MultiIndex columns, one row per bar."""
    columns = pd.MultiIndex.from_product([[ticker], _FIELDS])
    return pd.DataFrame(rows, index=index, columns=columns, dtype=float)


def _raw_multi(index: pd.DatetimeIndex, per_ticker: dict[str, list[list[float]]]) -> pd.DataFrame:
    tickers = list(per_ticker)
    columns = pd.MultiIndex.from_tuples([(t, f) for t in tickers for f in _FIELDS])
    data = [sum((per_ticker[t][i] for t in tickers), []) for i in range(len(index))]
    return pd.DataFrame(data, index=index, columns=columns, dtype=float)


def _ny_index(*dates: str) -> pd.DatetimeIndex:
    """Exchange-local midnight stamps, as yfinance returns for daily bars."""
    return pd.DatetimeIndex(list(dates), tz=_NY)


class FakeDownload:
    """Fake yf.download: returns or raises the next scripted item and records each call."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, tickers: list[str], **kwargs: Any) -> pd.DataFrame:
        self.calls.append((list(tickers), kwargs))
        if not self._script:
            raise AssertionError("unexpected yf.download call")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _provider(download: FakeDownload, sleeps: list[float], now: datetime) -> YFinanceProvider:
    return YFinanceProvider(
        clock=FixedClock(now),
        sleep=sleeps.append,
        download=download,
    )


LATE = datetime(2024, 7, 20, 12, 0, tzinfo=UTC)


@pytest.mark.unit
def test_split_like_factor_applies_to_ohlc_and_leaves_volume_unchanged():
    # Adj Close / Close = 25 / 50 = 0.5 (a 2-for-1 split). Expected:
    # open 52*0.5=26, high 54*0.5=27, low 49*0.5=24.5, close 50*0.5=25, volume 2000 as reported.
    raw = _raw(_ny_index("2024-07-12"), [[52.0, 54.0, 49.0, 50.0, 25.0, 2000.0]])
    frame = _provider(FakeDownload([raw]), [], LATE).fetch_bars(
        SPY, date(2024, 7, 12), date(2024, 7, 12)
    )

    assert frame.iloc[0].to_list() == pytest.approx([26.0, 27.0, 24.5, 25.0, 2000.0])


@pytest.mark.unit
def test_dividend_like_factor_applies_to_ohlc_and_leaves_volume_unchanged():
    # Adj Close / Close = 98 / 100 = 0.98 (a dividend). Expected:
    # open 101*0.98=98.98, high 102*0.98=99.96, low 99*0.98=97.02, close 100*0.98=98.0,
    # volume 1_000_000 as reported.
    raw = _raw(_ny_index("2024-07-12"), [[101.0, 102.0, 99.0, 100.0, 98.0, 1_000_000.0]])
    frame = _provider(FakeDownload([raw]), [], LATE).fetch_bars(
        SPY, date(2024, 7, 12), date(2024, 7, 12)
    )

    assert frame.iloc[0].to_list() == pytest.approx([98.98, 99.96, 97.02, 98.0, 1_000_000.0])


@pytest.mark.unit
@pytest.mark.parametrize("index_tz", [_NY, None], ids=["exchange-local", "naive"])
def test_session_labels_are_utc_midnight(index_tz):
    # Bars stamped at 00:00 exchange-local (or naive) become 00:00 UTC on the same calendar date.
    index = pd.DatetimeIndex(["2024-07-10", "2024-07-11"], tz=index_tz)
    raw = _raw(index, [[10.0, 11.0, 9.0, 10.5, 10.5, 100.0], [10.5, 12.0, 10.0, 11.0, 11.0, 200.0]])
    frame = _provider(FakeDownload([raw]), [], LATE).fetch_bars(
        SPY, date(2024, 7, 10), date(2024, 7, 11)
    )

    assert list(frame.index) == [
        pd.Timestamp("2024-07-10T00:00:00", tz="UTC"),
        pd.Timestamp("2024-07-11T00:00:00", tz="UTC"),
    ]
    assert str(frame.index.tz) == "UTC"
    assert frame.index.name == "ts"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("now", "expected_labels"),
    [
        # Mon 2024-07-15 closes 16:00 EDT = 20:00 UTC; +60 min = 21:00 UTC.
        (datetime(2024, 7, 15, 20, 59, tzinfo=UTC), ["2024-07-12"]),  # in progress: dropped
        (datetime(2024, 7, 15, 21, 1, tzinfo=UTC), ["2024-07-12", "2024-07-15"]),  # kept
    ],
    ids=["before-close-plus-60", "after-close-plus-60"],
)
def test_in_progress_session_dropped_before_close_plus_60_and_kept_after(now, expected_labels):
    raw = _raw(
        _ny_index("2024-07-12", "2024-07-15"),
        [[10.0, 11.0, 9.0, 10.5, 10.5, 100.0], [10.5, 12.0, 10.0, 11.0, 11.0, 200.0]],
    )
    frame = _provider(FakeDownload([raw]), [], now).fetch_bars(
        SPY, date(2024, 7, 12), date(2024, 7, 15)
    )

    assert [ts.date().isoformat() for ts in frame.index] == expected_labels


@pytest.mark.unit
def test_exception_then_empty_frame_then_success_retries_with_exact_sleeps():
    # Attempt 1 raises (sleep 1*2**0 = 1.0), attempt 2 is empty (sleep 1*2**1 = 2.0),
    # attempt 3 succeeds.
    raw = _raw(_ny_index("2024-07-12"), [[52.0, 54.0, 49.0, 50.0, 25.0, 2000.0]])
    download = FakeDownload([RuntimeError("yahoo down"), pd.DataFrame(), raw])
    sleeps: list[float] = []
    frame = _provider(download, sleeps, LATE).fetch_bars(SPY, date(2024, 7, 12), date(2024, 7, 12))

    assert len(download.calls) == 3
    assert sleeps == [1.0, 2.0]
    assert len(frame) == 1


@pytest.mark.unit
def test_persistent_exceptions_raise_provider_error_after_five_attempts():
    # Five attempts, four sleeps (1, 2, 4, 8), and no sleep after the last attempt.
    download = FakeDownload([RuntimeError("boom")] * 5)
    sleeps: list[float] = []
    with pytest.raises(ProviderError, match="failed after 5 attempts"):
        _provider(download, sleeps, LATE).fetch_bars(SPY, date(2024, 7, 12), date(2024, 7, 12))

    assert len(download.calls) == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.unit
def test_persistent_empty_frames_raise_provider_error_after_five_attempts():
    download = FakeDownload([pd.DataFrame()] * 5)
    sleeps: list[float] = []
    with pytest.raises(ProviderError, match="failed after 5 attempts"):
        _provider(download, sleeps, LATE).fetch_bars(SPY, date(2024, 7, 12), date(2024, 7, 12))

    assert len(download.calls) == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.unit
def test_fetch_many_makes_exactly_one_download_call():
    index = _ny_index("2024-07-12")
    raw = _raw_multi(
        index,
        {
            "SPY": [[52.0, 54.0, 49.0, 50.0, 25.0, 2000.0]],
            "QQQ": [[400.0, 410.0, 390.0, 400.0, 400.0, 10.0]],
        },
    )
    download = FakeDownload([raw])
    result = _provider(download, [], LATE).fetch_many(
        [SPY, QQQ], date(2024, 7, 12), date(2024, 7, 12)
    )

    assert len(download.calls) == 1
    assert download.calls[0][0] == ["SPY", "QQQ"]
    assert set(result) == {SPY, QQQ}
    assert result[SPY].iloc[0].to_list() == pytest.approx([26.0, 27.0, 24.5, 25.0, 2000.0])
    assert result[QQQ].iloc[0].to_list() == pytest.approx([400.0, 410.0, 390.0, 400.0, 10.0])


@pytest.mark.unit
def test_unsupported_symbol_raises_before_any_download():
    download = FakeDownload([])
    provider = _provider(download, [], LATE)
    with pytest.raises(ProviderError, match="unsupported symbol"):
        provider.fetch_bars(BTC, date(2024, 7, 12), date(2024, 7, 12))
    with pytest.raises(ProviderError, match="unsupported symbol"):
        provider.fetch_many([SPY, BTC], date(2024, 7, 12), date(2024, 7, 12))
    assert download.calls == []


@pytest.mark.unit
def test_download_kwargs_disable_adjustment_and_repair_and_omit_period():
    raw = _raw(_ny_index("2024-07-10"), [[52.0, 54.0, 49.0, 50.0, 25.0, 2000.0]])
    download = FakeDownload([raw])
    _provider(download, [], LATE).fetch_bars(SPY, date(2024, 7, 10), date(2024, 7, 12))

    tickers, kwargs = download.calls[0]
    assert tickers == ["SPY"]
    # yfinance treats end as exclusive, so end = 2024-07-12 + 1 day.
    assert kwargs["start"] == "2024-07-10"
    assert kwargs["end"] == "2024-07-13"
    assert kwargs["auto_adjust"] is False
    assert kwargs["repair"] is False
    assert kwargs["progress"] is False
    assert kwargs["group_by"] == "ticker"
    assert "period" not in kwargs


@pytest.mark.unit
def test_range_with_no_sessions_returns_empty_without_calling_download():
    # 2024-07-04 is a holiday, 07-06/07-07 a weekend.
    download = FakeDownload([])
    provider = _provider(download, [], LATE)
    for start, end in [
        (date(2024, 7, 4), date(2024, 7, 4)),
        (date(2024, 7, 6), date(2024, 7, 7)),
    ]:
        frame = provider.fetch_bars(SPY, start, end)
        assert frame.empty
        assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
        many = provider.fetch_many([SPY, QQQ], start, end)
        assert set(many) == {SPY, QQQ}
        assert all(f.empty for f in many.values())
    assert download.calls == []


@pytest.mark.unit
def test_ticker_missing_from_batch_is_logged_and_returned_as_a_warning(caplog):
    raw = _raw(_ny_index("2024-07-12"), [[10.0, 11.0, 9.0, 10.5, 10.5, 100.0]])
    with caplog.at_level("WARNING"):
        out = _provider(FakeDownload([raw]), [], LATE).fetch_many(
            [SPY, QQQ], date(2024, 7, 12), date(2024, 7, 12)
        )
    assert len(out[SPY]) == 1
    assert out[SPY].attrs.get("provider_warnings") is None
    assert out[QQQ].empty
    assert any("QQQ" in w for w in out[QQQ].attrs["provider_warnings"])
    assert any("QQQ" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_close_buffer_is_configurable():
    # Session closes 20:00 UTC (16:00 ET). 21:00 UTC is exactly close + 60.
    raw = _raw(_ny_index("2024-07-12"), [[10.0, 11.0, 9.0, 10.5, 10.5, 100.0]])
    now = datetime(2024, 7, 12, 21, 0, tzinfo=UTC)
    default = YFinanceProvider(clock=FixedClock(now), download=FakeDownload([raw]))
    wide = YFinanceProvider(
        clock=FixedClock(now), download=FakeDownload([raw]), equity_close_buffer_min=120
    )
    d = date(2024, 7, 12)
    assert len(default.fetch_bars(SPY, d, d)) == 1
    assert wide.fetch_bars(SPY, d, d).empty
