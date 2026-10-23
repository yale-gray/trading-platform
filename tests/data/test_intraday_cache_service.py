"""15m cache round-trip, intraday quality rules and the MarketData facade (offline)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tradebot.clock import FixedClock
from tradebot.data import BarCache, DataConfig, MarketData
from tradebot.data.errors import DataQualityError, StaleDataError
from tradebot.data.intraday import session_bar_opens
from tradebot.data.intraday_sync import SOURCE_15M
from tradebot.data.quality import validate_intraday_bars
from tradebot.data.types import BAR_COLUMNS, Symbol, Timeframe

NOW = datetime(2024, 6, 14, 22, 0, tzinfo=UTC)  # Friday, after the close
D1, D2 = date(2024, 6, 12), date(2024, 6, 13)


def frame_for(days: list[date], px: float = 100.0, drop: int | None = None) -> pd.DataFrame:
    idx = session_bar_opens(days[0])
    for d in days[1:]:
        idx = idx.append(session_bar_opens(d))
    n = len(idx)
    base = px + np.arange(n) * 0.01
    out = pd.DataFrame(
        {
            "open": base,
            "high": base + 0.5,
            "low": base - 0.5,
            "close": base + 0.1,
            "volume": np.full(n, 1000.0),
        },
        index=pd.DatetimeIndex(idx, name="ts"),
    )
    return out.drop(out.index[drop]) if drop is not None else out


class StubProvider:
    """History provider that serves a prepared frame, slicing by session date like Alpaca."""

    name = "alpaca_sip"

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.calls: list[tuple[list[str], date, date]] = []

    def supports(self, symbol: Symbol) -> bool:
        return "/" not in symbol

    def fetch_many(self, symbols, start, end, timeframe=Timeframe.M15):  # type: ignore[no-untyped-def]
        self.calls.append((list(symbols), start, end))
        lo = pd.Timestamp(start, tz="America/New_York")
        hi = pd.Timestamp(end, tz="America/New_York") + pd.Timedelta(days=1)
        return {
            s: self.frames[s].loc[(self.frames[s].index >= lo) & (self.frames[s].index < hi)]
            for s in symbols
        }

    def fetch_bars(self, symbol, start, end, timeframe=Timeframe.M15):  # type: ignore[no-untyped-def]
        return self.fetch_many([symbol], start, end)[symbol]


def make_md(
    tmp_path: Path, provider: Any, clock: FixedClock | None = None, **kw: Any
) -> MarketData:
    cfg = DataConfig(cache_dir=tmp_path, intraday_live_feed=None, **kw)
    return MarketData(cfg, providers=[], clock=clock or FixedClock(NOW), intraday_history=provider)


# ---- cache -------------------------------------------------------------------------
def test_cache_round_trip_15m_separate_path_and_timestamp_labels(tmp_path: Path) -> None:
    cache = BarCache(tmp_path, FixedClock(NOW))
    frame = frame_for([D1, D2])
    cache.create(SOURCE_15M, "AAPL", frame, "adj_close_as_of_2024-06-14")
    out = cache.read(SOURCE_15M, "AAPL")
    pd.testing.assert_frame_equal(out, frame, check_freq=False)
    sym_dir = tmp_path / "alpaca" / "15m" / "AAPL"
    assert (sym_dir / "meta.json").is_file()
    assert json.loads((sym_dir / "meta.json").read_text())["tf"] == "15m"
    info = cache.gen_info(SOURCE_15M, "AAPL")
    assert info["first"] == "2024-06-12T13:30:00Z" and info["last"] == "2024-06-13T19:45:00Z"
    # a daily source is untouched by the intraday layout
    cache.create(
        "yfinance",
        "AAPL",
        frame_for([D1])[:1].set_axis(
            pd.DatetimeIndex([pd.Timestamp("2024-06-12", tz="UTC")], name="ts")
        ),
        "adj_close_as_of_x",
    )
    assert cache.gen_info("yfinance", "AAPL")["first"] == "2024-06-12"
    assert json.loads((tmp_path / "yfinance" / "AAPL" / "meta.json").read_text())["tf"] == "1d"


def test_cache_append_and_restatement_work_for_15m(tmp_path: Path) -> None:
    cache = BarCache(tmp_path, FixedClock(NOW))
    cache.create(SOURCE_15M, "AAPL", frame_for([D1]), "adj_close_as_of_2024-06-14")
    both = frame_for([D1, D2])
    # pure restatement of the overlap (e.g. a dividend): constant ratio 0.99
    restated = both.copy()
    restated.loc[:, ["open", "high", "low", "close"]] *= 0.99
    result = cache.apply_fetch(SOURCE_15M, "AAPL", restated, overlap_bars=26)
    assert result.kind == "restatement"
    out = cache.read(SOURCE_15M, "AAPL")
    assert len(out) == 52
    new_part = out.loc[out.index >= pd.Timestamp("2024-06-13", tz="America/New_York")]
    # new rows were rescaled back onto the cached basis
    np.testing.assert_allclose(new_part["close"], both.loc[new_part.index, "close"], rtol=1e-9)


# ---- quality -----------------------------------------------------------------------
def test_missing_bars_are_warnings_not_failures() -> None:
    clean, report = validate_intraday_bars(frame_for([D1, D2], drop=10), "THIN", NOW)
    assert len(clean) == 51 and report.status == "ok"
    assert report.gaps == [D1] and any("missing 15m" in w for w in report.warnings)


def test_halt_in_middle_is_warning() -> None:
    f = frame_for([D1])
    f = f.drop(f.index[8:12])
    _, report = validate_intraday_bars(f, "HALT", NOW)
    assert report.gaps == [D1] and "4 missing" in report.warnings[0]


def test_extended_hours_and_incomplete_bars_dropped() -> None:
    f = frame_for([D1])
    extra = f.iloc[:1].copy()
    extra.index = pd.DatetimeIndex([pd.Timestamp("2024-06-12 12:00", tz="UTC")], name="ts")
    f = pd.concat([extra, f])
    mid_day = datetime(2024, 6, 12, 14, 0, tzinfo=UTC)  # 10:00 ET
    clean, report = validate_intraday_bars(f, "X", mid_day)
    assert len(clean) == 2  # 09:30 and 09:45 only
    assert any("outside regular-session" in w for w in report.warnings)
    assert any("in-progress" in w for w in report.warnings)


def test_structural_defects_still_fail_hard() -> None:
    f = frame_for([D1])
    f.iloc[3, f.columns.get_loc("low")] = f.iloc[3]["close"] + 5  # low above body
    with pytest.raises(DataQualityError, match="OHLC"):
        validate_intraday_bars(f, "BAD", NOW)
    g = frame_for([D1])
    g.iloc[0, g.columns.get_loc("close")] = -1.0
    with pytest.raises(DataQualityError, match="non-positive"):
        validate_intraday_bars(g, "BAD", NOW)


# ---- facade ------------------------------------------------------------------------
def test_marketdata_refresh_and_get_bars_15m(tmp_path: Path) -> None:
    frames = {"AAPL": frame_for([D1, D2, date(2024, 6, 14)]), "BRK.B": frame_for([D1, D2])}
    provider = StubProvider(frames)
    md = make_md(tmp_path, provider)
    reports = md.refresh(["AAPL", "BRK.B"], timeframe=Timeframe.M15, start=D1)
    assert {r.status for r in reports.values()} == {"ok"}
    assert reports[Symbol("AAPL")].n_bars == 78 and reports[Symbol("BRK.B")].n_bars == 52
    out = md.get_bars("AAPL", D2, D2, timeframe=Timeframe.M15, refresh=False)
    assert len(out) == 26 and list(out.columns) == list(BAR_COLUMNS)
    assert out.index[0] == pd.Timestamp("2024-06-13 13:30", tz="UTC")
    many = md.get_bars_many(["AAPL", "BRK.B"], D1, D2, timeframe=Timeframe.M15, refresh=False)
    assert len(many[Symbol("BRK.B")]) == 52
    # a second refresh with nothing new does not call the provider again
    n_calls = len(provider.calls)
    md.refresh(["AAPL"], timeframe=Timeframe.M15, start=D1)
    assert len(provider.calls) == n_calls


def test_incremental_tail_refresh_appends_only_new_bars(tmp_path: Path) -> None:
    frames = {"AAPL": frame_for([D1, D2, date(2024, 6, 14)])}
    clock = FixedClock(datetime(2024, 6, 13, 22, 0, tzinfo=UTC))
    md = make_md(tmp_path, StubProvider(frames), clock)
    md.refresh(["AAPL"], timeframe=Timeframe.M15, start=D1)
    assert (
        len(md.get_bars("AAPL", D1, date(2024, 6, 14), timeframe=Timeframe.M15, refresh=False))
        == 52
    )
    clock.set(NOW)
    md.refresh(["AAPL"], timeframe=Timeframe.M15, start=D1)
    out = md.get_bars("AAPL", D1, date(2024, 6, 14), timeframe=Timeframe.M15, refresh=False)
    assert len(out) == 78 and out.index.is_unique


def test_failed_symbol_is_isolated_and_reported(tmp_path: Path) -> None:
    provider = StubProvider({"AAPL": frame_for([D1, D2])})  # MSFT missing -> KeyError
    md = make_md(tmp_path, provider, intraday_chunk_symbols=1)
    md.refresh(["AAPL"], timeframe=Timeframe.M15, start=D1)
    assert md.verify(["AAPL"], timeframe=Timeframe.M15)[Symbol("AAPL")].status == "ok"


def test_offline_reads_cache_only_and_daily_path_unchanged(tmp_path: Path) -> None:
    md = make_md(tmp_path, StubProvider({"AAPL": frame_for([D1, D2])}))
    md.refresh(["AAPL"], timeframe=Timeframe.M15, start=D1)
    off = MarketData(
        DataConfig(cache_dir=tmp_path, offline=True), providers=[], clock=FixedClock(NOW)
    )
    out = off.get_bars("AAPL", D1, D2, timeframe=Timeframe.M15)
    assert len(out) == 52
    assert off.get_bars("MSFT", D1, D2, timeframe=Timeframe.M15).empty


def test_require_fresh_tolerates_few_missing_bars_but_flags_stale(tmp_path: Path) -> None:
    f = frame_for([D1, D2, date(2024, 6, 14)])
    thin = f.iloc[:-2]  # last two bars missing: thin name, still fresh
    md = make_md(tmp_path, StubProvider({"AAPL": thin}))
    md.refresh(["AAPL"], timeframe=Timeframe.M15, start=D1)
    md.get_bars(
        "AAPL", D1, date(2024, 6, 14), timeframe=Timeframe.M15, refresh=False, require_fresh=True
    )
    md2 = make_md(tmp_path / "b", StubProvider({"AAPL": frame_for([D1, D2])}))
    md2.refresh(["AAPL"], timeframe=Timeframe.M15, start=D1)
    with pytest.raises(StaleDataError):
        md2.get_bars(
            "AAPL",
            D1,
            date(2024, 6, 14),
            timeframe=Timeframe.M15,
            refresh=False,
            require_fresh=True,
        )


def test_live_tail_is_in_memory_only(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2024, 6, 14, 14, 20, tzinfo=UTC))  # 10:20 ET Friday
    full = frame_for([D1, D2, date(2024, 6, 14)])
    # SIP lags 16 min: at 14:20Z it can only serve bars completed by 14:04Z (open <= 13:45Z).
    hist = StubProvider(
        {"AAPL": full.loc[full.index <= pd.Timestamp("2024-06-14 13:45", tz="UTC")]}
    )
    live = StubProvider({"AAPL": frame_for([D1, D2, date(2024, 6, 14)], px=200.0)})
    cfg = DataConfig(cache_dir=tmp_path, intraday_live_feed="iex")
    md = MarketData(cfg, providers=[], clock=clock, intraday_history=hist, intraday_live=live)
    md.refresh(["AAPL"], timeframe=Timeframe.M15, start=D1)
    cached = BarCache(tmp_path, clock).read(SOURCE_15M, "AAPL")
    out = md.get_bars("AAPL", date(2024, 6, 14), date(2024, 6, 14), timeframe=Timeframe.M15)
    assert len(out) > 0 and out.attrs.get("live_tail_rows", 0) > 0
    assert out.index[-1] == pd.Timestamp("2024-06-14 14:00", tz="UTC")
    # nothing from the live feed reached the cache
    after = BarCache(tmp_path, clock).read(SOURCE_15M, "AAPL")
    pd.testing.assert_frame_equal(cached, after)
    assert after.index[-1] < out.index[-1]


def test_cli_parser_accepts_15m_refresh_flags() -> None:
    from tradebot.cli import build_parser

    args = build_parser().parse_args(
        ["data", "refresh", "--timeframe", "15m", "--symbols", "AAPL", "--start", "2016-01-01"]
    )
    assert args.timeframe == "15m" and args.start == date(2016, 1, 1)
    assert build_parser().parse_args(["data", "refresh"]).timeframe == "1d"
