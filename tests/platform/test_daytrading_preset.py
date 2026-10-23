"""The $10k day-trading preset, its CLI entry and the runner processing 15-minute bars (A21)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import Engine
from support.intraday_synth import make_intraday_bars

from tradebot import cli
from tradebot.config import Settings
from tradebot.data.types import Timeframe
from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.portfolio.models import EventType
from tradebot.runner import Runner, create_portfolio, daytrading_definition, restore_context
from tradebot.runner.bootstrap import benchmark_definition
from tradebot.runner.clock import due_bars_15m
from tradebot.runner.state import PortfolioDef, RunnerError
from tradebot.strategy.io import STRATEGIES_DIR

START = date(2024, 1, 8)
SYMS = ["AAA", "BBB", "CCC"]


@pytest.fixture
def cli_db() -> Iterator[Engine]:
    engine = make_engine(Settings().db_path)
    create_all(engine)
    yield engine
    engine.dispose()


def utc(d: int, hh: int, mm: int, month: int = 1) -> datetime:
    return datetime(2024, month, d, hh, mm, tzinfo=UTC)


# ------------------------------------------------------------------ the preset


def test_daytrading_definition_layout() -> None:
    d = daytrading_definition(START)
    cfg = d.engine_config()
    assert d.name == "day_trading" and d.kind == "fund" and d.benchmark == "bench_spy_15m"
    assert d.timeframe() == "15m" and d.markets() == ["equity"]
    assert cfg.initial_capital == Decimal("10000.00")
    assert {s.sleeve_id: s.target_weight for s in cfg.sleeves} == {
        "dip": Decimal("0.50"),
        "breakout": Decimal("0.50"),
    }
    assert cfg.intraday_flatten is True and cfg.settlement == "t1"
    assert cfg.equity_qty_quantum == Decimal("0.000001")  # fractional shares (A20)
    assert cfg.risk.kill_switch_drawdown == Decimal("0.20")  # the A18 kill switch ...
    assert cfg.risk.kill_switch_pause_bars == 20 * 26  # ... paused for 20 sessions, counted in bars
    assert [s.strategy.id for s in d.sleeves if s.strategy] == [
        "stocks_dt_dip",
        "stocks_dt_breakout",
    ]
    assert all(not s.vol_scaled for s in cfg.sleeves)
    assert all(s.trailing_stop_atr_mult == Decimal("2.0") for s in cfg.sleeves)
    assert (d.start - d.data_start).days == 30
    assert len(d.required_symbols()) >= 495


def test_definition_round_trips_through_its_stored_json() -> None:
    d = daytrading_definition(START)
    again = PortfolioDef.from_json(d.to_json())
    assert again.engine == d.engine and again.timeframe() == "15m"
    assert again.engine_config().intraday_flatten and again.engine_config().settlement == "t1"
    assert again.to_json() == d.to_json()


def test_missing_strategy_file_is_a_clear_error(tmp_path: Path) -> None:
    (tmp_path / "stocks_dt_dip.yaml").write_text(
        (STRATEGIES_DIR / "stocks_dt_dip.yaml").read_text()
    )
    with pytest.raises(RunnerError, match="stocks_dt_breakout.yaml"):
        daytrading_definition(START, strategies_dir=tmp_path)


def test_cli_creates_the_preset_and_the_15m_benchmark(cli_db: Engine, capsys: Any) -> None:
    assert cli.main(["portfolio", "create", "--preset", "daytrading", "--start", "2024-01-08"]) == 0
    assert "T+1" in capsys.readouterr().out
    assert (
        cli.main(["portfolio", "create", "--preset", "bench_spy_15m", "--start", "2024-01-08"]) == 0
    )
    with session_scope(cli_db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None and Decimal(p.initial_cash) == Decimal("10000.00")
        ctx = restore_context(s, p)
        assert ctx.engine.snapshot().equity == Decimal("10000.00")
        b = repo.get_portfolio(s, "bench_spy_15m")
        assert b is not None and PortfolioDef.from_json(b.config_json).timeframe() == "15m"
        cfg = PortfolioDef.from_json(b.config_json).engine_config()
        assert not cfg.intraday_flatten and cfg.settlement == "none"  # plain buy-and-hold
    assert cli.main(["portfolio", "create", "--preset", "daytrading", "--start", "2024-01-08"]) == 1


# ------------------------------------------------------------------ the 15m bar clock


def test_due_bars_are_the_completed_regular_session_bars() -> None:
    # A bar opened at t is due at t + 15 min + 1 min. Mon 2024-01-08: 09:30 ET = 14:30 UTC.
    assert due_bars_15m(start=START, after=None, now=utc(8, 14, 45)) == []
    got = due_bars_15m(start=START, after=None, now=utc(8, 14, 46))
    assert [t.isoformat() for t in got] == ["2024-01-08T14:30:00+00:00"]
    day = due_bars_15m(start=START, after=None, now=utc(8, 22, 0))
    assert len(day) == 26 and day[-1].isoformat() == "2024-01-08T20:45:00+00:00"
    # Resuming after a processed bar and across the weekend (Fri Jan 12 -> Tue Jan 16, MLK day).
    after = pd.Timestamp("2024-01-12T20:30:00Z")
    nxt = due_bars_15m(start=START, after=after, now=utc(16, 14, 47))
    assert [t.isoformat() for t in nxt] == [
        "2024-01-12T20:45:00+00:00",
        "2024-01-16T14:30:00+00:00",
    ]
    # Early close (Fri 2023-11-24, 13:00 ET): 14 bars, the last opens 17:45 UTC.
    early = due_bars_15m(
        start=date(2023, 11, 24), after=None, now=datetime(2023, 11, 24, 23, 0, tzinfo=UTC)
    )
    assert len(early) == 14 and early[-1].isoformat() == "2023-11-24T17:45:00+00:00"
    assert len(due_bars_15m(start=START, after=None, now=utc(12, 23, 0), limit=7)) == 7


# ------------------------------------------------------------------ the runner on 15m bars

ALWAYS_IN = """\
schema_version: 1
id: {sid}
name: always in (test)
kind: rules
asset_class: equity
timeframe: 15m
schedule: {{freq: bar}}
entry: {{always: true}}
position:
  weight: equal
  max_positions: 2
  rank: {{by: {{price: close}}, order: desc}}
