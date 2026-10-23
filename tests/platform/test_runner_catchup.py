"""Catch-up, calendars, data gaps and runner == backtest (SPEC-004 AC 3, 5-8; SPEC-005 T19)."""

from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest
from runner_support import (
    DATA_START,
    START,
    LaggingData,
    after_close,
    counts,
    db_fingerprint,
    defs,
    make_data,
    make_runner,
    seed,
)
from sqlalchemy import Engine
from sqlmodel import col, select
from support.backtest_fixtures import load_fixture_bars

from tradebot.backtest.engine import BacktestConfig, run_backtest
from tradebot.data import MarketData
from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.db.models import EquitySnapshot, RunLog
from tradebot.runner import Runner, benchmark_definition
from tradebot.runner.state import PortfolioDef

pytestmark = pytest.mark.restart

FUND = "paper_fund"


def _world(
    tmp_path: Path, market_cache: Path, tag: str, definitions: list[PortfolioDef] | None = None
) -> tuple[Engine, MarketData, Path]:
    cache = tmp_path / f"cache_{tag}"
    shutil.copytree(market_cache, cache)
    path = tmp_path / f"{tag}.db"
    db = make_engine(path)
    create_all(db)
    seed(db, definitions if definitions is not None else defs())
    return db, make_data(cache), path


def _backtest_ledger_and_equity(defn: PortfolioDef, end: date):  # type: ignore[no-untyped-def]
    bars = load_fixture_bars(set(defn.required_symbols()))
    cfg = BacktestConfig(sleeves=list(defn.sleeves), start=defn.start, end=end, engine=defn.engine)
    return run_backtest(cfg, bars)


def _assert_equals_backtest(db: Engine, defn: PortfolioDef, end: date) -> None:
    res = _backtest_ledger_and_equity(defn, end)
    with session_scope(db) as s:
        p = repo.get_portfolio(s, defn.name)
        assert p is not None and p.id is not None
        assert repo.ledger_digest(s, p.id) == repo.events_digest(res.ledger)
        curve = repo.equity_curve(s, p.id)
    got = {ts.date(): float(eq) for ts, eq, _ in curve}
    want = {ts.date(): float(v) for ts, v in res.equity.items()}
    assert got == want  # exactly equal, not approximately


@pytest.mark.slow
def test_catch_up_after_five_missed_sessions_equals_daily_run_and_backtest(
    tmp_path: Path, market_cache: Path
) -> None:
    """AC 3: five sessions of downtime are replayed in order and match the batch backtest."""
    last_seen, back = date(2019, 1, 10), date(2019, 1, 17)  # sessions 11, 14, 15, 16, 17 missed
    db, data, _ = _world(tmp_path, market_cache, "gap")
    ref, rdata, _ = _world(tmp_path, market_cache, "gapref")
    try:
        runner = make_runner(db, data)
        runner.tick(after_close(last_seen))
        before = counts(db, "bench_spy")
        res = {r.portfolio: r for r in runner.tick(after_close(back))}
        spy = res["bench_spy"]
        assert spy.status == "ok"
        assert spy.processed == [date(2019, 1, d) for d in (11, 14, 15, 16, 17)]
        assert res[FUND].processed == [date(2019, 1, d) for d in range(11, 18)]  # crypto: 7 days
        assert counts(db, "bench_spy")["run_log"] == before["run_log"] + 5
        with session_scope(db) as s:
            p = repo.get_portfolio(s, "bench_spy")
            assert p is not None and p.id is not None
            logs = s.exec(
                select(RunLog).where(RunLog.portfolio_id == p.id).order_by(col(RunLog.bar_ts))
            ).all()
        flags = [(r.bar_ts.date(), json.loads(r.detail_json)["catchup"]) for r in logs]
        assert [
            d for d, c in flags if c and d > last_seen
        ] == spy.processed  # replayed bars are flagged

        daily = make_runner(ref, rdata)
        day = START
        while day <= back:
            daily.tick(after_close(day))
            day += timedelta(days=1)
        assert db_fingerprint(db) == db_fingerprint(ref)
        for definition in defs():
            _assert_equals_backtest(db, definition, back)
    finally:
        ref.dispose()
        db.dispose()


