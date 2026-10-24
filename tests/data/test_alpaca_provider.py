"""AlpacaProvider with a mocked HTTP client (offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest

from tradebot.clock import FixedClock
from tradebot.data.errors import ProviderError
from tradebot.data.intraday import session_bar_opens
from tradebot.data.provider import DataProvider
from tradebot.data.providers.alpaca_provider import (
    AlpacaCredentials,
    AlpacaProvider,
    TokenBucket,
)
from tradebot.data.types import BAR_COLUMNS, Symbol, Timeframe

KEY = "PKTESTKEY1234567890ABCDEF"
SECRET = "sEcReT/abcdefghijklmnopqrstuvwxyz0123456789"
DAY = date(2024, 6, 12)
AFTER_CLOSE = FixedClock(datetime(2024, 6, 13, 12, 0, tzinfo=UTC))


class Resp:
    def __init__(self, status: int, payload: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: dict[str, Any], headers: dict[str, str], timeout: float):
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers)})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def bar(ts: pd.Timestamp, px: float = 100.0) -> dict[str, Any]:
    return {
        "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": px,
        "h": px + 1,
        "l": px - 1,
        "c": px + 0.5,
        "v": 1000,
        "n": 10,
        "vw": px,
    }


def make(client: FakeClient, **kw: Any) -> AlpacaProvider:
    kw.setdefault("clock", AFTER_CLOSE)
    kw.setdefault("sleep", lambda s: None)
    return AlpacaProvider(credentials=AlpacaCredentials(KEY, SECRET), client=client, **kw)  # type: ignore[arg-type]


def test_satisfies_protocol_and_supports_equities_only() -> None:
    p = make(FakeClient([]))
    assert isinstance(p, DataProvider)
    assert p.supports(Symbol("AAPL")) and p.supports(Symbol("BRK.B"))
    assert not p.supports(Symbol("BTC/USD"))


def test_canonical_frame_regular_session_only_and_complete() -> None:
    opens = session_bar_opens(DAY)
    premarket = pd.Timestamp("2024-06-12 12:00", tz="UTC")
    aftermarket = pd.Timestamp("2024-06-12 21:00", tz="UTC")
    rows = [bar(premarket)] + [bar(t, 100 + i) for i, t in enumerate(opens)] + [bar(aftermarket)]
    rows.append(rows[5])  # duplicate
    client = FakeClient([Resp(200, {"bars": {"AAPL": rows}, "next_page_token": None})])
    frame = make(client).fetch_bars(Symbol("AAPL"), DAY, DAY)
    assert list(frame.columns) == list(BAR_COLUMNS)
    assert frame.index.name == "ts" and str(frame.index.tz) == "UTC"
    assert (frame.dtypes == "float64").all()
    assert len(frame) == 26 and frame.index.is_unique and frame.index.is_monotonic_increasing
    assert frame.index[0] == opens[0]  # labelled at bar open
    params = client.calls[0]["params"]
    assert params["adjustment"] == "all" and params["feed"] == "sip"
    assert params["timeframe"] == "15Min" and params["sort"] == "asc"
    assert client.calls[0]["url"].startswith("https://data.alpaca.markets/")


def test_incomplete_bar_dropped_per_clock() -> None:
    opens = session_bar_opens(DAY)
    clock = FixedClock(datetime(2024, 6, 12, 14, 0, tzinfo=UTC))  # 10:00 ET
    rows = [bar(t) for t in opens[:4]]  # 09:30, 09:45, 10:00, 10:15
    p = make(FakeClient([Resp(200, {"bars": {"AAPL": rows}})]), clock=clock, sip_delay_min=0)
    frame = p.fetch_bars(Symbol("AAPL"), DAY, DAY)
    assert list(frame.index) == [opens[0], opens[1]]  # bar 10:00 completes at 10:15


def test_pagination_merges_pages_across_symbols() -> None:
    opens = session_bar_opens(DAY)
    first = {"bars": {"AAPL": [bar(t) for t in opens[:20]]}, "next_page_token": "tok1"}
    second = {
        "bars": {"AAPL": [bar(t) for t in opens[20:]], "MSFT": [bar(t) for t in opens]},
        "next_page_token": None,
    }
    client = FakeClient([Resp(200, first), Resp(200, second)])
    out = make(client).fetch_many([Symbol("AAPL"), Symbol("MSFT")], DAY, DAY)
    assert len(out["AAPL"]) == 26 and len(out["MSFT"]) == 26
    assert "page_token" not in client.calls[0]["params"]
    assert client.calls[1]["params"]["page_token"] == "tok1"
    assert client.calls[0]["params"]["symbols"] == "AAPL,MSFT"


def test_symbol_without_bars_is_empty_with_warning() -> None:
    client = FakeClient([Resp(200, {"bars": {}, "next_page_token": None})])
    frame = make(client).fetch_bars(Symbol("BRK.B"), DAY, DAY)
    assert frame.empty and frame.attrs["provider_warnings"]
    assert client.calls[0]["params"]["symbols"] == "BRK.B"  # Alpaca uses the dotted form


def test_429_backs_off_then_succeeds() -> None:
    sleeps: list[float] = []
    opens = session_bar_opens(DAY)
    client = FakeClient(
        [
            Resp(429, {}, {"Retry-After": "7"}),
            Resp(503, {}),
            Resp(200, {"bars": {"AAPL": [bar(opens[0])]}}),
        ]
    )
    p = make(client, sleep=sleeps.append)
    assert len(p.fetch_bars(Symbol("AAPL"), DAY, DAY)) == 1
    assert len(client.calls) == 3
    assert sleeps[0] == 7.0  # Retry-After wins over 1s backoff
    assert sleeps[1] == 2.0  # exponential: 1 * 2**1


def test_persistent_429_raises_provider_error() -> None:
    client = FakeClient([Resp(429, {})] * 3)
    p = make(client, max_attempts=3)
    with pytest.raises(ProviderError, match="after 3 attempts"):
        p.fetch_bars(Symbol("AAPL"), DAY, DAY)
    assert len(client.calls) == 3


def test_4xx_is_not_retried() -> None:
    client = FakeClient([Resp(403, {"message": "forbidden"})])
    with pytest.raises(ProviderError, match="HTTP 403"):
        make(client).fetch_bars(Symbol("AAPL"), DAY, DAY)
    assert len(client.calls) == 1


def test_keys_never_appear_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    leaky = RuntimeError(f"connect failed with headers {KEY} {SECRET}")
    client = FakeClient([leaky, leaky])
    with pytest.raises(ProviderError) as info:
        make(client, max_attempts=2).fetch_bars(Symbol("AAPL"), DAY, DAY)
    assert KEY not in str(info.value) and SECRET not in str(info.value)
    assert info.value.__cause__ is None
    # body echo on a 4xx is redacted too
    client2 = FakeClient([Resp(401, {"message": f"bad key {KEY} / {SECRET}"})])
    with pytest.raises(ProviderError) as info2:
        make(client2).fetch_bars(Symbol("AAPL"), DAY, DAY)
    assert KEY not in str(info2.value) and SECRET not in str(info2.value)
    assert KEY not in caplog.text and SECRET not in caplog.text


def test_credentials_and_provider_repr_hide_secrets() -> None:
    creds = AlpacaCredentials(KEY, SECRET)
    p = make(FakeClient([]))
    for text in (repr(creds), str(creds), repr(p), str(p)):
        assert KEY not in text and SECRET not in text


def test_missing_credentials_message_has_no_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.chdir("/")  # no .env here
    p = AlpacaProvider(client=FakeClient([]), clock=AFTER_CLOSE)  # type: ignore[arg-type]
    with pytest.raises(ProviderError, match="not set"):
        p.fetch_bars(Symbol("AAPL"), DAY, DAY)


def test_non_trading_unsupported_inputs() -> None:
    with pytest.raises(ValueError):
        AlpacaProvider(feed="otc")
    with pytest.raises(ValueError):
        AlpacaProvider(timeframe="1h")  # 1d is a supported daily source (A32)
    with pytest.raises(ProviderError):
        make(FakeClient([])).fetch_many([Symbol("BTC/USD")], DAY, DAY)
    with pytest.raises(ProviderError):
        make(FakeClient([])).fetch_many([Symbol("BTC/USD")], DAY, DAY, Timeframe.D1)
    with pytest.raises(ProviderError):
        make(FakeClient([])).fetch_many([Symbol("AAPL")], DAY, DAY, "1h")  # type: ignore[arg-type]


def test_sip_request_end_is_clamped_to_now_minus_delay() -> None:
    clock = FixedClock(datetime(2024, 6, 12, 15, 0, tzinfo=UTC))
    client = FakeClient([Resp(200, {"bars": {}})])
    make(client, clock=clock).fetch_bars(Symbol("AAPL"), DAY, DAY)
    assert client.calls[0]["params"]["end"] == "2024-06-12T14:44:00Z"


def test_holiday_range_makes_no_request() -> None:
    client = FakeClient([])
    out = make(client).fetch_many([Symbol("AAPL")], date(2024, 12, 25), date(2024, 12, 25))
    assert out[Symbol("AAPL")].empty and client.calls == []


def test_token_bucket_throttles_to_rate() -> None:
    t = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        t[0] += s

    bucket = TokenBucket(2.0, 2, monotonic=lambda: t[0], sleep=sleep)
    for _ in range(6):
        bucket.acquire()
    assert t[0] == pytest.approx(2.0)  # 2 burst tokens, then 4 more at 2/s
