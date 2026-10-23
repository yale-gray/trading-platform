"""``MarketData``: the facade the rest of the system imports (SPEC-001 Section 3).

Routing (equity -> yfinance, crypto -> ccxt unless providers are injected), cache
policy (overlap-window refresh with chain-linking, immutable history),
validation of every fetch before it can reach the cache, and staleness checks.
Time only ever comes from the injected ``Clock``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from typing import Any

import pandas as pd

from tradebot.clock import Clock, SystemClock
from tradebot.data.cache import REAL_BASIS_PREFIX, BarCache
from tradebot.data.calendar import XNYS_START, last_closed_bar_date
from tradebot.data.config import DataConfig
from tradebot.data.errors import (
    CacheCorruptError,
    DataError,
    DataQualityError,
    DataRevisionError,
    ProviderError,
    RevisionQuarantinedError,
    StaleDataError,
    UnknownSymbolError,
    quarantine_warning,
)
from tradebot.data.intraday_sync import DEFAULT_INTRADAY_START, IntradaySync
from tradebot.data.provider import DataProvider
from tradebot.data.providers.ccxt_provider import CcxtProvider
from tradebot.data.providers.yfinance_provider import YFinanceProvider
from tradebot.data.quality import QualityReport, validate_bars
from tradebot.data.restatement import reconcile_overlap
from tradebot.data.symbols import asset_class, normalize_symbol
from tradebot.data.types import BAR_COLUMNS, AssetClass, Symbol, Timeframe
from tradebot.data.universe import DEFAULT_UNIVERSE, SP500_UNIVERSE

log = logging.getLogger(__name__)

# First bar requested when a symbol has no cache and the caller gave no start.
DEFAULT_HISTORY_START: dict[AssetClass, date] = {
    AssetClass.EQUITY: date(2005, 1, 1),
    # 2017: Coinbase ETH-USD has missing daily candles on 2016-05-21/22, which the
    # gap check (correctly) rejects; starting later keeps every crypto series gap-free.
    AssetClass.CRYPTO: date(2017, 1, 1),
}


STATUS_QUARANTINED = "revision_quarantined"


def _empty_bars() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS}, index=index)


def _slice_window(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Keep rows labelled in ``[start, end]`` (a shared batch can span a wider range)."""
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        return frame  # validation reports the malformed index
    out = frame.loc[(index >= _ts(start)) & (index <= _ts(end))]
    out.attrs.update(frame.attrs)
    return out


def _ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d.isoformat(), tz="UTC")