def test_gap_beyond_max_catchup_is_refused_without_state_change(
    tmp_path: Path, market_cache: Path
) -> None:
    """AC 6."""
    db, data, _ = _world(tmp_path, market_cache, "cap")
    try:
        runner = make_runner(db, data, max_catchup=3)
        runner.tick(after_close(date(2019, 1, 4)), portfolios=["bench_spy"])  # 3 sessions
        before = db_fingerprint(db)
        res = runner.tick(after_close(date(2019, 1, 10)), portfolios=["bench_spy"])  # 4 missed
        assert res[0].status == "catchup_refused" and "MAX_CATCHUP_BARS" in (res[0].error or "")
        assert db_fingerprint(db) == before
        with session_scope(db) as s:
            p = repo.get_portfolio(s, "bench_spy")
            assert p is not None and p.id is not None
            rs = repo.get_runner_state(s, p.id)
            assert rs.last_status == "error" and rs.last_processed_bar_ts is not None
            assert rs.last_processed_bar_ts.date() == date(2019, 1, 4)
        # within the cap it proceeds
        ok = runner.tick(after_close(date(2019, 1, 7)), portfolios=["bench_spy"])
        assert ok[0].status == "ok" and ok[0].processed == [date(2019, 1, 7)]
    finally:
        db.dispose()


def test_missing_bar_keeps_the_marker_and_the_next_tick_retries(
    tmp_path: Path, market_cache: Path
) -> None:
    """AC 7."""
    db, data, _ = _world(tmp_path, market_cache, "lag", defs(fund=False, benchmarks=("SPY",)))
    try:
        lagging = LaggingData(data, date(2019, 1, 7))
        runner = Runner(
            db, lagging, make_runner(db, data).settings, make_runner(db, data).clock, refresh=False
        )  # type: ignore[arg-type]
        res = runner.tick(after_close(date(2019, 1, 9)))
        assert res[0].status == "data_unavailable"
        assert res[0].processed == [
            date(2019, 1, 2),
            date(2019, 1, 3),
            date(2019, 1, 4),
            date(2019, 1, 7),
        ]
        with session_scope(db) as s:
            p = repo.get_portfolio(s, "bench_spy")
            assert p is not None and p.id is not None
            rs = repo.get_runner_state(s, p.id)
            assert rs.last_status == "data_unavailable"
            assert rs.last_processed_bar_ts is not None
            assert rs.last_processed_bar_ts.date() == date(2019, 1, 7)
        again = runner.tick(after_close(date(2019, 1, 9)))
        assert again[0].status == "data_unavailable" and again[0].processed == []
        lagging.cutoff = date(2019, 1, 31)  # the provider catches up
        done = runner.tick(after_close(date(2019, 1, 9)))
        assert done[0].status == "ok" and done[0].processed == [date(2019, 1, 8), date(2019, 1, 9)]
        with session_scope(db) as s:
            p = repo.get_portfolio(s, "bench_spy")
            assert p is not None and p.id is not None
            assert repo.get_runner_state(s, p.id).last_status == "ok"
    finally:
        db.dispose()


def test_equity_portfolio_skips_holidays_and_weekends_but_crypto_and_fund_do_not(
    tmp_path: Path, market_cache: Path
) -> None:
    """AC 5 on real fixture dates: Thanksgiving 2024-11-28, early-close Friday 2024-11-29."""
    start = date(2024, 11, 25)
    ds = [
        benchmark_definition("SPY", start, data_start=DATA_START),
        benchmark_definition("BTC/USD", start, data_start=DATA_START),
    ]
    db, data, _ = _world(tmp_path, market_cache, "cal", ds)
    try:
        runner = make_runner(db, data)
        res = {r.portfolio: r for r in runner.tick(after_close(date(2024, 12, 2)))}
        assert res["bench_spy"].processed == [
            date(2024, 11, 25),
            date(2024, 11, 26),
            date(2024, 11, 27),
            date(2024, 11, 29),
            date(2024, 12, 2),
        ]
        assert res["bench_btc"].processed == [start + timedelta(days=i) for i in range(8)]
    finally:
        db.dispose()


