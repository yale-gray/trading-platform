"""MarketData facade tests (SPEC-001 AC 2, 3, 6, 8, 11, 12, 13, 15).

Hand-worked numbers used below:
- Committed fixtures run 2018-01-02..2024-12-31 (manifest). SOL/USD starts 2020-04-10.
- 2024 NYSE sessions: 262 weekdays minus 10 exchange holidays = 252.
- 2024 crypto days: leap year = 366.
- SOL/USD 2020-04-10..2020-12-31 inclusive: 21 (April) + 245 (May-Dec) = 266.
- 2018-01-02..2024-12-31 inclusive every UTC day: 2557 days from 2018-01-01 to
  2025-01-01 minus the 2018-01-01 day = 2556.
- Clock 2025-01-01 12:00 UTC: equity bar 2024-12-31 closes 21:00 UTC and is final at
  22:00 UTC (60 min buffer); crypto bar 2024-12-31 closes 2025-01-01 00:00 UTC. Both
  are fresh, so the expected last bar is 2024-12-31 for both asset classes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradebot.clock import FixedClock
from tradebot.data import MarketData
from tradebot.data.cache import BarCache
from tradebot.data.calendar import sessions
from tradebot.data.config import DataConfig
from tradebot.data.errors import DataError, DataRevisionError, StaleDataError, UnknownSymbolError
from tradebot.data.providers.fixture_provider import FixtureProvider
from tradebot.data.quality import QualityReport
from tradebot.data.service import STATUS_QUARANTINED, RevisionQuarantinedError
from tradebot.data.symbols import asset_class
from tradebot.data.types import BAR_COLUMNS, AssetClass, Timeframe

UTC_NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
FIX_START = date(2018, 1, 2)
FIX_END = date(2024, 12, 31)
EQUITY = "SPY"
CRYPTO = "BTC/USD"
PRICE_COLS = ("open", "high", "low", "close")


# ---- helpers ----------------------------------------------------------------


def _clock(when: datetime = UTC_NOW) -> FixedClock:
    return FixedClock(when)


def _config(cache_dir: Path, *, offline: bool = False) -> DataConfig:
    return DataConfig(cache_dir=cache_dir, offline=offline)


def _ts(d: str) -> pd.Timestamp:
    return pd.Timestamp(d, tz="UTC")


class _Raising:
    """Provider that fails the test if the facade ever calls it."""

    def __init__(self, name: str, asset: AssetClass) -> None:
        self.name = name
        self._asset = asset

    def supports(self, symbol: str) -> bool:
        return asset_class(symbol) is self._asset

    def fetch_bars(
        self, symbol: str, start: date, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> pd.DataFrame:
        raise AssertionError(f"provider called for {symbol} {start}..{end}")


def _raising_providers() -> list[_Raising]:
    # Source names match the committed cache layout (yfinance/, coinbaseexchange/).
    return [
        _Raising("yfinance", AssetClass.EQUITY),
        _Raising("coinbaseexchange", AssetClass.CRYPTO),
    ]


class _Delegating:
    """Wraps a provider; subclasses alter what ``fetch_bars`` returns."""

    def __init__(self, inner: FixtureProvider) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    def supports(self, symbol: str) -> bool:
        return self._inner.supports(symbol)

    def fetch_bars(
        self, symbol: str, start: date, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> pd.DataFrame:
        return self._inner.fetch_bars(symbol, start, end, timeframe)


class _Scaled(_Delegating):
    """Fixture bars restated by a constant factor on OHLC (volume untouched)."""

    def __init__(self, inner: FixtureProvider, factor: float) -> None:
        super().__init__(inner)
        self._factor = factor

    def fetch_bars(
        self, symbol: str, start: date, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> pd.DataFrame:
        frame = super().fetch_bars(symbol, start, end, timeframe).copy()
        for col in PRICE_COLS:
            frame[col] = frame[col] * self._factor
        return frame


class _Revised(_Delegating):
    """Fixture bars with a non-constant per-row revision on OHLC (ratios vary)."""

    def fetch_bars(
        self, symbol: str, start: date, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> pd.DataFrame:
        frame = super().fetch_bars(symbol, start, end, timeframe).copy()
        factors = 1.0 + 0.01 * (np.arange(len(frame)) % 3)
        for col in PRICE_COLS:
            frame[col] = frame[col].to_numpy() * factors
        return frame


class _Empty(_Delegating):
    """Provider that can never supply the tail."""

    def fetch_bars(
        self, symbol: str, start: date, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> pd.DataFrame:
        return super().fetch_bars(symbol, start, end, timeframe).iloc[0:0]


class _Recording(_Delegating):
    """Records each fetched symbol; optionally supports only one asset class."""

    def __init__(self, inner: FixtureProvider, asset: AssetClass | None = None) -> None:
        super().__init__(inner)
        self._asset = asset
        self.calls: list[str] = []

    def supports(self, symbol: str) -> bool:
        if self._asset is not None and asset_class(symbol) is not self._asset:
            return False
        return super().supports(symbol)

    def fetch_bars(
        self, symbol: str, start: date, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> pd.DataFrame:
        self.calls.append(symbol)
        return super().fetch_bars(symbol, start, end, timeframe)


class _FakeEquityProvider:
    """A minimal protocol-conforming provider with four hand-written sessions."""

    name = "fake"

    def supports(self, symbol: str) -> bool:
        return asset_class(symbol) is AssetClass.EQUITY

    def fetch_bars(
        self, symbol: str, start: date, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> pd.DataFrame:
        idx = pd.DatetimeIndex(
            ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"], tz="UTC", name="ts"
        )
        close = np.array([100.0, 101.0, 102.0, 103.0])
        frame = pd.DataFrame(
            {
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.98,
                "close": close,
                "volume": np.full(4, 1000.0),
            },
            index=idx,
        )
        keep = (frame.index >= _ts(start.isoformat())) & (frame.index <= _ts(end.isoformat()))
        return frame.loc[keep]


def _seed_fixture_cache(
    cache_dir: Path, fixtures: FixtureProvider, symbol: str, last: date
) -> None:
    """Create generation 1 under source ``fixture`` holding fixture bars up to ``last``."""
    frame = fixtures.fetch_bars(symbol, FIX_START, last)
    BarCache(cache_dir, _clock()).create("fixture", symbol, frame, "synthetic", "synthetic-v1")


def _bars_file(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / "fixture" / symbol.replace("/", "_") / "gen-0001" / "bars.parquet"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- reads: shape, routing, slices -------------------------------------------


@pytest.mark.integration
def test_spy_full_range_has_manifest_rows_and_calendar_sessions(
    tmp_path: Path, market_cache: Path
) -> None:
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=_clock()
    )
    bars = md.get_bars(EQUITY, FIX_START, FIX_END)
    # Manifest yfinance/SPY: 1761 rows, 2018-01-02..2024-12-31; equities = XNYS sessions.
    assert len(bars) == 1761
    assert bars.index.equals(sessions(EQUITY, FIX_START, FIX_END))
    assert bars.index[0] == _ts("2018-01-02")
    assert bars.index[-1] == _ts("2024-12-31")


@pytest.mark.integration
def test_btc_full_range_has_every_utc_day(tmp_path: Path, market_cache: Path) -> None:
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=_clock()
    )
    bars = md.get_bars(CRYPTO, FIX_START, FIX_END)
    # Hand count: 2018-01-02..2024-12-31 inclusive = 2556 UTC days, no gaps.
    assert len(bars) == 2556
    assert bars.index.equals(pd.date_range("2018-01-02", "2024-12-31", freq="D", tz="UTC"))


@pytest.mark.unit
def test_get_bars_returns_canonical_frame_and_a_copy(tmp_path: Path, market_cache: Path) -> None:
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=_clock()
    )
    bars = md.get_bars(EQUITY, date(2024, 1, 1), date(2024, 12, 31))
    assert list(bars.columns) == list(BAR_COLUMNS)
    assert all(bars[c].dtype == np.float64 for c in BAR_COLUMNS)
    assert bars.index.name == "ts"
    assert str(bars.index.tz) == "UTC"
    assert bars.index.is_monotonic_increasing and bars.index.is_unique

    # Mutating the returned frame must never reach the cache.
    bars.iloc[:, 0] = -1.0
    again = md.get_bars(EQUITY, date(2024, 1, 1), date(2024, 12, 31))
    assert (again["open"] > 0).all()


@pytest.mark.unit
def test_sliced_range_returns_only_requested_sessions(tmp_path: Path, market_cache: Path) -> None:
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=_clock()
    )
    spy = md.get_bars(EQUITY, date(2024, 1, 1), date(2024, 12, 31))
    # 2024: 262 weekdays minus 10 NYSE holidays = 252; Jan 1 is a holiday so Jan 2 is first.
    assert len(spy) == 252
    assert spy.index[0] == _ts("2024-01-02")
    assert spy.index[-1] == _ts("2024-12-31")
    assert _ts("2024-07-04") not in spy.index

    btc = md.get_bars(CRYPTO, date(2024, 3, 1), date(2024, 3, 31))
    assert len(btc) == 31  # every UTC day of March 2024


@pytest.mark.unit
def test_sol_slice_starts_at_listing_date(tmp_path: Path, market_cache: Path) -> None:
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=_clock()
    )
    sol = md.get_bars("SOL/USD", date(2020, 1, 1), date(2020, 12, 31))
    # SOL/USD listing in fixtures: 2020-04-10. April 10..30 = 21 days, May..Dec = 245.
    assert len(sol) == 266
    assert sol.index[0] == _ts("2020-04-10")


@pytest.mark.unit
def test_get_bars_many_routes_each_symbol_to_its_asset_class_provider(
    tmp_path: Path, market_cache: Path
) -> None:
    fixtures = FixtureProvider(market_cache)
    eq = _Recording(fixtures, AssetClass.EQUITY)
    cr = _Recording(fixtures, AssetClass.CRYPTO)
    md = MarketData(_config(tmp_path / "cache"), providers=[eq, cr], clock=_clock())

    out = md.get_bars_many(["spy", "btc-usd"], date(2024, 1, 1), date(2024, 12, 31))

    assert list(out) == ["SPY", "BTC/USD"]
    assert len(out["SPY"]) == 252
    assert len(out["BTC/USD"]) == 366
    assert set(eq.calls) == {"SPY"}
    assert set(cr.calls) == {"BTC/USD"}


@pytest.mark.unit
def test_latest_bar_is_last_closed_bar_of_the_fixture(tmp_path: Path, market_cache: Path) -> None:
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=_clock()
    )
    truth = pd.read_parquet(market_cache / "yfinance" / "SPY" / "gen-0001" / "bars.parquet")
    row = md.latest_bar(EQUITY)
    assert row is not None
    assert row.name == _ts("2024-12-31")
    assert list(row.index) == list(BAR_COLUMNS)
    for col in BAR_COLUMNS:
        assert row[col] == truth[col].iloc[-1]

    btc = md.latest_bar(CRYPTO)
    assert btc is not None and btc.name == _ts("2024-12-31")


@pytest.mark.unit
def test_latest_bar_without_data_is_none_only_when_freshness_not_required(
    tmp_path: Path,
) -> None:
    md = MarketData(
        _config(tmp_path / "cache", offline=True), providers=_raising_providers(), clock=_clock()
    )
    assert md.latest_bar(EQUITY, require_fresh=False) is None
    with pytest.raises(StaleDataError):
        md.latest_bar(EQUITY)


@pytest.mark.unit
def test_latest_bar_require_fresh_raises_when_provider_cannot_supply_tail(
    tmp_path: Path, market_cache: Path
) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_fixture_cache(cache_dir, fixtures, EQUITY, date(2024, 12, 24))
    # Clock is 2025-01-05, so the expected bar is 2025-01-03; the provider returns nothing.
    md = MarketData(
        _config(cache_dir),
        providers=[_Empty(fixtures)],
        clock=_clock(datetime(2025, 1, 5, 12, 0, tzinfo=UTC)),
    )
    with pytest.raises(StaleDataError):
        md.latest_bar(EQUITY)


# ---- offline and provider contracts -------------------------------------------


@pytest.mark.unit
def test_offline_mode_never_calls_a_provider(tmp_path: Path, market_cache: Path) -> None:
    md = MarketData(
        _config(market_cache, offline=True), providers=_raising_providers(), clock=_clock()
    )
    assert md.offline is True
    spy = md.get_bars(EQUITY, date(2024, 1, 1), date(2024, 12, 31), refresh=True)
    btc = md.get_bars(CRYPTO, date(2024, 1, 1), date(2024, 12, 31), refresh=True)
    assert len(spy) == 252
    assert len(btc) == 366
    assert md.latest_bar(EQUITY) is not None


@pytest.mark.integration
def test_offline_get_bars_matches_manifest_for_all_twenty_fixture_symbols(
    market_cache: Path,
) -> None:
    manifest = json.loads((market_cache / "manifest.json").read_text(encoding="utf-8"))
    expected: dict[str, tuple[int, str]] = {}
    for rel, meta in manifest["files"].items():
        if rel.endswith("/gen-0001/bars.parquet"):
            _, fs_symbol, *_ = rel.split("/")
            expected[fs_symbol.replace("_", "/")] = (meta["rows"], meta["first"])
    assert len(expected) == 20

    md = MarketData(
        _config(market_cache, offline=True), providers=_raising_providers(), clock=_clock()
    )
    for symbol, (rows, first) in expected.items():
        bars = md.get_bars(symbol, FIX_START, FIX_END)
        assert len(bars) == rows, symbol
        assert bars.index[0].date().isoformat() == first, symbol
        if asset_class(symbol) is AssetClass.EQUITY:
            assert len(bars) == len(sessions(symbol, FIX_START, FIX_END)), symbol


@pytest.mark.unit
def test_providers_are_swappable_for_a_protocol_conforming_fake(tmp_path: Path) -> None:
    md = MarketData(_config(tmp_path / "cache"), providers=[_FakeEquityProvider()], clock=_clock())
    bars = md.get_bars(EQUITY, date(2024, 1, 2), date(2024, 1, 5))
    assert list(bars["close"]) == [100.0, 101.0, 102.0, 103.0]
    with pytest.raises(UnknownSymbolError):
        md.get_bars(CRYPTO, date(2024, 1, 2), date(2024, 1, 5))


# ---- freshness and the in-progress bar ------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("symbol", [EQUITY, CRYPTO])
def test_require_fresh_passes_at_2025_01_01(
    tmp_path: Path, market_cache: Path, symbol: str
) -> None:
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=_clock()
    )
    bars = md.get_bars(symbol, date(2024, 12, 1), FIX_END, require_fresh=True)
    assert bars.index[-1] == _ts("2024-12-31")


@pytest.mark.unit
@pytest.mark.parametrize("symbol", [EQUITY, CRYPTO])
def test_require_fresh_raises_when_clock_moves_past_the_data(
    tmp_path: Path, market_cache: Path, symbol: str
) -> None:
    clock = _clock()
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=clock
    )
    assert len(md.get_bars(symbol, date(2024, 12, 1), FIX_END)) > 0

    # Fixtures end 2024-12-31 and the provider cannot supply more, so 2025-01-05 is stale.
    clock.set(datetime(2025, 1, 5, 12, 0, tzinfo=UTC))
    with pytest.raises(StaleDataError):
        md.get_bars(symbol, date(2024, 12, 1), FIX_END, require_fresh=True)
    # Without require_fresh the same read succeeds and still ends at the last fixture bar.
    bars = md.get_bars(symbol, date(2024, 12, 1), FIX_END)
    assert bars.index[-1] == _ts("2024-12-31")


@pytest.mark.unit
def test_in_progress_bar_is_excluded_until_it_closes(tmp_path: Path, market_cache: Path) -> None:
    clock = _clock(datetime(2024, 12, 31, 20, 0, tzinfo=UTC))  # 15:00 ET on 2024-12-31
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=clock
    )
    eq = md.get_bars(EQUITY, date(2024, 12, 30), date(2024, 12, 31))
    assert list(eq.index.date) == [date(2024, 12, 30)]  # 12-31 closes 21:00 UTC, final 22:00
    btc = md.get_bars(CRYPTO, date(2024, 12, 30), date(2024, 12, 31))
    assert list(btc.index.date) == [date(2024, 12, 30)]  # today's UTC date is absent

    clock.set(datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC))
    btc = md.get_bars(CRYPTO, date(2024, 12, 30), date(2024, 12, 31))
    assert list(btc.index.date) == [date(2024, 12, 30), date(2024, 12, 31)]
    eq = md.get_bars(EQUITY, date(2024, 12, 30), date(2024, 12, 31))
    assert list(eq.index.date) == [date(2024, 12, 30), date(2024, 12, 31)]


# ---- restatement, revision and generations ----------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(("symbol", "n_new"), [(EQUITY, 4), (CRYPTO, 7)])
def test_restated_tail_is_chain_linked_and_old_rows_are_unchanged(
    tmp_path: Path, market_cache: Path, symbol: str, n_new: int
) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_fixture_cache(cache_dir, fixtures, symbol, date(2024, 12, 24))
    cache = BarCache(cache_dir, _clock())
    before = cache.read("fixture", symbol)

    # Provider restates everything by x2, so the overlap ratio is a constant 0.5.
    md = MarketData(_config(cache_dir), providers=[_Scaled(fixtures, 2.0)], clock=_clock())
    bars = md.get_bars(symbol, FIX_START, FIX_END)

    after = cache.read("fixture", symbol)
    old = after.loc[after.index <= _ts("2024-12-24")]
    pd.testing.assert_frame_equal(old, before, check_freq=False)

    new = after.loc[after.index > _ts("2024-12-24")]
    assert len(new) == n_new
    # New rows come back on the cached basis: identical to the unscaled fixture values.
    pd.testing.assert_frame_equal(
        new, fixtures.fetch_bars(symbol, date(2024, 12, 25), FIX_END), check_freq=False
    )
    assert bars.index[-1] == _ts("2024-12-31")

    events = cache.gen_info("fixture", symbol)["events"]
    assert len(events) == 1
    assert events[0]["ratio"] == pytest.approx(0.5)


@pytest.mark.unit
def test_revised_tail_raises_and_appends_nothing(tmp_path: Path, market_cache: Path) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_fixture_cache(cache_dir, fixtures, EQUITY, date(2024, 12, 24))
    cache = BarCache(cache_dir, _clock())
    before = cache.read("fixture", EQUITY)
    before_sha = _sha(_bars_file(cache_dir, EQUITY))

    md = MarketData(_config(cache_dir), providers=[_Revised(fixtures)], clock=_clock())
    with pytest.raises(DataRevisionError):
        md.get_bars(EQUITY, FIX_START, FIX_END)

    pd.testing.assert_frame_equal(cache.read("fixture", EQUITY), before, check_freq=False)
    assert cache.gen_info("fixture", EQUITY)["last"] == "2024-12-24"
    assert _sha(_bars_file(cache_dir, EQUITY)) == before_sha


@pytest.mark.unit
def test_rebase_creates_generation_two_and_generation_one_still_reads_original(
    tmp_path: Path, market_cache: Path
) -> None:
    cache_dir = tmp_path / "cache"
    md = MarketData(_config(cache_dir), providers=[FixtureProvider(market_cache)], clock=_clock())
    original = md.get_bars(EQUITY, FIX_START, FIX_END)

    assert md.rebase(EQUITY) == 2

    gen1 = md.get_bars(EQUITY, FIX_START, FIX_END, generation=1)
    assert len(gen1) == 1761
    pd.testing.assert_frame_equal(gen1, original, check_freq=False)

    cache = BarCache(cache_dir, _clock())
    assert cache.generations("fixture", EQUITY) == [1, 2]
    assert cache.current_gen("fixture", EQUITY) == 2


@pytest.mark.unit
def test_rebase_is_refused_offline(tmp_path: Path, market_cache: Path) -> None:
    md = MarketData(
        _config(market_cache, offline=True), providers=_raising_providers(), clock=_clock()
    )
    with pytest.raises(DataError):
        md.rebase(EQUITY)


@pytest.mark.unit
def test_fingerprint_is_stable_and_differs_by_generation(
    tmp_path: Path, market_cache: Path
) -> None:
    cache_dir = tmp_path / "cache"
    provider = FixtureProvider(market_cache)
    md = MarketData(_config(cache_dir), providers=[provider], clock=_clock())
    md.get_bars(EQUITY, FIX_START, FIX_END)

    fp_gen1 = md.data_fingerprint([EQUITY], FIX_START, FIX_END)
    assert md.data_fingerprint([EQUITY], FIX_START, FIX_END) == fp_gen1
    other_instance = MarketData(_config(cache_dir), providers=[provider], clock=_clock())
    assert other_instance.data_fingerprint([EQUITY], FIX_START, FIX_END) == fp_gen1
    assert md.data_fingerprint([EQUITY], FIX_START, FIX_END, generation=1) == fp_gen1

    md.rebase(EQUITY)
    assert md.data_fingerprint([EQUITY], FIX_START, FIX_END, generation=1) == fp_gen1
    fp_gen2 = md.data_fingerprint([EQUITY], FIX_START, FIX_END, generation=2)
    assert fp_gen2 != fp_gen1
    assert md.data_fingerprint([EQUITY], FIX_START, FIX_END) == fp_gen2


# ---- verify, maintenance and package surface -----------------------------------


@pytest.mark.unit
def test_verify_reports_cached_symbols_without_calling_providers(
    tmp_path: Path, market_cache: Path
) -> None:
    md = MarketData(
        _config(market_cache, offline=True), providers=_raising_providers(), clock=_clock()
    )
    reports = md.verify(["SPY", "btc-usd"])
    assert set(reports) == {"SPY", "BTC/USD"}
    for symbol, report in reports.items():
        assert isinstance(report, QualityReport)
        assert report.symbol == symbol
        assert not any(w.startswith("quality error") for w in report.warnings)
    assert reports["SPY"].n_bars == 1761
    assert reports["BTC/USD"].n_bars == 2556


@pytest.mark.unit
def test_verify_reports_missing_cache_as_a_warning(tmp_path: Path, market_cache: Path) -> None:
    md = MarketData(
        _config(tmp_path / "cache"), providers=[FixtureProvider(market_cache)], clock=_clock()
    )
    report = md.verify([EQUITY])[EQUITY]
    assert report.n_bars == 0
    assert report.warnings == ["no cached data"]


@pytest.mark.unit
def test_importing_the_package_does_not_load_yfinance_or_ccxt() -> None:
    code = (
        "import json, sys; import tradebot.data; "
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.split('.')[0] in {'yfinance', 'ccxt'})))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


@pytest.mark.unit
def test_package_exposes_plain_functions_and_no_cli() -> None:
    import tradebot.data as data

    for name in ("refresh", "verify", "rebase"):
        assert callable(getattr(data, name))
    assert importlib.util.find_spec("tradebot.data.__main__") is None


# ---- audit fixes: cutoff, head prepend, quarantine memo, isolation -------------------


def _seed_range(cache_dir: Path, fixtures: FixtureProvider, symbol: str, a: date, b: date) -> None:
    frame = fixtures.fetch_bars(symbol, a, b)
    BarCache(cache_dir, _clock()).create("fixture", symbol, frame, "synthetic", "synthetic-v1")


class _Counting(_Delegating):
    """Counts fetch calls; ``revise`` names symbols whose bars get a varying revision."""

    def __init__(self, inner: FixtureProvider, revise: tuple[str, ...] = ()) -> None:
        super().__init__(inner)
        self._revise = revise
        self.calls = 0

    def fetch_bars(
        self, symbol: str, start: date, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> pd.DataFrame:
        self.calls += 1
        frame = super().fetch_bars(symbol, start, end, timeframe).copy()
        if symbol in self._revise:
            factors = 1.0 + 0.01 * (np.arange(len(frame)) % 3)
            for col in PRICE_COLS:
                frame[col] = frame[col].to_numpy() * factors
        return frame


@pytest.mark.unit
@pytest.mark.parametrize("symbol", [EQUITY, CRYPTO])
def test_read_cutoff_hides_cached_bars_newer_than_the_clock(
    tmp_path: Path, market_cache: Path, symbol: str
) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_fixture_cache(cache_dir, fixtures, symbol, FIX_END)
    # Saturday 2024-12-21: last closed bar is Friday 12-20 for both asset classes.
    clock = _clock(datetime(2024, 12, 21, 12, 0, tzinfo=UTC))
    md = MarketData(_config(cache_dir, offline=True), providers=[fixtures], clock=clock)
    bars = md.get_bars(symbol, date(2024, 12, 1), FIX_END)
    assert bars.index[-1] == _ts("2024-12-20")
    latest = md.latest_bar(symbol)
    assert latest is not None
    assert latest.name == _ts("2024-12-20")


@pytest.mark.unit
@pytest.mark.parametrize("symbol", [EQUITY, CRYPTO])
def test_head_prepend_adds_older_rows_and_leaves_cached_rows(
    tmp_path: Path, market_cache: Path, symbol: str
) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_range(cache_dir, fixtures, symbol, date(2024, 6, 3), FIX_END)
    cache = BarCache(cache_dir, _clock())
    before = cache.read("fixture", symbol)

    md = MarketData(_config(cache_dir), providers=[fixtures], clock=_clock())
    md.get_bars(symbol, date(2024, 5, 1), FIX_END)

    after = cache.read("fixture", symbol)
    expected_head = fixtures.fetch_bars(symbol, date(2024, 5, 1), date(2024, 6, 2))
    assert after.index[0] == expected_head.index[0]
    assert len(after) == len(before) + len(expected_head)
    pd.testing.assert_frame_equal(
        after.loc[after.index >= before.index[0]], before, check_freq=False
    )
    pd.testing.assert_frame_equal(
        after.loc[after.index < before.index[0]], expected_head, check_freq=False
    )


@pytest.mark.unit
def test_head_prepend_rescales_on_constant_ratio_overlap(
    tmp_path: Path, market_cache: Path
) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_range(cache_dir, fixtures, EQUITY, date(2024, 6, 3), FIX_END)
    cache = BarCache(cache_dir, _clock())
    first_cached = cache.read("fixture", EQUITY).index[0]

    # Provider now reports everything x2; overlap ratio cached/fetched is a constant 0.5.
    md = MarketData(_config(cache_dir), providers=[_Scaled(fixtures, 2.0)], clock=_clock())
    md.get_bars(EQUITY, date(2024, 5, 1), FIX_END)

    after = cache.read("fixture", EQUITY)
    head = after.loc[after.index < first_cached]
    assert not head.empty
    # Rescaled back onto the cached basis: equal to the unscaled fixture values.
    pd.testing.assert_frame_equal(
        head, fixtures.fetch_bars(EQUITY, date(2024, 5, 1), date(2024, 6, 2)), check_freq=False
    )


@pytest.mark.unit
def test_head_prepend_with_varying_ratio_quarantines_and_is_not_retried(
    tmp_path: Path, market_cache: Path
) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_range(cache_dir, fixtures, EQUITY, date(2024, 6, 3), FIX_END)
    cache = BarCache(cache_dir, _clock())
    before = cache.read("fixture", EQUITY)
    provider = _Counting(fixtures, revise=(EQUITY,))
    md = MarketData(_config(cache_dir), providers=[provider], clock=_clock())

    with pytest.raises(DataRevisionError):
        md.get_bars(EQUITY, date(2024, 5, 1), FIX_END)
    pd.testing.assert_frame_equal(cache.read("fixture", EQUITY), before, check_freq=False)
    assert list((cache_dir / "fixture" / EQUITY / "quarantine").glob("*.parquet"))

    calls = provider.calls
    md.get_bars(EQUITY, date(2024, 5, 1), FIX_END)  # second call: no new request
    assert provider.calls == calls


@pytest.mark.unit
def test_quarantined_revision_is_loud_memoised_and_cleared_by_rebase(
    tmp_path: Path, market_cache: Path
) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_fixture_cache(cache_dir, fixtures, EQUITY, date(2024, 12, 24))
    provider = _Counting(fixtures, revise=(EQUITY,))
    md = MarketData(_config(cache_dir), providers=[provider], clock=_clock())

    with pytest.raises(RevisionQuarantinedError) as excinfo:
        md.get_bars(EQUITY, FIX_START, FIX_END)
    assert isinstance(excinfo.value, DataRevisionError)
    assert excinfo.value.since == date(2025, 1, 1)
    calls = provider.calls

    with pytest.raises(RevisionQuarantinedError):
        md.latest_bar(EQUITY)
    with pytest.raises(RevisionQuarantinedError):
        md.get_bars(EQUITY, FIX_START, FIX_END)
    report = md.refresh([EQUITY])[EQUITY]
    assert report.status == STATUS_QUARANTINED
    assert report.warnings == ["revision_quarantined: needs rebase since 2025-01-01"]
    assert report.n_bars > 0
    assert provider.calls == calls  # memoised: no re-fetch

    md.rebase(EQUITY)  # human decision; clears the memo
    assert md.refresh([EQUITY])[EQUITY].status == "ok"


@pytest.mark.unit
def test_one_failing_symbol_does_not_block_the_others(tmp_path: Path, market_cache: Path) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    for sym in ("SPY", "QQQ"):
        _seed_fixture_cache(cache_dir, fixtures, sym, date(2024, 12, 24))
    md = MarketData(
        _config(cache_dir), providers=[_Counting(fixtures, revise=("SPY",))], clock=_clock()
    )
    reports = md.refresh(["SPY", "QQQ"])
    assert reports["SPY"].status == STATUS_QUARANTINED
    assert reports["QQQ"].status == "ok"
    assert BarCache(cache_dir, _clock()).gen_info("fixture", "QQQ")["last"] == "2024-12-31"

    md2 = MarketData(
        _config(cache_dir), providers=[_Counting(fixtures, revise=("SPY",))], clock=_clock()
    )
    with pytest.raises(DataRevisionError):
        md2.get_bars_many(["SPY", "QQQ"], FIX_START, FIX_END)


@pytest.mark.unit
def test_verify_and_offline_refresh_report_corruption_per_symbol(
    tmp_path: Path, market_cache: Path
) -> None:
    cache = BarCache(market_cache, _clock())
    gen_dir = market_cache / "yfinance" / "SPY" / "gen-0001"
    bars = gen_dir / "bars.parquet"
    assert bars.exists()
    data = bytearray(bars.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bars.write_bytes(bytes(data))
    md = MarketData(
        _config(market_cache, offline=True), providers=_raising_providers(), clock=_clock()
    )
    for reports in (md.verify(["SPY", "QQQ"]), md.refresh(["SPY", "QQQ"])):
        assert reports["SPY"].status == "corrupt"
        assert reports["SPY"].warnings[0].startswith("corrupt:")
        assert reports["QQQ"].status == "ok"
        assert reports["QQQ"].n_bars > 0
    assert cache.exists("yfinance", "QQQ")


@pytest.mark.unit
def test_empty_provider_result_is_a_warning_in_the_report(
    tmp_path: Path, market_cache: Path
) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_fixture_cache(cache_dir, fixtures, EQUITY, date(2024, 12, 24))
    md = MarketData(_config(cache_dir), providers=[_Empty(fixtures)], clock=_clock())
    report = md.refresh([EQUITY])[EQUITY]
    assert any("provider returned no rows" in w for w in report.warnings)


class _Overshoot(_Delegating):
    """Ignores ``end`` and returns fixture bars through the end of the fixtures."""

    def fetch_bars(
        self, symbol: str, start: date, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> pd.DataFrame:
        return super().fetch_bars(symbol, start, FIX_END, timeframe)


@pytest.mark.unit
@pytest.mark.parametrize(("buffer_min", "last"), [(60, "2024-12-31"), (120, "2024-12-30")])
def test_equity_close_buffer_from_config_reaches_validation(
    tmp_path: Path, market_cache: Path, buffer_min: int, last: str
) -> None:
    cache_dir = tmp_path / "cache"
    fixtures = FixtureProvider(market_cache)
    _seed_fixture_cache(cache_dir, fixtures, EQUITY, date(2024, 12, 24))
    # 12-31 closes 21:00 UTC; at 22:30 it is final with a 60 min buffer, not with 120.
    cfg = DataConfig(cache_dir=cache_dir, equity_close_buffer_min=buffer_min)
    clock = _clock(datetime(2024, 12, 31, 22, 30, tzinfo=UTC))
    md = MarketData(cfg, providers=[_Overshoot(fixtures)], clock=clock)
    md.refresh([EQUITY])
    assert BarCache(cache_dir, _clock()).gen_info("fixture", EQUITY)["last"] == last


@pytest.mark.unit
def test_basis_is_spot_exchange_for_crypto_and_adjusted_for_equity(
    tmp_path: Path, market_cache: Path
) -> None:
    cache_dir = tmp_path / "cache"
    md = MarketData(_config(cache_dir), providers=[FixtureProvider(market_cache)], clock=_clock())
    md.get_bars(CRYPTO, date(2024, 12, 1), FIX_END)
    md.get_bars(EQUITY, date(2024, 12, 1), FIX_END)
    cache = BarCache(cache_dir, _clock())
    assert cache.gen_info("fixture", CRYPTO)["basis"] == "spot_coinbaseexchange"
    assert cache.gen_info("fixture", EQUITY)["basis"] == "adj_close_as_of_2025-01-01"
