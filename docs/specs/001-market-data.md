# SPEC-001: Market data layer

- **Lead:** data-lead
- **Status:** approved with amendments (see ADR-0001 §4; ADR wins on conflict)
- **Roadmap item:** Phase 1: Market data (and Phase 0.5 research round)
- **Related ADRs/specs:** ADR-0001 (pending). Siblings: SPEC-002 strategy/backtest, SPEC-003 execution/sim/risk/portfolio, SPEC-004 platform, SPEC-005 quality.

## Goal
Provide clean, cached, bias-free **daily** OHLCV bars for the STRATEGY.md universe (17 ETFs + BTC/ETH/SOL vs USD) behind one small, mockable interface. Cached history is immutable, so a backtest run today and the same run next month see identical numbers. Tests run fully offline from a committed fixture.

## Non-goals
- Intraday bars (interfaces leave room; nothing is implemented). Order books, trades, fundamentals, news.
- Indicators, signals, fills, accounting, scheduling, API/UI (owned by strategy, execution, platform leads).
- Individual stocks and date-aware universe data (v1 is a fixed ETF list; see Survivorship).
- Any broker, key, or authenticated endpoint. All sources are keyless public data.

## Design

### 1. Conventions (the contract every area relies on)
| Item | Rule |
|---|---|
| Frequency | `Timeframe.D1` only in v1. `Timeframe` is an enum so `H1` etc. can be added later without changing signatures. |
| Index | tz-aware UTC `DatetimeIndex`, name `"ts"`, sorted ascending, unique. Each timestamp is the **bar's label date at 00:00 UTC** (the session date for stocks; the UTC day the candle opens for crypto). |
| Columns | exactly `open, high, low, close, volume`, dtype `float64`. Nothing else is returned to callers. |
| Known-at time | A bar is "known" only at its close: crypto bar `d` closes at `d+1 00:00 UTC`; equity bar `d` closes at 16:00 America/New_York on session `d` (13:00 on early-close days). `bar_close_time()` exposes this. |
| Closed bars only | The in-progress bar is never returned or cached. Crypto: drop the bar whose label date is today (UTC). Equity: drop session `d` until `close_time(d) + 60 min` has passed (Yahoo's final adjusted values lag the close). |
| Stock adjustment | Prices are split- and dividend-adjusted (total-return basis): fetched with `auto_adjust=False`, then `adj = Adj Close / Close` is applied to open/high/low/close; volume is left as reported (not split-adjusted). See Section 5 for why we do not use `auto_adjust=True`. |
| Crypto adjustment | None. Spot candles are not restated. |
| Symbols | Canonical form is upper case: `SPY`, `BTC/USD`. Asset class is derived by `AssetClass.EQUITY` if no `/`, else `AssetClass.CRYPTO`. Provider-specific forms (Yahoo `BTC-USD`, exchange ids) never leak out of adapters. |
| Money | Bars are floats (analytics domain). Execution converts to `Decimal` at the fill boundary (CLAUDE.md rule 3). |

### 2. Sources and limits (verified)
**yfinance (stocks/ETFs).** `Ticker.history()`/`download()` is an unofficial scraper of Yahoo, not an API with a contract.
- Intraday history is capped (docstring: "Intraday data cannot extend last 60 days"); daily history goes back decades with `period="max"` or `start`/`end`. This is why v1 is daily only and why any future intraday must be captured forward and cached, not back-filled. [yfinance `history.py`](https://github.com/ranaroussi/yfinance/blob/main/yfinance/scrapers/history.py)
- `auto_adjust` defaults to `True`; `repair` (fixes 100x errors, missing values) defaults to `False`; `back_adjust` exists. We pass `auto_adjust=False, repair=False` and adjust ourselves (Section 5). Same source.
- No published rate limit. Yahoo answers abusive traffic with HTTP 429 / IP blocks, so adapters batch tickers in one `download()` call, back off exponentially on 429/empty frames, and cache aggressively. ([Apify Yahoo OHLCV notes](https://apify.com/superhero99999/yahoo-finance-ohlcv), secondary source; treat as unverified, hence the defensive design.)
- A secondary source reports yfinance 1.6.0 on PyPI (Aug 2026). Pin a minimum version at implementation time and re-check the `history()` signature. ([overview](https://orthogonal.info/yfinance-python-stock-data-live-streaming-screening/))

**ccxt (crypto), exchange choice: Coinbase Exchange (`ccxt.coinbaseexchange`, public REST `api.exchange.coinbase.com`).**
- Why: native USD quote (no USDT stablecoin proxy), US-regulated so reachable by US users, keyless public candles, BTC/ETH/SOL-USD all listed, deep history. Official docs: candles endpoint accepts granularity `60, 300, 900, 3600, 21600, 86400` s, **max 300 candles per request** (larger ranges need multiple start/end windows), and docs advise against polling history frequently. [Coinbase Exchange: Get product candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles.md)
- 300 daily candles per call means ~13 calls for 10 years per symbol; trivial.
- Not documented, so to be confirmed by a one-off live smoke test in T6: (a) whether the 1d bucket is aligned to 00:00 UTC, (b) the numeric rate limit. ccxt sets `enableRateLimit=True` and we keep >=0.5 s between pages.
- **Fallback: Kraken (`ccxt.kraken`, `XBT/USD` handled by ccxt as `BTC/USD`).** Kraken OHLC returns max 720 candles per call (~2 years daily) and recommends >=1 s between calls; its guide says omitting `since` returns the oldest data and you page forward with the returned `last`, but we have not verified this against ccxt's wrapper. [Kraken historical data guide](https://docs.kraken.com/exchange/guides/general/historical-data) Exchange id is config (`crypto_exchange: str = "coinbaseexchange"`), so swapping is a config change.
- ccxt `fetch_ohlcv(symbol, timeframe, since_ms, limit)` is the uniform call; `exchange.timeframes` lists supported timeframes (not all exchanges support all). Source: [ccxt manual / examples](https://docs.ccxt.com/docs/examples/py/async-kucoin-rate-limit).

Crypto coverage caveat: Coinbase SOL-USD listing started in 2021, so 5-year SOL history (STRATEGY validation gate) may be short. Surfaced as a risk and an open question.

### 3. Architecture
```
MarketData (facade: routing + cache policy)  <-- the only thing other areas import
  |-- BarCache  (parquet, generations, immutable)
  |-- DataProvider (Protocol)
        |-- YFinanceProvider      (EQUITY)
        |-- CcxtProvider          (CRYPTO)
        |-- FixtureProvider       (offline tests; reads committed parquet)
  |-- quality.validate_bars()   (run on every fetch before it can reach the cache)
  |-- calendar helpers          (exchange_calendars XNYS; 24/7 for crypto)
```
Flow for `get_bars`: normalize symbol -> cache lookup -> if range not fully covered (and refresh allowed) fetch missing tail from provider -> validate -> chain-link onto cached basis (Section 5) -> append to cache atomically -> return slice. History before the first cached bar is fetched once, validated and prepended only if it does not overlap cached rows (it can't change them).

### 4. Cache layout
```
<cache_dir>/                         # Config.cache_dir, default ~/.tradebot/cache; env TRADEBOT_CACHE_DIR
  <source>/<SYMBOL_FS>/              # source in {yfinance, coinbaseexchange}; SYMBOL_FS = BTC_USD, SPY
    meta.json                        # {"current_gen": 2, "tf": "1d", "schema": 1}
    gen-0001/bars.parquet            # columns: open..volume (float64), fetched_at (UTC), source_rev
    gen-0001/gen.json                # {"created", "basis": "adj_close_as_of_<date>", "first", "last", "sha256"}
    gen-0002/...                     # created only by explicit rebase (Section 5)
```
- Parquet via pyarrow, snappy. `fetched_at` and `source_rev` are cache-internal audit columns; `read` strips them.
- Writes are atomic (write temp file in same dir, `os.replace`). A `gen.json` sha256 is verified on read (`CacheCorruptError` on mismatch).
- **Append-only within a generation.** Existing rows are never rewritten. A cache write that would alter any existing row raises `ImmutabilityError`.
- `data_fingerprint(symbols, start, end) -> str` hashes (symbol, gen id, sha256) so backtest results can record exactly which data they used.
- Cache is per source, so switching crypto exchange never mixes histories.

### 5. Data quality and the adjusted-price restatement problem
**The problem.** Adjusted prices are a function of *future* corporate actions. When SPY pays a dividend or a fund splits, Yahoo recomputes the adjusted close of *all prior history* by a constant factor. A naive "re-fetch and overwrite" silently changes history, and a naive "append only new bars" produces a discontinuity (the new bars sit on a different basis than the old).

**Handling (chain-linking, immutable history):**
1. Fetch with `auto_adjust=False` to get raw `Close` and `Adj Close`; derive `adj = Adj Close / Close` per bar; apply to OHLC. (Using the same fetch for all columns keeps one consistent basis.)
2. On incremental update, always request an **overlap window** of the last `overlap_bars=10` cached bars plus the new ones.
3. Compute `ratio_i = cached_close_i / new_close_i` over the overlap.
   - If `max|ratio_i - 1| <= 1e-6`: no restatement; append only rows strictly after `cached_last`.
   - Else if ratios are constant across the overlap (`std(ratio) / mean(ratio) <= 1e-6`): **pure restatement** (dividend or split). Multiply the new rows' OHLC by `mean(ratio)` so they sit on the cached basis, then append. Old rows are untouched. Record `restated_at`, `ratio` in `gen.json`'s `events` list and log a WARNING. Price series stay continuous; the cost is that the forward series is price-return-like for that dividend (ex-date drop shows as a small loss vs total return). Acceptable for forward paper trading of months; documented.
   - Else (ratios vary): genuine revision or corruption. Quarantine the fetch to `<symbol>/quarantine/<ts>.parquet`, raise `DataRevisionError`; nothing is appended.
4. **Rebase** (`MarketData.rebase(symbol)`, human-triggered, CLI `rebase`): full re-fetch of history into a **new generation** (`gen-N+1`), switch `current_gen` in `meta.json`, keep old generations. Used between research experiments to get a clean total-return basis. Backtest outputs must store `data_fingerprint`, so old results remain reproducible against the old generation (`get_bars(..., generation=1)`).
5. Splits for ETFs are rare and handled by the same ratio logic (ratio = split factor). Volume is not split-adjusted; strategies must not compare volume across a split (documented, not corrected in v1).

**Per-fetch validation (`quality.validate_bars`), run before caching; raises `DataQualityError` listing violations:**
| Check | Rule | On failure |
|---|---|---|
| Schema | exact columns, float64, UTC tz-aware sorted index | error |
| Duplicates | duplicate timestamps | drop duplicates keeping last **within the fetch only**, log WARNING (never across the cache boundary) |
| Non-positive prices | any of o/h/l/c `<= 0` or NaN | error, row quarantined |
| OHLC sanity | `low <= min(open,close)` and `high >= max(open,close)`; `volume >= 0` | error |
| Outliers | `abs(close/prev_close - 1) > 0.5` for EQUITY ETFs (> 0.6 crypto) | WARNING only (real crashes exist), flagged in `QualityReport` |
| Gaps (equity) | session dates in `calendar.sessions(start,end)` missing from the index | error if > `max_gap_sessions=0` interior gaps; leading/trailing ignored |
| Gaps (crypto) | any missing UTC date inside `[first,last]` (24/7) | error; 1-day gaps are retried once from the provider before failing |
| Zero volume | equity bar with `volume == 0` on a session day | WARNING |
| Staleness | last bar older than expected last closed bar by > 1 session (equity) / 1 day (crypto) | `StaleDataError` from `get_bars` only when `require_fresh=True` (paper runner) |

`QualityReport` (dataclass: `symbol, n_bars, gaps, duplicates_dropped, outliers, warnings`) is returned by `MarketData.verify()` and used by the CLI and dashboard.

### 6. Survivorship and date-aware universes
v1 universe is the fixed STRATEGY.md ETF list (SPY, QQQ, ... ; BTC/ETH/SOL). Caveat recorded: this list was chosen with hindsight, and several ETFs did not exist before 2007-2010 (XLC excluded for that reason; EEM from 2003, IEF/TLT from 2002), so backtest start dates are bounded by the youngest member; `universe.py` exposes first-available date per symbol from cache.

Plug-in point (interface only, shipped as a trivial implementation):
```python
class Universe(Protocol):
    def members(self, as_of: date) -> list[Symbol]: ...
class StaticUniverse:  # v1: returns the same list for every date, filtered to symbols with data at as_of
```
Later, a `PointInTimeUniverse` can read a `(symbol, start_date, end_date)` membership table (e.g., index constituent history, including delisted tickers whose bars are cached under their own symbol). `MarketData` already needs no change because delisted symbols are just cache entries with a `last` date; strategies call `universe.members(t)` rather than hardcoding lists.

### Interfaces
```python
# src/tradebot/data/types.py
from enum import Enum
from datetime import date, datetime
from typing import Protocol, NewType
import pandas as pd

Symbol = NewType("Symbol", str)            # canonical: "SPY", "BTC/USD"

class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"

class Timeframe(str, Enum):
    D1 = "1d"                               # v1 only; H1 etc. added later

BAR_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")

# src/tradebot/data/errors.py
class DataError(Exception): ...
class DataQualityError(DataError): ...
class DataRevisionError(DataError): ...     # non-constant ratio on overlap
class ImmutabilityError(DataError): ...
class CacheCorruptError(DataError): ...
class StaleDataError(DataError): ...
class ProviderError(DataError): ...         # network, 429 after retries, empty frame
class UnknownSymbolError(DataError): ...

# src/tradebot/data/symbols.py
def normalize_symbol(raw: str) -> Symbol: ...        # "spy"->"SPY", "btc-usd"/"BTCUSD"->"BTC/USD"; raises UnknownSymbolError
def asset_class(symbol: Symbol) -> AssetClass: ...
def to_yahoo(symbol: Symbol) -> str: ...             # "BTC/USD" -> "BTC-USD" (only used by adapters)

# src/tradebot/data/provider.py   (swappable + mockable)
class DataProvider(Protocol):
    name: str                                         # cache source key
    def supports(self, symbol: Symbol) -> bool: ...
    def fetch_bars(self, symbol: Symbol, start: date, end: date,
                   timeframe: Timeframe = Timeframe.D1) -> pd.DataFrame:
        """Closed bars with label date in [start, end], canonical schema (Section 1),
        already on the adjusted basis. No caching, no validation here."""

# src/tradebot/data/config.py
@dataclass(frozen=True)
class DataConfig:
    cache_dir: Path = Path("~/.tradebot/cache").expanduser()   # env TRADEBOT_CACHE_DIR
    crypto_exchange: str = "coinbaseexchange"
    overlap_bars: int = 10
    equity_close_buffer_min: int = 60
    offline: bool = False                              # True => never call providers (tests, CI)
```

## Interfaces offered to other areas
Import only from `tradebot.data` (re-exported in `__init__.py`). Everything below is sync and pure-read from the caller's view.

```python
class MarketData:
    def __init__(self, config: DataConfig | None = None,
                 providers: Sequence[DataProvider] | None = None,
                 clock: Callable[[], datetime] = utc_now) -> None: ...

    # --- history (backtester, strategy warm-up, dashboard) ---
    def get_bars(self, symbol: str, start: date, end: date, *,
                 timeframe: Timeframe = Timeframe.D1,
                 refresh: bool = True,              # False => cache only (backtests)
                 generation: int | None = None) -> pd.DataFrame:
        """Closed daily bars with label date in [start, end] inclusive. Empty DataFrame
        with the right columns/index if none. Never contains an unclosed bar."""

    def get_bars_many(self, symbols: Sequence[str], start: date, end: date,
                      **kw) -> dict[Symbol, pd.DataFrame]: ...   # one yfinance batch call

    # --- live / paper loop (runner) ---
    def latest_bar(self, symbol: str, *, require_fresh: bool = True) -> pd.Series | None:
        """Most recent CLOSED bar as a Series (name = Timestamp, index = BAR_COLUMNS).
        Refreshes the tail first. Raises StaleDataError if require_fresh and it is
        not the expected last closed bar."""

    def refresh(self, symbols: Sequence[str] | None = None) -> dict[Symbol, "QualityReport"]: ...
    def rebase(self, symbol: str) -> int: ...            # new generation id; human-triggered
    def verify(self, symbols: Sequence[str] | None = None) -> dict[Symbol, "QualityReport"]: ...
    def data_fingerprint(self, symbols: Sequence[str], start: date, end: date) -> str: ...

# src/tradebot/data/calendar.py  (trading-calendar helper)
def is_session(symbol: str, d: date) -> bool:
    """EQUITY: NYSE session (holidays, early closes via exchange_calendars XNYS). CRYPTO: always True."""
def bar_close_time(symbol: str, d: date) -> datetime:
    """Tz-aware UTC instant at which bar labelled d is final. Strategies/backtester use it as 'known-at'."""
def last_closed_bar_date(symbol: str, now: datetime) -> date:
    """Label date of the latest bar that is closed (plus the equity buffer) at `now`."""
def new_bar_available(symbol: str, last_seen: date | None, now: datetime) -> bool:
    """True iff last_closed_bar_date(symbol, now) > last_seen. The paper runner's trigger."""
def sessions(symbol: str, start: date, end: date) -> pd.DatetimeIndex:
    """Expected bar labels in [start, end] (UTC midnight): NYSE sessions or every UTC day."""
def next_bar_date(symbol: str, d: date) -> date:
    """Label of the bar after d (next NYSE session / d+1 for crypto). Backtester uses it for the t+1 fill bar."""

# src/tradebot/data/universe.py
class Universe(Protocol):
    def members(self, as_of: date) -> list[Symbol]: ...
class StaticUniverse:
    def __init__(self, symbols: Sequence[str]) -> None: ...
DEFAULT_UNIVERSE: tuple[Symbol, ...]   # the 20 symbols in STRATEGY.md section 3
```
Rules callers can rely on: result index/columns as in Section 1; the returned frame is a copy (mutation never touches the cache); with `refresh=False` and `offline=True` no network is touched; the same call with the same generation returns byte-identical data.

### Invariants / rules
- Cached rows are never modified; only appended (or a new generation created).
- A bar for date `d` contains only information known at `bar_close_time(d)`; the in-progress bar is never returned.
- Every row reaching the cache passed `validate_bars`.
- Provider details (Yahoo ticker form, exchange ids, `Adj Close`) never appear outside adapters.
- No keys, no authenticated endpoints, no order/account endpoints anywhere in `data/`.

## Offline fixture dataset (other specs may rely on this)
Committed under `tests/fixtures/market/` in the canonical cache layout so `MarketData(DataConfig(cache_dir=fixtures, offline=True))` works directly, plus `manifest.json` (symbols, range, source, fetch date, sha256 per file).

| Symbols | Range (label dates, inclusive) | Why |
|---|---|---|
| SPY, QQQ, TLT, GLD, IEF | 2019-01-02 .. 2024-12-31 | Covers a 200-day warm-up before 2020, the COVID crash (V-shaped reversal), 2022 bear (stocks and bonds fall together), 2023-24 recovery. Daily, adjusted. ~1,510 rows each. |
| BTC/USD, ETH/USD | 2019-01-01 .. 2024-12-31 | 24/7 calendar (2,192 rows), 2020 rally, 2022 crash. |
| SOL/USD | 2021-06-01 .. 2024-12-31 | Shorter history; tests uneven start dates. |

Extra tiny hand-made frames (built in-test, not committed) cover defects: duplicate rows, a gap, a zero price, a restated series (x0.98 on history), a 2:1 split, a non-constant revision.
Size estimate under 1 MB total. The fixture is a frozen snapshot: it is regenerated only by `tests/fixtures/build_fixtures.py` (network, run manually, never in CI), and a change requires a PM-approved commit because it shifts downstream test expectations. The fixture build records the Yahoo basis date, so the `adj` factors are deliberately never refreshed by tests.

## Acceptance criteria
1. `normalize_symbol` maps `spy`, `SPY`, `btc-usd`, `BTCUSD`, `BTC/USD` to canonical forms and rejects unknown formats; `asset_class` is correct for both.
2. `get_bars("SPY", ...)` returns a UTC tz-aware, sorted, unique `DatetimeIndex` named `ts` with exactly the five float64 columns.
3. With `offline=True` and the fixture cache, `get_bars` for every fixture symbol returns the expected row counts and makes zero network calls (test asserts providers are never called).
4. Cache round-trip: write then read returns identical frames; sha256 mismatch raises `CacheCorruptError`.
5. Immutability: an attempted overwrite of existing rows raises `ImmutabilityError`; an incremental update leaves all earlier rows byte-identical.
6. Restatement: given a fake provider whose overlap is scaled by a constant factor, new rows are rescaled onto the cached basis, old rows unchanged, an event is recorded. A non-constant mismatch raises `DataRevisionError` and appends nothing. `rebase` creates generation 2 and `get_bars(generation=1)` still returns the original data.
7. `validate_bars` detects duplicates, interior gaps (equity vs calendar, crypto vs every day), non-positive/NaN prices, OHLC inconsistency, and flags (does not fail on) >50% daily moves.
8. The in-progress bar is excluded: with an injected clock at 15:00 ET on a session day, the equity bar for that day is absent; for crypto, today's UTC date is absent; at 00:00:01 UTC the previous crypto day is present.
9. `is_session`/`sessions` are correct for US holidays (e.g., 2024-07-04, 2024-11-28), weekends (equity false, crypto true) and early-close `bar_close_time` (2024-11-29 is 13:00 ET).
10. `new_bar_available` is False before the close+buffer, True after, and False when `last_seen` is already the latest closed bar; correct across weekends/holidays for equity.
11. `latest_bar(require_fresh=True)` raises `StaleDataError` when the cache tail is behind and the provider cannot supply it.
12. Providers are swappable: `MarketData(providers=[FakeProvider()])` works with no yfinance/ccxt import side effects (adapters import lazily).
13. `data_fingerprint` is stable across runs and changes when a generation changes.
14. Fixture manifest hashes match files (guards accidental fixture edits).

## Tasks
Routing per CLAUDE.md: Sonnet if >2 source files, money/state/concurrency, or the implementer must make design choices; else Haiku.

### T1 [haiku] Core types, errors, config
- **Files:** `src/tradebot/data/types.py`, `src/tradebot/data/errors.py`, `src/tradebot/data/config.py`
- **Depends on:** none
- **Do:** implement exactly the `types.py`/`errors.py` blocks above. Also add `src/tradebot/data/config.py` with the frozen `DataConfig` dataclass (pure data; reads env `TRADEBOT_CACHE_DIR`).
- **Done when:** modules import; AC 1 (types part).

### T2 [haiku] Symbol normalization
- **Files:** `src/tradebot/data/symbols.py`
- **Depends on:** T1
- **Do:** `normalize_symbol`, `asset_class`, `to_yahoo` per signatures. Crypto pairs accepted: `BASE/USD`, `BASE-USD`, `BASEUSD` for BASE in a small known set (BTC, ETH, SOL) extendable by a module constant; equities: `^[A-Z]{1,5}$` after upper-casing.
- **Done when:** AC 1.

### T3 [haiku] Trading calendar helpers
- **Files:** `src/tradebot/data/calendar.py`
- **Depends on:** T1
- **Do:** implement the calendar functions using `exchange_calendars` (`XNYS`) for EQUITY and pure date math for CRYPTO. All times converted to UTC. Equity close buffer comes from `DataConfig`.
- **Done when:** AC 8 (calendar part), 9, 10.

### T4 [haiku] Quality validation
- **Files:** `src/tradebot/data/quality.py`
- **Depends on:** T1, T3
- **Do:** `validate_bars(df, symbol, now) -> tuple[pd.DataFrame, QualityReport]` implementing the table in Section 5 exactly; `QualityReport` dataclass.
- **Done when:** AC 7.

### T5 [sonnet] Parquet cache with generations and chain-linking
- **Files:** `src/tradebot/data/cache.py`, `src/tradebot/data/restatement.py`
- **Depends on:** T1, T4
- **Do:** `BarCache` (layout Section 4: atomic writes, sha256, append-only, `ImmutabilityError`, generations, fingerprint) and `reconcile_overlap(cached, fetched, tol) -> ReconcileResult` implementing the three-way ratio logic of Section 5 (no-change / pure restatement / revision).
- **Done when:** AC 4, 5, 6, 13.

### T6 [sonnet] ccxt crypto adapter
- **Files:** `src/tradebot/data/providers/ccxt_provider.py`, `src/tradebot/data/providers/__init__.py`
- **Depends on:** T1, T2
- **Do:** `CcxtProvider(exchange_id)` using `fetch_ohlcv` paged in windows of <=300 candles with `since` in ms, dedupe on timestamp, `enableRateLimit=True`, >=0.5 s between pages, retry with exponential backoff on ccxt `NetworkError`/`RateLimitExceeded`, no keys. Map to the canonical frame (UTC midnight labels, drop unclosed bar). Lazy import of ccxt. Include a manual smoke script `tests/manual/smoke_ccxt.py` (excluded from CI) that confirms 00:00 UTC bucket alignment and that SOL/USD history start date, and report both to the PM.
- **Done when:** AC 2, 8 (crypto), 12 via a mocked exchange object.

### T7 [haiku] yfinance adapter
- **Files:** `src/tradebot/data/providers/yfinance_provider.py`
- **Depends on:** T1, T2, T6 (for `providers/__init__.py`)
- **Do:** `YFinanceProvider` using `yf.download(..., auto_adjust=False, repair=False, progress=False, group_by="ticker")`; derive `adj = Adj Close / Close`, apply to OHLC; tz-localize/normalize to UTC midnight labels; retry with exponential backoff (max 5) on exceptions or empty frames; lazy import; batch method `fetch_many` used by `get_bars_many`. Do not pass `period="max"` for incremental updates.
- **Done when:** AC 2, 12 with `yfinance` monkeypatched.

### T8 [haiku] Fixture provider, universe, fixture builder
- **Files:** `src/tradebot/data/universe.py`, `src/tradebot/data/providers/fixture_provider.py`, `tests/fixtures/build_fixtures.py`
- **Depends on:** T5, T6, T7
- **Do:** `Universe`/`StaticUniverse`/`DEFAULT_UNIVERSE`; `FixtureProvider(root)` reading parquet fixtures; `build_fixtures.py` (manual, network) that populates `tests/fixtures/market/` via a real `MarketData` and writes `manifest.json`. After running, commit the data files.
- **Done when:** AC 3, 14.

### T9 [sonnet] MarketData facade
- **Files:** `src/tradebot/data/service.py`, `src/tradebot/data/__init__.py`, `src/tradebot/data/__main__.py`
- **Depends on:** T3, T4, T5, T6, T7, T8
- **Do:** `MarketData` with all methods in "Interfaces offered", routing by `asset_class`, overlap-window refresh, `require_fresh`, injectable `clock`, `offline` mode; `__main__` CLI with `refresh`, `verify`, `rebase SYMBOL` subcommands (argparse, no scheduling). Re-export public names in `__init__.py`.
- **Done when:** AC 2, 3, 8, 11, 12, 13.

### T10 [haiku] Unit tests (core)
- **Files:** `tests/data/test_symbols.py`, `tests/data/test_calendar.py`, `tests/data/test_quality.py`
- **Depends on:** T2, T3, T4
- **Do:** cover AC 1, 7, 9, 10 using in-test frames.
- **Done when:** pass offline.

### T11 [haiku] Provider adapter tests
- **Files:** `tests/data/test_ccxt_provider.py`, `tests/data/test_yfinance_provider.py`
- **Depends on:** T6, T7
- **Do:** mock `ccxt`/`yf.download`; assert pagination, dedupe, adjusted-price math, unclosed-bar drop, backoff.
- **Done when:** AC 2, 8, 12 pass offline.

### T12 [sonnet] Cache and restatement property tests
- **Files:** `tests/data/test_cache.py`, `tests/data/test_restatement.py`
- **Depends on:** T5
- **Do:** AC 4, 5, 6; property tests (hypothesis, add as dev dependency) that for random fetch sequences old rows are never altered and continuity holds after chain-linking.
- **Done when:** pass offline.

### T13 [haiku] Facade and fixture tests
- **Files:** `tests/data/test_service.py`, `tests/data/test_fixtures.py`
- **Depends on:** T9, T8
- **Do:** AC 3, 8, 11, 13, 14 against the committed fixture; assert providers raise if called.
- **Done when:** pass offline.

Parallelism: T1 first; then T2, T3 in parallel; T4; T5 and T6 in parallel; T7; T8; T9; tests T10-T13 as dependencies allow.

## New runtime dependencies (for pyproject)
| Package | Reason |
|---|---|
| `pandas` | bar frames (already in stack) |
| `pyarrow` | parquet read/write backend for the cache |
| `yfinance` | stocks/ETF daily bars (pin a minimum after checking the installed `history()` signature) |
| `ccxt` | public crypto OHLCV (no keys) |
| `exchange-calendars` | NYSE sessions, holidays, early closes for the calendar helpers (chosen over `pandas_market_calendars` as a lighter, actively maintained XNYS source; confirm at install time) |
| dev: `hypothesis` | property tests for immutability and chain-linking |

## Assumptions about other areas
- **Strategy/backtest (002):** consume `get_bars`/`get_bars_many` with `refresh=False` after a one-time warm cache; compute indicators on the returned frame; signal on bar `t` fills at `next_bar_date(symbol, t)`'s open using `open` from the same frame; they record `data_fingerprint(...)` in each result; they handle warm-up (200-day SMA) by requesting `start` earlier than the evaluation start; they treat BTC and SPY as the benchmarks via the same API; they do not forward-fill across asset-class calendars (equity vs 24/7 alignment is their job; they may reindex with explicit rules).
- **Execution (003):** reads fill prices from bars supplied by the backtester/runner, converts floats to `Decimal` itself, and does not call providers. Weekend/holiday handling for crypto vs equity fills relies on `is_session`.
- **Platform (004):** the runner calls `new_bar_available(symbol, last_seen, now)` on its schedule, then `latest_bar(...)`/`refresh(...)`, and persists `last_seen` itself (data layer keeps no runner state). Platform owns scheduling and the cache directory path in config (`TRADEBOT_CACHE_DIR`), and the dashboard may call `verify()` for a data-health panel. Cache dir must be persistent storage and not on a path synced to cloud drives.
- **Quality (005):** supplies bias audits (shifted-data tests) on top of the fixture; owns CI marking that `tests/manual/` is excluded; may extend the fixture only through a PM-approved change.
- **PM/ADR:** decides the timestamp-label convention (below) so all five specs agree.

## Open questions
1. **ADR: bar timestamp convention.** This spec labels a bar by its *open* date at 00:00 UTC and exposes `bar_close_time()` for "known-at". Alternative is labelling by close time. Needs one cross-area decision (affects SPEC-002/003 indexing).
2. **Crypto exchange fallback:** is Coinbase Exchange acceptable, with SOL history possibly short (<5 years vs the STRATEGY.md gate)? Options: relax the crypto history gate for SOL, or drop SOL from the validation gate.
3. **Forward restatement policy:** chain-linking makes the forward series price-return for a dividend; is that acceptable, or should paper runs call `rebase` monthly (changes the fingerprint mid-run)? Recommend: accept chain-linking, rebase between experiments only.
4. **Who owns the CLI?** `python -m tradebot.data` is minimal; platform-lead may prefer a unified `tradebot` CLI wrapping it.
5. **Volume under splits** is left unadjusted; accept?

## Risks
- **yfinance is unofficial** and can break or get rate-limited without notice; mitigated by caching, backoff, the provider interface, and the offline fixture, but live refresh may need occasional fixes.
- **Adjusted data inherits Yahoo's errors** (bad dividend adjustments, 100x glitches); outlier checks flag but cannot prove correctness. `repair=False` is deliberate so we control changes.
- **Coinbase 1d bucket alignment and rate limit are undocumented**; T6 smoke test must confirm before crypto data is trusted. If buckets are not 00:00 UTC, switch exchange (Kraken fallback) or build daily bars from hourly.
- **Crypto exchange venue risk:** one exchange's prints are not "the" BTC price (small differences vs other venues); a switch of exchange means a new cache source and a change in backtest numbers.
- **Chain-linked drift** accumulates small basis error over many forward dividends; bounded by periodic rebase.
- **Hindsight universe** (fixed ETF list) overstates results; documented, with the `Universe` hook as the remedy.
- **Fixture staleness:** frozen snapshot diverges from live Yahoo basis over time; intentional, but don't compare fixture prices to live prices.