def test_mixed_fund_follows_the_union_clock_and_carries_benchmark_equity(
    tmp_path: Path, market_cache: Path
) -> None:
    """AC 5 (union clock) and AC 8 (benchmark equity on the fund curve)."""
    db, data, _ = _world(tmp_path, market_cache, "union")
    try:
        runner = make_runner(db, data)
        res = {r.portfolio: r for r in runner.tick(after_close(date(2019, 1, 8)))}
        assert date(2019, 1, 5) in res[FUND].processed  # Saturday: crypto-only bar
        assert date(2019, 1, 5) not in res["bench_spy"].processed
        assert date(2019, 1, 5) in res["bench_btc"].processed
        with session_scope(db) as s:
            fund = repo.get_portfolio(s, FUND)
            spy = repo.get_portfolio(s, "bench_spy")
            assert (
                fund is not None and fund.id is not None and spy is not None and spy.id is not None
            )
            curve = repo.equity_curve(s, fund.id)
            spy_curve = {ts: eq for ts, eq, _ in repo.equity_curve(s, spy.id)}
            rows = s.exec(
                select(EquitySnapshot).where(
                    EquitySnapshot.portfolio_id == fund.id, EquitySnapshot.sleeve_id == 0
                )
            ).all()
        assert len(curve) == len(rows) == 7
        for ts, _, bench in curve:
            if ts in spy_curve:  # on a session day the fund shows the SPY benchmark equity
                assert bench == spy_curve[ts]
            else:
                assert bench is None
    finally:
        db.dispose()


@pytest.mark.slow
def test_runner_equals_backtest_over_one_month_with_restarts(
    tmp_path: Path, market_cache: Path
) -> None:
    """Fund and both benchmarks, fed bar by bar with a new runner every week."""
    end = date(2019, 1, 31)
    db, data, path = _world(tmp_path, market_cache, "bt")
    db.dispose()
    day = START
    while day <= end:
        eng = make_engine(path)
        try:
            runner = make_runner(eng, data)
            for _ in range(7):
                if day > end:
                    break
                runner.tick(after_close(day))
                day += timedelta(days=1)
        finally:
            eng.dispose()
    eng = make_engine(path)
    try:
        for definition in defs():
            _assert_equals_backtest(eng, definition, end)
    finally:
        eng.dispose()


@pytest.mark.slow
@pytest.mark.timeout(120)
def test_runner_equals_backtest_over_a_year(tmp_path: Path, market_cache: Path) -> None:
    """The G4-shaped fund for 2019: monthly rebalances, stops, kill-switch path, restarts."""
    end = date(2019, 12, 31)
    db, data, path = _world(tmp_path, market_cache, "year")
    db.dispose()
    day = START
    while day <= end:
        eng = make_engine(path)
        try:
            runner = make_runner(eng, data)
            chunk_end = min(day + timedelta(days=40), end)
            runner.tick(after_close(chunk_end))  # ~40-bar catch-up per process "lifetime"
            day = chunk_end + timedelta(days=1)
        finally:
            eng.dispose()
    eng = make_engine(path)
    try:
        for definition in defs():
            _assert_equals_backtest(eng, definition, end)
        with session_scope(eng) as s:
            fund = repo.get_portfolio(s, FUND)
            assert fund is not None and fund.id is not None
            assert repo.ledger_count(s, fund.id) > 50  # it really traded
    finally:
        eng.dispose()