universe: {universe}
"""


class FakeIntradayData:
    """``MarketData`` stand-in serving synthetic 15m bars (the real intraday source is separate)."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.calls: list[Timeframe] = []

    def get_bars_many(
        self, symbols: Sequence[str], start: date, end: date, **kw: Any
    ) -> dict[str, pd.DataFrame]:
        tf = kw.get("timeframe", Timeframe.D1)
        self.calls.append(tf)
        assert tf is Timeframe.M15
        lo, hi = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        return {
            s: self.frames[s].loc[(self.frames[s].index >= lo) & (self.frames[s].index < hi)]
            for s in symbols
        }


def _defn(tmp_path: Path):  # noqa: ANN202
    (tmp_path / "stocks_dt_dip.yaml").write_text(
        ALWAYS_IN.format(sid="stocks_dt_dip", universe="[AAA, BBB]")
    )
    (tmp_path / "stocks_dt_breakout.yaml").write_text(
        ALWAYS_IN.format(sid="stocks_dt_breakout", universe="[BBB, CCC]")
    )
    return daytrading_definition(START, strategies_dir=tmp_path, benchmark=None)


def _frames() -> dict[str, pd.DataFrame]:
    return make_intraday_bars(SYMS, date(2023, 12, 11), date(2024, 1, 12), seed=5, bar_vol=0.004)


def _runner(db: Engine, data: FakeIntradayData) -> Runner:
    from tradebot.clock import FixedClock

    return Runner(db, data, Settings(), FixedClock(utc(8, 12, 0)), refresh=False)  # type: ignore[arg-type]