class MarketData:
    """Cached, validated, closed daily bars behind one small interface."""

    def __init__(
        self,
        config: DataConfig | None = None,
        providers: Sequence[DataProvider] | None = None,
        clock: Clock | None = None,
        *,
        cache: BarCache | None = None,
        offline: bool = False,
        intraday_history: DataProvider | None = None,
        intraday_live: DataProvider | None = None,
    ) -> None:
        self._config = config if config is not None else DataConfig()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._offline = offline or self._config.offline
        if providers is None:
            providers = [
                YFinanceProvider(
                    clock=self._clock,
                    equity_close_buffer_min=self._config.equity_close_buffer_min,
                ),
                CcxtProvider(self._config.crypto_exchange, clock=self._clock),
            ]
        self._providers: list[DataProvider] = list(providers)
        self._cache = cache if cache is not None else BarCache(self._config.cache_dir, self._clock)
        self._intraday = IntradaySync(
            self._cache, self._config, self._clock, intraday_history, intraday_live
        )
        self._head_tried: dict[tuple[str, str], date] = {}
        # (source, symbol) -> (cache generation at the time, error). Memoises a quarantined
        # revision so repeated refreshes do not re-fetch; cleared by ``rebase``.
        self._wedged: dict[tuple[str, str], tuple[int, RevisionQuarantinedError]] = {}

    # ---- routing --------------------------------------------------------
    @property
    def offline(self) -> bool:
        return self._offline

    def _provider_for(self, symbol: Symbol) -> DataProvider:
        for provider in self._providers:
            if provider.supports(symbol):
                return provider
        raise UnknownSymbolError(f"no provider supports {symbol!r}")

    @staticmethod
    def _source_of(provider: DataProvider) -> str:
        """Cache source key: the provider name without the ``ccxt:`` prefix."""
        return provider.name.removeprefix("ccxt:")

    def _source_for(self, symbol: Symbol) -> str:
        return self._source_of(self._provider_for(symbol))

    def _basis(self, symbol: Symbol) -> str:
        """Basis label: ``spot_<exchange>`` for crypto, ``adj_close_as_of_<date>`` for equity."""
        if asset_class(symbol) is AssetClass.CRYPTO:
            return f"spot_{self._config.crypto_exchange}"
        return f"{REAL_BASIS_PREFIX}{self._clock.now().date().isoformat()}"

    # ---- time helpers ---------------------------------------------------
    def _target(self, symbol: Symbol) -> date:
        return last_closed_bar_date(symbol, self._clock.now(), self._config.equity_close_buffer_min)

    @staticmethod
    def _floor(symbol: Symbol, d: date) -> date:
        if asset_class(symbol) is AssetClass.EQUITY:
            return max(d, XNYS_START.date())
        return d

    # ---- validation -----------------------------------------------------
    def _validate(
        self, symbol: Symbol, frame: pd.DataFrame, refetch: Any = None
    ) -> tuple[pd.DataFrame, QualityReport]:
        now = self._clock.now()
        try:
            return validate_bars(frame, symbol, now, self._config.equity_close_buffer_min)
        except DataQualityError:
            if refetch is None or asset_class(symbol) is not AssetClass.CRYPTO:
                raise
            log.warning("%s: quality failure, retrying fetch once", symbol)
            return validate_bars(refetch(), symbol, now, self._config.equity_close_buffer_min)

    # ---- refresh planning -----------------------------------------------
    def _tail_plan(
        self, symbol: Symbol, source: str, req_start: date | None
    ) -> tuple[date, date] | None:
        target = self._target(symbol)
        if not self._cache.exists(source, symbol):
            start = self._floor(symbol, req_start or DEFAULT_HISTORY_START[asset_class(symbol)])
            return (start, target) if start <= target else None
        last = date.fromisoformat(self._cache.gen_info(source, symbol)["last"])
        if last >= target:
            return None
        tail = self._cache.read(source, symbol).tail(self._config.overlap_bars)
        return (tail.index[0].date(), target)

    def _ingest_tail(
        self,
        symbol: Symbol,
        provider: DataProvider,
        frame: pd.DataFrame,
        refetch: Any,
        window: tuple[date, date] | None = None,
    ) -> QualityReport:
        source = self._source_of(provider)
        provider_warnings = list(frame.attrs.get("provider_warnings", []))
        raw_empty = frame.empty
        clean, report = self._validate(symbol, frame, refetch)
        for message in provider_warnings:
            report.warnings.append(message)
        if raw_empty:
            span = f" for {window[0]}..{window[1]}" if window else ""
            message = f"provider returned no rows{span}"
            log.warning("%s: %s", symbol, message)
            report.warnings.append(message)
        if clean.empty:
            return report
        if not self._cache.exists(source, symbol):
            self._cache.create(source, symbol, clean, self._basis(symbol), provider.name)
        else:
            self._cache.apply_fetch(
                source, symbol, clean, provider.name, overlap_bars=self._config.overlap_bars
            )
        return report

    def _sync_head(self, symbol: Symbol, start: date) -> None:
        """Prepend history older than the first cached bar (never touches cached rows)."""
        provider = self._provider_for(symbol)
        source = self._source_of(provider)
        if not self._cache.exists(source, symbol):
            return
        start = self._floor(symbol, start)
        first = date.fromisoformat(self._cache.gen_info(source, symbol)["first"])
        tried = self._head_tried.get((source, symbol))
        if start >= first or (tried is not None and tried <= start):
            return
        self._head_tried[(source, symbol)] = start
        head = self._cache.read(source, symbol).head(self._config.overlap_bars)
        fetch_end = head.index[-1].date()
        raw = provider.fetch_bars(symbol, start, fetch_end)
        clean, _ = self._validate(
            symbol, raw, lambda: provider.fetch_bars(symbol, start, fetch_end)
        )
        shared = head.index.intersection(clean.index)
        if len(shared) == 0:
            log.warning("%s: head fetch has no overlap with cache; not prepending", symbol)
            return
        try:  # same rule as the tail: tolerance, min overlap, constant ratio
            scale = reconcile_overlap(head, clean).ratio
        except DataRevisionError:
            self._cache.quarantine(source, symbol, clean)
            raise RevisionQuarantinedError(symbol, self._clock.now().date()) from None
        rows = clean.loc[clean.index < head.index[0]].copy()
        if rows.empty:
            return
        if scale != 1.0:
            for col in ("open", "high", "low", "close"):
                rows[col] = rows[col] * scale
        self._cache.append(source, symbol, rows, provider.name)

    def _cached_len(self, source: str, symbol: Symbol) -> int:
        try:
            if self._cache.exists(source, symbol):
                return len(self._cache.read(source, symbol))
        except DataError:
            pass
        return 0

    def _wedge(self, source: str, symbol: Symbol) -> RevisionQuarantinedError:
        """Record (and return) the quarantine memo for ``symbol``."""
        err = RevisionQuarantinedError(symbol, self._clock.now().date())
        self._wedged[(source, symbol)] = (self._cache.current_gen(source, symbol), err)
        log.error("%s", err)
        return err

    def _active_wedge(self, source: str, symbol: Symbol) -> RevisionQuarantinedError | None:
        memo = self._wedged.get((source, symbol))
        if memo is None:
            return None
        gen, err = memo
        try:
            same_gen = self._cache.current_gen(source, symbol) == gen
        except DataError:
            same_gen = False
        if not same_gen:  # rebased elsewhere: the memo is stale
            del self._wedged[(source, symbol)]
            return None
        return err

    def _failure_report(self, symbol: Symbol, exc: DataError, source: str | None) -> QualityReport:
        n_bars = self._cached_len(source, symbol) if source else 0
        if isinstance(exc, RevisionQuarantinedError):
            return QualityReport(
                symbol=symbol,
                n_bars=n_bars,
                warnings=[quarantine_warning(exc.since)],
                status=STATUS_QUARANTINED,
            )
        status = "corrupt" if isinstance(exc, CacheCorruptError) else "error"
        return QualityReport(
            symbol=symbol,
            n_bars=n_bars,
            warnings=[f"{status}: {exc}"],
            status=status,
        )

    def _sync_tails(
        self, symbols: Sequence[Symbol], req_start: date | None
    ) -> tuple[dict[Symbol, QualityReport], dict[Symbol, DataError]]:
        """Refresh the tail of each symbol (batching providers with ``fetch_many``).

        Errors are isolated per symbol: a failure is collected in the second result (and
        reported in the first) and the remaining symbols are still refreshed. A symbol whose
        fetch was quarantined as a revision is memoised and not re-fetched until ``rebase``.
        """
        reports: dict[Symbol, QualityReport] = {}
        errors: dict[Symbol, DataError] = {}

        def fail(symbol: Symbol, exc: DataError, source: str | None) -> None:
            log.error("%s: refresh failed: %s", symbol, exc)
            errors[symbol] = exc
            reports[symbol] = self._failure_report(symbol, exc, source)

        plans: dict[Symbol, tuple[DataProvider, date, date]] = {}
        for symbol in symbols:
            source = None
            try:
                provider = self._provider_for(symbol)
                source = self._source_of(provider)
                plan = self._tail_plan(symbol, source, req_start)
                if plan is None:
                    continue
                wedge = self._active_wedge(source, symbol)
                if wedge is not None:
                    fail(symbol, wedge, source)
                    continue
                plans[symbol] = (provider, plan[0], plan[1])
            except DataError as exc:
                fail(symbol, exc, source)

        by_provider: dict[int, list[Symbol]] = {}
        for symbol, (provider, _, _) in plans.items():
            by_provider.setdefault(id(provider), []).append(symbol)

        for group in by_provider.values():
            provider = plans[group[0]][0]
            source = self._source_of(provider)
            frames: dict[Symbol, pd.DataFrame] = {}
            if len(group) > 1 and hasattr(provider, "fetch_many"):
                lo = min(plans[s][1] for s in group)
                hi = max(plans[s][2] for s in group)
                try:
                    frames = provider.fetch_many(group, lo, hi)
                except DataError as exc:
                    for symbol in group:
                        fail(symbol, exc, source)
                    continue
            for symbol in group:
                _, a, b = plans[symbol]
                try:
                    if symbol in frames:
                        frame = _slice_window(frames[symbol], a, b)
                    else:
                        frame = provider.fetch_bars(symbol, a, b)
                    reports[symbol] = self._ingest_tail(
                        symbol,
                        provider,
                        frame,
                        lambda p=provider, s=symbol, a=a, b=b: p.fetch_bars(s, a, b),
                        (a, b),
                    )
                except RevisionQuarantinedError as exc:
                    fail(symbol, exc, source)
                except DataRevisionError:
                    fail(symbol, self._wedge(source, symbol), source)
                except DataError as exc:
                    fail(symbol, exc, source)
        return reports, errors

    def _cache_report(self, symbol: Symbol) -> QualityReport:
        """Validate what is cached for ``symbol`` (no provider calls)."""
        try:
            source = self._source_for(symbol)
            if not self._cache.exists(source, symbol):
                return QualityReport(symbol=symbol, n_bars=0, warnings=["no cached data"])
            frame = self._cache.read(source, symbol)
        except DataError as exc:
            return self._failure_report(symbol, exc, None)
        try:
            _, report = validate_bars(
                frame, symbol, self._clock.now(), self._config.equity_close_buffer_min
            )
        except DataQualityError as exc:
            return QualityReport(
                symbol=symbol,
                n_bars=len(frame),
                warnings=[f"quality error: {exc}"],
                status="quality_error",
            )
        return report

    # ---- reads ----------------------------------------------------------
    def _read(
        self,
        symbol: Symbol,
        start: date | None,
        end: date | None,
        generation: int | None = None,
    ) -> pd.DataFrame:
        source = self._source_for(symbol)
        if not self._cache.exists(source, symbol):
            return _empty_bars()
        frame = self._cache.read(
            source,
            symbol,
            _ts(start) if start else None,
            _ts(end) if end else None,
            generation,
        )
        cutoff = _ts(self._target(symbol))
        frame = frame.loc[frame.index <= cutoff]
        return frame if not frame.empty else _empty_bars()

    def _check_fresh(self, symbol: Symbol, frame: pd.DataFrame) -> None:
        expected = self._target(symbol)
        if frame.empty:
            raise StaleDataError(f"{symbol}: no bars; expected last closed bar {expected}")
        last = frame.index[-1].date()
        if last < expected:
            raise StaleDataError(f"{symbol}: last bar {last} is behind expected {expected}")

    def get_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        timeframe: Timeframe = Timeframe.D1,
        refresh: bool = True,
        generation: int | None = None,
        require_fresh: bool = False,
    ) -> pd.DataFrame:
        """Closed daily bars with label date in ``[start, end]`` inclusive.

        Empty (right columns and index) if none. ``refresh=False``, offline mode or an
        explicit ``generation`` read the cache only.
        """
        return self.get_bars_many(
            [symbol],
            start,
            end,
            timeframe=timeframe,
            refresh=refresh,
            generation=generation,
            require_fresh=require_fresh,
        )[normalize_symbol(symbol)]

    def get_bars_many(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        timeframe: Timeframe = Timeframe.D1,
        refresh: bool = True,
        generation: int | None = None,
        require_fresh: bool = False,
    ) -> dict[Symbol, pd.DataFrame]:
        """Like :meth:`get_bars` for many symbols; equities share one batch download."""
        canon = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
        if timeframe is Timeframe.M15:
            return self._get_bars_many_15m(
                canon, start, end, refresh, generation, require_fresh=require_fresh
            )
        if timeframe is not Timeframe.D1:
            raise DataError(f"unsupported timeframe {timeframe}")
        if refresh and not self._offline and generation is None:
            _, errors = self._sync_tails(canon, start)
            for symbol in canon:
                if symbol in errors:
                    continue
                try:
                    self._sync_head(symbol, start)
                except DataError as exc:
                    errors[symbol] = exc
            if errors:  # every symbol was attempted; surface the first failure
                raise next(iter(errors.values()))
        out: dict[Symbol, pd.DataFrame] = {}
        for symbol in canon:
            frame = _empty_bars() if end < start else self._read(symbol, start, end, generation)
            if require_fresh:
                self._check_fresh(symbol, self._read(symbol, None, None, generation))
            out[symbol] = frame
        return out

    def _get_bars_many_15m(
        self,
        canon: list[Symbol],
        start: date,
        end: date,
        refresh: bool,
        generation: int | None,
        *,
        require_fresh: bool,
    ) -> dict[Symbol, pd.DataFrame]:
        """15m bars with New York session date in ``[start, end]`` (A21).

        History comes from the SIP cache; the not-yet-cached last few bars come from the
        live (IEX) provider, in memory only. ``require_fresh`` tolerates 4 missing bars.
        """
        online = refresh and not self._offline and generation is None
        if online:
            _, errors = self._intraday.refresh(canon, start)
            if errors:  # every symbol was attempted; surface the first failure
                raise next(iter(errors.values()))
        out: dict[Symbol, pd.DataFrame] = {}
        for symbol in canon:
            if end < start:
                out[symbol] = _empty_bars()
                continue
            frame = self._intraday.read(symbol, start, end)
            if online:
                frame = self._intraday.with_live_tail(symbol, frame, start, end)
            if require_fresh:
                latest = self._intraday.read(symbol, None, None)
                if online:
                    latest = self._intraday.with_live_tail(symbol, latest, start, end)
                self._intraday.check_fresh(symbol, latest, live=online)
            out[symbol] = frame
        return out

    def latest_bar(self, symbol: str, *, require_fresh: bool = True) -> pd.Series | None:
        """Most recent closed bar (name = Timestamp, index = BAR_COLUMNS), or None.

        Refreshes the tail first (unless offline). With ``require_fresh`` raises
        :class:`StaleDataError` if it is not the expected last closed bar.
        """
        canon = normalize_symbol(symbol)
        if not self._offline:
            _, errors = self._sync_tails([canon], None)
            if canon in errors:
                raise errors[canon]
        frame = self._read(canon, None, None)
        if require_fresh:
            self._check_fresh(canon, frame)
        if frame.empty:
            return None
        row = frame.iloc[-1]
        row.name = frame.index[-1]
        return row

    # ---- maintenance ----------------------------------------------------
    def refresh(
        self,
        symbols: Sequence[str] | None = None,
        *,
        timeframe: Timeframe = Timeframe.D1,
        start: date | None = None,
    ) -> dict[Symbol, QualityReport]:
        """Fetch the missing tail (with overlap window) for each symbol.

        Defaults to ``DEFAULT_UNIVERSE`` (``SP500_UNIVERSE`` for 15m). Offline: reports on the
        cache only. ``start`` applies to 15m only (default 2016-01-01 for new symbols).
        """
        if timeframe is Timeframe.M15:
            canon15 = list(dict.fromkeys(normalize_symbol(s) for s in (symbols or SP500_UNIVERSE)))
            if self._offline:
                return {s: self._intraday.cache_report(s) for s in canon15}
            reports15, _ = self._intraday.refresh(canon15, start or DEFAULT_INTRADAY_START)
            return {
                s: reports15[s] if s in reports15 else self._intraday.cache_report(s)
                for s in canon15
            }
        if timeframe is not Timeframe.D1:
            raise DataError(f"unsupported timeframe {timeframe}")
        canon = list(dict.fromkeys(normalize_symbol(s) for s in (symbols or DEFAULT_UNIVERSE)))
        reports = {} if self._offline else self._sync_tails(canon, None)[0]
        return {s: reports[s] if s in reports else self._cache_report(s) for s in canon}

    def verify(
        self, symbols: Sequence[str] | None = None, *, timeframe: Timeframe = Timeframe.D1
    ) -> dict[Symbol, QualityReport]:
        """Check cache integrity (sha256) and re-run quality validation on cached bars.

        Never calls a provider. A quality failure is reported in ``warnings`` rather
        than raised, and so is a corrupt cache file (``status="corrupt"``), so one bad
        symbol does not hide the rest.
        """
        if timeframe is Timeframe.M15:
            canon15 = list(dict.fromkeys(normalize_symbol(s) for s in (symbols or SP500_UNIVERSE)))
            return {s: self._intraday.cache_report(s) for s in canon15}
        canon = list(dict.fromkeys(normalize_symbol(s) for s in (symbols or DEFAULT_UNIVERSE)))
        return {s: self._cache_report(s) for s in canon}

    def rebase(self, symbol: str) -> int:
        """Full re-fetch into a new generation; returns the new generation id."""
        if self._offline:
            raise DataError("rebase requires a provider; MarketData is offline")
        canon = normalize_symbol(symbol)
        provider = self._provider_for(canon)
        source = self._source_of(provider)
        if self._cache.exists(source, canon):
            start = date.fromisoformat(self._cache.gen_info(source, canon)["first"])
        else:
            start = DEFAULT_HISTORY_START[asset_class(canon)]
        start = self._floor(canon, start)
        end = self._target(canon)
        raw = provider.fetch_bars(canon, start, end)
        clean, _ = self._validate(canon, raw, lambda: provider.fetch_bars(canon, start, end))
        if clean.empty:
            raise ProviderError(f"{canon}: rebase fetch returned no bars")
        if not self._cache.exists(source, canon):
            generation = self._cache.create(source, canon, clean, self._basis(canon), provider.name)
        else:
            generation = self._cache.rebase(source, canon, clean, self._basis(canon), provider.name)
        self._wedged.pop((source, canon), None)
        self._head_tried.pop((source, canon), None)
        return generation

    def data_fingerprint(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        generation: int | None = None,
    ) -> str:
        """Hash of (source, symbol, generation, sha256) plus the window, for backtest records."""
        canon = [normalize_symbol(s) for s in symbols]
        entries = [(self._source_for(s), s) for s in canon]
        return self._cache.fingerprint(entries, _ts(start), _ts(end), generation)


def refresh(
    symbols: Sequence[str] | None = None, *, config: DataConfig | None = None
) -> dict[Symbol, QualityReport]:
    """Build a ``MarketData(config)`` and refresh ``symbols`` (default: the universe)."""
    return MarketData(config).refresh(symbols)


def verify(
    symbols: Sequence[str] | None = None, *, config: DataConfig | None = None
) -> dict[Symbol, QualityReport]:
    """Build a ``MarketData(config)`` and verify the cache for ``symbols``."""
    return MarketData(config).verify(symbols)


def rebase(symbol: str, *, config: DataConfig | None = None) -> int:
    """Build a ``MarketData(config)`` and rebase ``symbol``; returns the new generation id."""
    return MarketData(config).rebase(symbol)


__all__ = [
    "DEFAULT_HISTORY_START",
    "STATUS_QUARANTINED",
    "MarketData",
    "RevisionQuarantinedError",
    "rebase",
    "refresh",
    "verify",
]
