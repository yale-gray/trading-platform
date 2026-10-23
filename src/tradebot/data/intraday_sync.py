"""15-minute bar synchronisation used by ``MarketData`` (ADR-0001 A21).

Cache layout: source key ``alpaca/15m`` (``<cache>/alpaca/15m/<SYMBOL>/gen-NNNN/...``), holding
**SIP** bars adjusted for splits and dividends as of the fetch date. IEX bars are never written
to the cache: the optional live tail is fetched on read and returned only in memory, because
IEX prices and volumes differ from the consolidated tape and splicing them into one stored
series would corrupt the overlap/restatement checks.

Dates passed around here are New York session dates (inclusive). Time comes from the
injected ``Clock`` only.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta

import pandas as pd

from tradebot.clock import Clock
from tradebot.data.cache import REAL_BASIS_PREFIX, BarCache
from tradebot.data.calendar import XNYS_START
from tradebot.data.config import DataConfig
from tradebot.data.errors import (
    CacheCorruptError,
    DataError,
    DataRevisionError,
    RevisionQuarantinedError,
)
from tradebot.data.intraday import BAR_DELTA, complete_mask, last_complete_bar_open, session_date
from tradebot.data.provider import DataProvider
from tradebot.data.quality import QualityReport, validate_intraday_bars
from tradebot.data.restatement import reconcile_overlap
from tradebot.data.symbols import asset_class
from tradebot.data.types import BAR_COLUMNS, AssetClass, Symbol, Timeframe

log = logging.getLogger(__name__)

SOURCE_15M = "alpaca/15m"
DEFAULT_INTRADAY_START = date(2016, 1, 1)
STALE_TOLERANCE_BARS = 4  # thin names may have no trades in the last hour


def empty_bars() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS}, index=index)


def _ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text)  # gen.json stores "%Y-%m-%dT%H:%M:%SZ"


class IntradaySync:
    """Plans, fetches, validates and caches 15m bars; reads them back with a live tail."""

    def __init__(
        self,
        cache: BarCache,
        config: DataConfig,
        clock: Clock,
        history: DataProvider | None = None,
        live: DataProvider | None = None,
    ) -> None:
        self._cache = cache
        self._config = config
        self._clock = clock
        self._history = history
        self._live = live
        self._live_built = live is not None

    # ---- providers ------------------------------------------------------
    def history(self) -> DataProvider:
        if self._history is None:
            from tradebot.data.providers.alpaca_provider import AlpacaProvider

            self._history = AlpacaProvider(
                feed=self._config.intraday_history_feed,
                clock=self._clock,
                sip_delay_min=self._config.sip_delay_min,
            )
        return self._history

    def live(self) -> DataProvider | None:
        if not self._live_built:
            self._live_built = True
            if self._config.intraday_live_feed:
                from tradebot.data.providers.alpaca_provider import AlpacaProvider

                self._live = AlpacaProvider(feed=self._config.intraday_live_feed, clock=self._clock)
        return self._live

    # ---- time -----------------------------------------------------------
    def _lag(self) -> timedelta:
        sip = self._config.intraday_history_feed == "sip"
        return timedelta(minutes=self._config.sip_delay_min if sip else 0)

    def target_open(self) -> pd.Timestamp | None:
        """Open of the last bar the history feed can be expected to have."""
        return last_complete_bar_open(self._clock.now() - self._lag())

    # ---- planning -------------------------------------------------------
    def _plan(self, symbol: Symbol, req_start: date | None) -> tuple[date, date] | None:
        target = self.target_open()
        if target is None:
            return None
        target_d = session_date(target)
        if not self._cache.exists(SOURCE_15M, symbol):
            start = max(req_start or DEFAULT_INTRADAY_START, XNYS_START.date())
            return (start, target_d) if start <= target_d else None
        last = _ts(self._cache.gen_info(SOURCE_15M, symbol)["last"])
        if last >= target:
            return None
        tail = self._cache.read(SOURCE_15M, symbol).tail(self._config.intraday_overlap_bars)
        return (session_date(tail.index[0]), target_d)

    # ---- refresh --------------------------------------------------------
    def refresh(
        self, symbols: Sequence[Symbol], req_start: date | None
    ) -> tuple[dict[Symbol, QualityReport], dict[Symbol, DataError]]:
        """Refresh each symbol's tail (and head if ``req_start`` precedes the cache).

        Errors are isolated per symbol. Symbols are fetched ``intraday_chunk_symbols`` at a
        time (multi-symbol requests) so memory stays bounded for the full S&P 500.
        """
        reports: dict[Symbol, QualityReport] = {}
        errors: dict[Symbol, DataError] = {}

        def fail(symbol: Symbol, exc: DataError) -> None:
            log.error("%s: 15m refresh failed: %s", symbol, exc)
            errors[symbol] = exc
            n = self._cached_len(symbol)
            if isinstance(exc, RevisionQuarantinedError):
                status = "revision_quarantined"
            elif isinstance(exc, CacheCorruptError):
                status = "corrupt"
            else:
                status = "error"
            reports[symbol] = QualityReport(
                symbol=symbol, n_bars=n, warnings=[f"{status}: {exc}"], status=status
            )

        plans: dict[Symbol, tuple[date, date]] = {}
        for symbol in symbols:
            try:
                if asset_class(symbol) is not AssetClass.EQUITY:
                    raise DataError(f"{symbol}: 15m bars are equities only")
                plan = self._plan(symbol, req_start)
                if plan is not None:
                    plans[symbol] = plan
            except DataError as exc:
                fail(symbol, exc)

        provider = self.history()
        ordered = sorted(plans, key=lambda s: plans[s])
        size = max(1, self._config.intraday_chunk_symbols)
        for i in range(0, len(ordered), size):
            group = ordered[i : i + size]
            lo = min(plans[s][0] for s in group)
            hi = max(plans[s][1] for s in group)
            try:
                fetched = provider.fetch_many(group, lo, hi, Timeframe.M15)  # type: ignore[attr-defined]
            except DataError as exc:
                for symbol in group:
                    fail(symbol, exc)
                continue
            for symbol in group:
                a, b = plans[symbol]
                try:
                    reports[symbol] = self._ingest(symbol, provider, _slice(fetched[symbol], a, b))
                except DataRevisionError as exc:
                    fail(symbol, exc)
                except DataError as exc:
                    fail(symbol, exc)

        if req_start is not None:
            for symbol in symbols:
                if symbol in errors:
                    continue
                try:
                    self._sync_head(symbol, req_start, provider)
                except DataError as exc:
                    fail(symbol, exc)
        return reports, errors

    def _cached_len(self, symbol: Symbol) -> int:
        try:
            if self._cache.exists(SOURCE_15M, symbol):
                return len(self._cache.read(SOURCE_15M, symbol))
        except DataError:
            pass
        return 0

    def _ingest(self, symbol: Symbol, provider: DataProvider, frame: pd.DataFrame) -> QualityReport:
        warnings = list(frame.attrs.get("provider_warnings", []))
        raw_empty = frame.empty
        clean, report = validate_intraday_bars(frame, symbol, self._clock.now())
        report.warnings.extend(warnings)
        if raw_empty:
            report.warnings.append("provider returned no rows")
        if clean.empty:
            return report
        if not self._cache.exists(SOURCE_15M, symbol):
            basis = f"{REAL_BASIS_PREFIX}{self._clock.now().date().isoformat()}"
            self._cache.create(SOURCE_15M, symbol, clean, basis, provider.name)
        else:
            self._cache.apply_fetch(
                SOURCE_15M,
                symbol,
                clean,
                provider.name,
                overlap_bars=self._config.intraday_overlap_bars,
            )
        return report

    def _sync_head(self, symbol: Symbol, req_start: date, provider: DataProvider) -> None:
        """Prepend history older than the first cached bar (never touches cached rows)."""
        if not self._cache.exists(SOURCE_15M, symbol):
            return
        start = max(req_start, XNYS_START.date())
        first = _ts(self._cache.gen_info(SOURCE_15M, symbol)["first"])
        if start >= session_date(first):
            return
        head = self._cache.read(SOURCE_15M, symbol).head(self._config.intraday_overlap_bars)
        raw = provider.fetch_bars(symbol, start, session_date(head.index[-1]), Timeframe.M15)
        clean, _ = validate_intraday_bars(raw, symbol, self._clock.now())
        if len(head.index.intersection(clean.index)) == 0:
            log.warning("%s: 15m head fetch has no overlap with cache; not prepending", symbol)
            return
        try:
            scale = reconcile_overlap(head, clean).ratio
        except DataRevisionError:
            self._cache.quarantine(SOURCE_15M, symbol, clean)
            raise
        rows = clean.loc[clean.index < head.index[0]].copy()
        if rows.empty:
            return
        if scale != 1.0:
            for col in ("open", "high", "low", "close"):
                rows[col] = rows[col] * scale
        self._cache.append(SOURCE_15M, symbol, rows, provider.name)

    # ---- reads ----------------------------------------------------------
    def read(self, symbol: Symbol, start: date | None, end: date | None) -> pd.DataFrame:
        """Cached complete bars with New York session date in ``[start, end]``."""
        if not self._cache.exists(SOURCE_15M, symbol):
            return empty_bars()
        lo = pd.Timestamp(start, tz="America/New_York").tz_convert("UTC") if start else None
        frame = self._cache.read(SOURCE_15M, symbol, lo, None)
        if end is not None:
            hi = pd.Timestamp(end + timedelta(days=1), tz="America/New_York").tz_convert("UTC")
            frame = frame.loc[frame.index < hi]
        frame = frame.loc[complete_mask(frame.index, self._clock.now())]
        return frame if not frame.empty else empty_bars()

    def with_live_tail(
        self, symbol: Symbol, cached: pd.DataFrame, start: date, end: date
    ) -> pd.DataFrame:
        """Append not-yet-cached bars from the live (IEX) feed, in memory only."""
        live = self.live()
        if live is None or cached.empty or not self._cache.exists(SOURCE_15M, symbol):
            return cached
        last_cached = _ts(self._cache.gen_info(SOURCE_15M, symbol)["last"])
        newest = last_complete_bar_open(self._clock.now())
        if newest is None or last_cached >= newest or cached.index[-1] < last_cached:
            return cached  # up to date, or the caller asked for an older window
        try:
            raw = live.fetch_bars(
                symbol,
                max(start, session_date(last_cached)),
                min(end, session_date(newest)),
                Timeframe.M15,
            )
            clean, _ = validate_intraday_bars(raw, symbol, self._clock.now())
        except DataError as exc:
            log.warning("%s: live 15m tail unavailable: %s", symbol, exc)
            cached.attrs["live_tail_error"] = str(exc)
            return cached
        fresh = clean.loc[clean.index > last_cached]
        if fresh.empty:
            return cached
        out = pd.concat([cached, fresh])
        out.index.name = "ts"
        out.attrs["live_tail_rows"] = len(fresh)
        return out

    def check_fresh(self, symbol: Symbol, frame: pd.DataFrame, *, live: bool) -> None:
        """Raise ``StaleDataError`` if the last bar is more than 4 bars behind expectation."""
        from tradebot.data.errors import StaleDataError

        expected = last_complete_bar_open(self._clock.now()) if live else self.target_open()
        if expected is None:
            return
        if frame.empty:
            raise StaleDataError(f"{symbol}: no 15m bars; expected last bar open {expected}")
        if frame.index[-1] < expected - STALE_TOLERANCE_BARS * BAR_DELTA:
            raise StaleDataError(
                f"{symbol}: last 15m bar {frame.index[-1]} is behind expected {expected}"
            )

    def cache_report(self, symbol: Symbol) -> QualityReport:
        """Validate what is cached for ``symbol`` (no provider calls)."""
        from tradebot.data.errors import DataQualityError

        try:
            if not self._cache.exists(SOURCE_15M, symbol):
                return QualityReport(symbol=symbol, n_bars=0, warnings=["no cached data"])
            frame = self._cache.read(SOURCE_15M, symbol)
        except DataError as exc:
            status = "corrupt" if isinstance(exc, CacheCorruptError) else "error"
            return QualityReport(
                symbol=symbol, n_bars=0, warnings=[f"{status}: {exc}"], status=status
            )
        try:
            return validate_intraday_bars(frame, symbol, self._clock.now())[1]
        except DataQualityError as exc:
            return QualityReport(
                symbol=symbol,
                n_bars=len(frame),
                warnings=[f"quality error: {exc}"],
                status="quality_error",
            )


def _slice(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Keep rows whose New York session date is in ``[start, end]``."""
    if frame.empty:
        return frame
    lo = pd.Timestamp(start, tz="America/New_York").tz_convert("UTC")
    hi = pd.Timestamp(end + timedelta(days=1), tz="America/New_York").tz_convert("UTC")
    out = frame.loc[(frame.index >= lo) & (frame.index < hi)]
    out.attrs.update(frame.attrs)
    return out
