"""US equity 15-minute and daily bars from the Alpaca **market data** API (ADR-0001 A21, A32).

Data only: the sole host is ``https://data.alpaca.markets`` and the sole endpoint is
``/v2/stocks/bars``. There is no trading, account or order code anywhere in this module.

* Multi-symbol requests, paginated with ``next_page_token``, ``adjustment=all`` (splits and
  dividends) and ``sort=asc``.
* ``feed="sip"`` is the full consolidated tape (history back to 2016); ``feed="iex"`` is the
  single-exchange real-time feed. Volumes and prices differ slightly between the two, so a
  caller must not splice them into one cached series.
* Symbols: Alpaca spells share classes with a dot, exactly like our canonical form
  (``BRK.B``, ``BF.B``), so no translation is needed (unlike Yahoo's ``BRK-B``).
* Daily bars (``Timeframe.D1``, A32) are a fetch source for delisted / removed S&P 500 names that
  yfinance no longer serves. They use the same ``adjustment=all`` (splits + dividends, i.e. total
  return, like the yfinance adjusted basis) and are labelled with the New York session date at
  00:00 UTC, like the yfinance daily frames. ``MarketData`` does not route to this provider; the
  ``tradebot.data.pit_prices`` helper writes its frames into the normal cache.
* Output (15m): the canonical frame (``open, high, low, close, volume`` as float64, UTC index named
  ``ts`` at bar **open**), regular session only (extended-hours bars are dropped), and only
  bars complete at the injected ``Clock``'s now (``open + 15 min <= now``).
* Rate limit: a token bucket (default 180 requests/min, under Alpaca's 200). HTTP 429 and
  5xx (and transport errors) are retried with exponential backoff, honouring ``Retry-After``.
* Secrets: credentials live in :class:`AlpacaCredentials` (repr hidden), are sent only as
  headers, and every message that leaves this module is passed through a redactor.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from urllib.parse import urlsplit

import pandas as pd

from tradebot.clock import Clock, SystemClock
from tradebot.data.calendar import last_closed_bar_date
from tradebot.data.errors import ProviderError
from tradebot.data.intraday import bar_opens, complete_mask, session_close_time
from tradebot.data.symbols import asset_class
from tradebot.data.types import BAR_COLUMNS, AssetClass, Symbol, Timeframe

log = logging.getLogger(__name__)

DATA_HOST = "data.alpaca.markets"
BARS_URL = f"https://{DATA_HOST}/v2/stocks/bars"
_TIMEFRAMES = {"15m": "15Min", "1d": "1Day"}
_MAX_BODY_CHARS = 200
_FORBIDDEN_HOST_PARTS = ("paper-api", "api.alpaca.markets", "broker", "trading")


class _Response(Protocol):
    status_code: int
    headers: Any

    def json(self) -> Any: ...


class HttpClient(Protocol):
    """The slice of ``httpx.Client`` / ``requests.Session`` this provider uses."""

    def get(
        self, url: str, *, params: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> _Response: ...


@dataclass(frozen=True)
class AlpacaCredentials:
    """API key pair. ``repr`` is hidden so the values cannot leak through logging."""

    key_id: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "AlpacaCredentials(<redacted>)"

    __str__ = __repr__


def credentials_from_env() -> AlpacaCredentials:
    """Credentials from ``Settings`` (``.env``) or ``os.environ``; never echoes the values."""
    key = secret = None
    try:
        from tradebot.config import Settings

        settings = Settings()
        key = settings.alpaca_key_id.get_secret_value() if settings.alpaca_key_id else None
        secret = (
            settings.alpaca_secret_key.get_secret_value() if settings.alpaca_secret_key else None
        )
    except Exception:  # a broken .env must not leak its content into the message
        pass
    key = key or os.environ.get("APCA_API_KEY_ID")
    secret = secret or os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise ProviderError("alpaca: APCA_API_KEY_ID / APCA_API_SECRET_KEY are not set in .env")
    return AlpacaCredentials(key, secret)


class TokenBucket:
    """Blocking token bucket. ``monotonic`` and ``sleep`` are injectable for tests."""

    def __init__(
        self,
        rate_per_s: float,
        capacity: int = 10,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = rate_per_s
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()

    def acquire(self) -> None:
        while True:
            now = self._monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            self._sleep((1.0 - self._tokens) / self._rate)


def _empty_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame(columns=list(BAR_COLUMNS), index=index, dtype=float)


class AlpacaProvider:
    """15-minute equity bars from Alpaca market data. Implements ``DataProvider``."""

    def __init__(
        self,
        feed: str = "sip",
        timeframe: str = "15m",
        *,
        credentials: AlpacaCredentials | None = None,
        client: HttpClient | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        max_attempts: int = 6,
        backoff_base_s: float = 1.0,
        requests_per_min: int = 180,
        bucket_capacity: int = 10,
        page_limit: int = 10_000,
        symbols_per_request: int = 100,
        sip_delay_min: int = 16,
        timeout_s: float = 30.0,
    ) -> None:
        if feed not in ("sip", "iex"):
            raise ValueError(f"feed must be 'sip' or 'iex', got {feed!r}")
        if timeframe not in _TIMEFRAMES:
            raise ValueError(f"unsupported timeframe {timeframe!r}; supported: {list(_TIMEFRAMES)}")
        self.name = f"alpaca_{feed}"
        self.feed = feed
        self._tf_param = _TIMEFRAMES[timeframe]
        self._credentials = credentials
        self._client = client
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._bucket = TokenBucket(
            requests_per_min / 60.0, bucket_capacity, monotonic=monotonic, sleep=sleep
        )
        self._page_limit = page_limit
        self._symbols_per_request = symbols_per_request
        # The free plan refuses SIP data newer than 15 minutes; query up to now - delay.
        self._sip_delay_min = sip_delay_min if feed == "sip" else 0
        self._timeout_s = timeout_s

    def __repr__(self) -> str:
        return f"AlpacaProvider(feed={self.feed!r})"

    # ---- DataProvider ---------------------------------------------------
    def supports(self, symbol: Symbol) -> bool:
        return asset_class(symbol) is AssetClass.EQUITY

    def fetch_bars(
        self,
        symbol: Symbol,
        start: date,
        end: date,
        timeframe: Timeframe = Timeframe.M15,
    ) -> pd.DataFrame:
        """Complete regular-session bars whose New York session date is in [start, end]."""
        return self.fetch_many([symbol], start, end, timeframe)[symbol]

    def fetch_many(
        self,
        symbols: Sequence[Symbol],
        start: date,
        end: date,
        timeframe: Timeframe = Timeframe.M15,
    ) -> dict[Symbol, pd.DataFrame]:
        """Like :meth:`fetch_bars` for many symbols via multi-symbol paginated requests."""
        if timeframe not in (Timeframe.M15, Timeframe.D1):
            raise ProviderError(f"{self.name}: unsupported timeframe {timeframe}")
        unique = list(dict.fromkeys(symbols))
        for symbol in unique:
            if not self.supports(symbol):
                raise ProviderError(f"{self.name}: unsupported symbol {symbol}")
        if not unique:
            return {}
        if timeframe is Timeframe.D1:
            return self._fetch_many_daily(unique, start, end)
        expected = bar_opens(start, end)
        if len(expected) == 0:
            return {s: _empty_frame() for s in unique}

        now = pd.Timestamp(self._clock.now())
        req_start = expected[0]
        req_end = session_close_time(expected[-1].tz_convert("America/New_York").date())
        if self._sip_delay_min:
            req_end = min(req_end, now - pd.Timedelta(minutes=self._sip_delay_min))
        out: dict[Symbol, pd.DataFrame] = {}
        if req_end <= req_start:
            return {s: _empty_frame() for s in unique}

        for i in range(0, len(unique), self._symbols_per_request):
            group = unique[i : i + self._symbols_per_request]
            raw = self._fetch_group(group, req_start, req_end)
            for symbol in group:
                frame = _to_frame(raw.get(symbol), expected, self._clock.now())
                if frame.empty:
                    message = f"{self.name}: no bars returned for {symbol}"
                    log.warning(message)
                    frame.attrs["provider_warnings"] = [message]
                out[symbol] = frame
        return out

    def _fetch_many_daily(
        self, unique: list[Symbol], start: date, end: date
    ) -> dict[Symbol, pd.DataFrame]:
        """Closed daily bars, session date in [start, end], split- and dividend-adjusted."""
        if end < start:
            return {s: _empty_frame() for s in unique}
        now = pd.Timestamp(self._clock.now())
        req_start = pd.Timestamp(start, tz="UTC")
        req_end = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        if self._sip_delay_min:
            req_end = min(req_end, now - pd.Timedelta(minutes=self._sip_delay_min))
        if req_end <= req_start:
            return {s: _empty_frame() for s in unique}
        out: dict[Symbol, pd.DataFrame] = {}
        for i in range(0, len(unique), self._symbols_per_request):
            group = unique[i : i + self._symbols_per_request]
            raw = self._fetch_group(group, req_start, req_end, "1Day")
            for symbol in group:
                frame = _to_daily_frame(raw.get(symbol), symbol, start, end, self._clock.now())
                if frame.empty:
                    message = f"{self.name}: no daily bars returned for {symbol}"
                    log.warning(message)
                    frame.attrs["provider_warnings"] = [message]
                out[symbol] = frame
        return out

    # ---- HTTP -----------------------------------------------------------
    def _get_client(self) -> HttpClient:
        if self._client is None:
            import httpx  # lazy: keeps httpx out of import time and offline tests

            self._client = httpx.Client()
        return self._client

    def _get_credentials(self) -> AlpacaCredentials:
        if self._credentials is None:
            self._credentials = credentials_from_env()
        return self._credentials

    def _redact(self, text: str) -> str:
        creds = self._credentials
        if creds is not None:
            for secret in (creds.key_id, creds.secret_key):
                if secret:
                    text = text.replace(secret, "<redacted>")
        return text

    def _fetch_group(
        self,
        group: list[Symbol],
        start: pd.Timestamp,
        end: pd.Timestamp,
        tf_param: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """All pages for one multi-symbol request window -> per-symbol raw bar dicts."""
        params: dict[str, Any] = {
            "symbols": ",".join(group),
            "timeframe": tf_param or self._tf_param,
            "start": start.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
            "adjustment": "all",
            "feed": self.feed,
            "limit": self._page_limit,
            "sort": "asc",
        }
        collected: dict[str, list[dict[str, Any]]] = {}
        token: str | None = None
        while True:
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            payload = self._request(page_params)
            bars = payload.get("bars") or {}
            for sym, rows in bars.items():
                if rows:
                    collected.setdefault(sym, []).extend(rows)
            token = payload.get("next_page_token")
            if not token:
                return collected

    def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        """One GET with rate limiting and retry on 429 / 5xx / transport errors."""
        host = urlsplit(BARS_URL).hostname or ""
        if host != DATA_HOST or any(part in host for part in _FORBIDDEN_HOST_PARTS):
            raise ProviderError("alpaca: refusing to call a non-market-data host")
        creds = self._get_credentials()
        headers = {
            "APCA-API-KEY-ID": creds.key_id,
            "APCA-API-SECRET-KEY": creds.secret_key,
            "Accept": "application/json",
        }
        client = self._get_client()
        reason = "unknown"
        for attempt in range(self._max_attempts):
            self._bucket.acquire()
            retry_after = 0.0
            try:
                resp = client.get(BARS_URL, params=params, headers=headers, timeout=self._timeout_s)
            except Exception as exc:  # transport error; text may embed request details
                reason = self._redact(f"{type(exc).__name__}: {exc}")[:_MAX_BODY_CHARS]
            else:
                status = int(resp.status_code)
                if status == 200:
                    try:
                        payload = resp.json()
                    except Exception:
                        raise ProviderError("alpaca: response was not valid JSON") from None
                    if not isinstance(payload, dict):
                        raise ProviderError("alpaca: unexpected response shape")
                    return payload
                if status == 429 or status >= 500:
                    reason = f"HTTP {status}"
                    retry_after = _retry_after(resp)
                else:
                    raise ProviderError(
                        f"alpaca: HTTP {status}: {self._redact(_body_snippet(resp))}"
                    )
            if attempt == self._max_attempts - 1:
                break
            delay = max(retry_after, self._backoff_base_s * 2**attempt)
            log.warning("alpaca: %s, retrying in %.1fs", reason, delay)
            self._sleep(delay)
        raise ProviderError(f"alpaca: request failed after {self._max_attempts} attempts: {reason}")


def _retry_after(resp: _Response) -> float:
    try:
        value = resp.headers.get("Retry-After")
        return max(0.0, float(value)) if value is not None else 0.0
    except Exception:
        return 0.0


def _body_snippet(resp: _Response) -> str:
    try:
        data = resp.json()
        text = str(data.get("message", data)) if isinstance(data, dict) else str(data)
    except Exception:
        text = ""
    return text[:_MAX_BODY_CHARS]


def _to_frame(
    rows: list[dict[str, Any]] | None, expected: pd.DatetimeIndex, now: Any
) -> pd.DataFrame:
    """Canonical frame: regular-session opens only, complete at ``now``, float64."""
    if not rows:
        return _empty_frame()
    try:
        raw = pd.DataFrame(rows)
        index = pd.DatetimeIndex(pd.to_datetime(raw["t"], utc=True), name="ts")
        frame = pd.DataFrame(
            {
                "open": raw["o"].to_numpy(dtype="float64"),
                "high": raw["h"].to_numpy(dtype="float64"),
                "low": raw["l"].to_numpy(dtype="float64"),
                "close": raw["c"].to_numpy(dtype="float64"),
                "volume": raw["v"].to_numpy(dtype="float64"),
            },
            index=index,
            columns=list(BAR_COLUMNS),
        )
    except (KeyError, ValueError, TypeError):
        raise ProviderError("alpaca: bar payload missing expected fields") from None
    frame.index = frame.index.as_unit("ns")
    frame = frame[frame.index.isin(expected)]  # drops pre/post-market bars
    frame = frame[complete_mask(frame.index, now)]  # drops the in-progress bar
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame.astype(float)


def _to_daily_frame(
    rows: list[dict[str, Any]] | None, symbol: Symbol, start: date, end: date, now: Any
) -> pd.DataFrame:
    """Canonical daily frame: New York session date at 00:00 UTC, closed bars, float64."""
    if not rows:
        return _empty_frame()
    try:
        raw = pd.DataFrame(rows)
        stamp = pd.to_datetime(raw["t"], utc=True).dt.tz_convert("America/New_York")
        labels = pd.DatetimeIndex(stamp.dt.tz_localize(None).dt.normalize(), name="ts")
        frame = pd.DataFrame(
            {
                "open": raw["o"].to_numpy(dtype="float64"),
                "high": raw["h"].to_numpy(dtype="float64"),
                "low": raw["l"].to_numpy(dtype="float64"),
                "close": raw["c"].to_numpy(dtype="float64"),
                "volume": raw["v"].to_numpy(dtype="float64"),
            },
            index=labels.tz_localize("UTC"),
            columns=list(BAR_COLUMNS),
        )
    except (KeyError, ValueError, TypeError):
        raise ProviderError("alpaca: bar payload missing expected fields") from None
    frame.index = frame.index.as_unit("ns")
    upper = min(end, last_closed_bar_date(symbol, now))
    frame = frame[
        (frame.index >= pd.Timestamp(start, tz="UTC"))
        & (frame.index <= pd.Timestamp(upper, tz="UTC"))
    ]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame.astype(float)