def _fingerprint(db: Engine) -> dict[str, str]:
    from runner_support import db_fingerprint

    return db_fingerprint(db)


def test_runner_processes_completed_15m_bars_flattens_nightly_and_catches_up_identically(
    db: Engine, tmp_path: Path
) -> None:
    data = FakeIntradayData(_frames())
    with session_scope(db) as s:
        create_portfolio(s, _defn(tmp_path), utc(8, 12, 0))
    runner = _runner(db, data)

    first = runner.tick(utc(8, 21, 30))  # Monday evening: all 26 bars are complete
    assert [(t.status, len(t.processed)) for t in first] == [("ok", 26)]
    assert first[0].processed[0] == pd.Timestamp("2024-01-08T14:30:00Z")
    assert runner.tick(utc(8, 21, 31))[0].status == "noop"  # nothing new
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None
        ctx = restore_context(s, p)
        assert ctx.last_bar == datetime(2024, 1, 8, 20, 45)  # naive UTC bar open
        assert ctx.engine.snapshot().sleeves[0].positions == []  # flat at the close
        fills = [e for e in ctx.engine.ledger if e.type is EventType.FILL]
        assert fills and isinstance(fills[0].asof, datetime)
        ctx.engine.verify()

    # Mid-session: only bars that have completed (+1 min) are due.
    mid = runner.tick(utc(11, 15, 1))  # Tue + Wed bars, then Thu 09:30 .. 10:00 (bar 15:00 not yet)
    assert mid[0].status == "ok" and mid[0].processed[-1] == pd.Timestamp("2024-01-11T14:45:00Z")
    assert all(tf is Timeframe.M15 for tf in data.calls)
    mid_print = _fingerprint(db)

    # The same total, processed in one batch on a fresh database, gives the identical result.
    other = make_engine(tmp_path / "other.db")
    create_all(other)
    try:
        with session_scope(other) as s:
            create_portfolio(s, _defn(tmp_path), utc(8, 12, 0))
        batch = _runner(other, FakeIntradayData(_frames()))
        assert batch.tick(utc(11, 15, 1))[0].status == "ok"
        assert _fingerprint(other) == mid_print
    finally:
        other.dispose()


def test_runner_restart_resumes_an_intraday_portfolio(db: Engine, tmp_path: Path) -> None:
    data = FakeIntradayData(_frames())
    with session_scope(db) as s:
        create_portfolio(s, _defn(tmp_path), utc(8, 12, 0))
    _runner(db, data).tick(utc(9, 15, 1))
    before = _fingerprint(db)
    fresh = _runner(db, data)  # a new process: contexts are restored from the checkpoint
    assert fresh.tick(utc(9, 15, 1))[0].status == "noop"
    assert _fingerprint(db) == before
    out = fresh.tick(utc(9, 21, 30))
    assert out[0].status == "ok" and out[0].processed[-1] == pd.Timestamp("2024-01-09T20:45:00Z")
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None
        snap = restore_context(s, p).engine.snapshot()
    assert snap.sleeves[0].positions == [] and snap.sleeves[1].positions == []


def test_the_runner_refuses_a_gap_longer_than_the_catchup_cap(db: Engine, tmp_path: Path) -> None:
    with session_scope(db) as s:
        create_portfolio(s, _defn(tmp_path), utc(8, 12, 0))
    runner = Runner(
        db,
        FakeIntradayData(_frames()),
        Settings(max_catchup_bars=30),
        refresh=False,  # type: ignore[arg-type]
    )
    res = runner.tick(utc(10, 21, 30))
    assert res[0].status == "catchup_refused" and "MAX_CATCHUP_BARS" in (res[0].error or "")


def test_benchmark_definition_15m_is_buy_and_hold_on_the_same_clock() -> None:
    b = benchmark_definition("SPY", START, timeframe="15m")
    assert b.name == "bench_spy_15m" and b.timeframe() == "15m" and b.kind == "benchmark"
    assert (b.start - b.data_start).days == 30
