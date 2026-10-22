"""Runner restart, crash and idempotency tests (SPEC-004 AC 1, 2, 4, 14; ADR-0001 A3/A4).

All use a file-based SQLite DB in ``tmp_path``, a fresh engine and ``Runner`` for every
"restart", a ``FixedClock`` and the offline synthetic fixtures.
"""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from runner_support import (
    START,
    SimulatedCrash,
    after_close,
    counts,
    crash_at,
    db_fingerprint,
    defs,
    make_data,
    make_runner,
    seed,
)
from sqlalchemy import Engine
from sqlmodel import col, select

from tradebot.data import MarketData
from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.db.models import LedgerEventRow, OrderRow
from tradebot.portfolio.engine import StateCorruptionError
from tradebot.runner import Runner, RunnerError, restore_context
from tradebot.runner.state import FAULT_POINTS

pytestmark = pytest.mark.restart

D1 = date(2019, 1, 2)  # first bar: orders are created, nothing fills yet
D2 = date(2019, 1, 12)
FUND = "paper_fund"
BENCHES = ["bench_spy", "bench_btc"]


def _world(tmp_path: Path, market_cache: Path, tag: str) -> tuple[Engine, MarketData, Path]:
    cache = tmp_path / f"cache_{tag}"
    shutil.copytree(market_cache, cache)
    path = tmp_path / f"{tag}.db"
    db = make_engine(path)
    create_all(db)
    seed(db, defs())
    return db, make_data(cache), path


def _phases(runner: Runner) -> None:
    """Benchmarks and the fund's first bar clean, then the rest of the benchmarks."""
    runner.tick(after_close(D1))
    runner.tick(after_close(D2), portfolios=BENCHES)


@pytest.fixture
def reference(tmp_path: Path, market_cache: Path) -> dict[str, str]:
    """Fingerprint of an uninterrupted run to ``D2``."""
    db, data, _ = _world(tmp_path, market_cache, "ref")
    try:
        runner = make_runner(db, data)
        _phases(runner)
        runner.tick(after_close(D2), portfolios=[FUND])
        return db_fingerprint(db)
    finally:
        db.dispose()


def test_double_tick_creates_nothing_new(tmp_path: Path, market_cache: Path) -> None:
    """AC 2: a second tick for the same time adds no orders, fills or snapshots."""
    db, data, _ = _world(tmp_path, market_cache, "a")
    try:
        runner = make_runner(db, data)
        first = runner.tick(after_close(D2))
        assert all(r.status == "ok" and r.processed for r in first)
        before = db_fingerprint(db)
        again = runner.tick(after_close(D2))
        assert [r.status for r in again] == ["noop"] * 3
        assert db_fingerprint(db) == before
        # a brand-new runner (restart) is just as idle
        db.dispose()
        db2 = make_engine(tmp_path / "a.db")
        try:
            assert db_fingerprint(db2) == before
            assert [r.status for r in make_runner(db2, data).tick(after_close(D2))] == ["noop"] * 3
            assert db_fingerprint(db2) == before
        finally:
            db2.dispose()
    finally:
        db.dispose()


def test_each_bar_writes_exactly_one_checkpoint_run_log_and_marker(
    tmp_path: Path, market_cache: Path
) -> None:
    """AC 14."""
    db, data, _ = _world(tmp_path, market_cache, "b")
    try:
        runner = make_runner(db, data)
        runner.tick(after_close(D1))
        prev = counts(db, FUND)
        for day in range(3, 9):
            runner.tick(after_close(date(2019, 1, day)))
            now = counts(db, FUND)
            assert now["checkpoints"] == prev["checkpoints"] + 1
            assert now["run_log"] == prev["run_log"] + 1
            assert now["equity"] == prev["equity"] + 5  # 4 sleeves + the total row
            assert now["marker"] == 1
            prev = now
        with session_scope(db) as s:
            p = repo.get_portfolio(s, FUND)
            assert p is not None and p.id is not None
            cp = repo.latest_checkpoint(s, p.id)
            assert cp is not None
            assert cp.ledger_seq == repo.ledger_count(s, p.id)
            assert cp.target_book_json  # ADR A1: the book rides in the checkpoint
    finally:
        db.dispose()


@pytest.mark.parametrize("on_call", [1, 3])
@pytest.mark.parametrize("point", FAULT_POINTS)
def test_crash_at_each_fault_point_then_restart(
    point: str, on_call: int, tmp_path: Path, market_cache: Path, reference: dict[str, str]
) -> None:
    """AC 1: a crash at any fault point, then a restart, equals the uninterrupted run."""
    db, data, path = _world(tmp_path, market_cache, "crash")
    try:
        runner = make_runner(db, data)
        _phases(runner)
        c0 = counts(db, FUND)
        runner.fault_hook = crash_at(point, on_call)
        with pytest.raises(SimulatedCrash):
            runner.tick(after_close(D2), portfolios=[FUND])
        committed = on_call if point == "after_commit" else on_call - 1
        c1 = counts(db, FUND)
        # atomicity: either the whole bar is present or none of it
        assert c1["checkpoints"] == c0["checkpoints"] + committed
        assert c1["run_log"] == c0["run_log"] + committed
        assert c1["equity"] == c0["equity"] + 5 * committed
        with session_scope(db) as s:
            p = repo.get_portfolio(s, FUND)
            assert p is not None and p.id is not None
            cp = repo.latest_checkpoint(s, p.id)
            assert cp is not None
            assert cp.ledger_seq == repo.ledger_count(s, p.id)
            assert s.exec(
                select(LedgerEventRow.seq).where(LedgerEventRow.portfolio_id == p.id)
            ).all() == list(range(1, cp.ledger_seq + 1))
        db.dispose()

        db2 = make_engine(path)  # restart: new engine, new runner, nothing cached
        try:
            restarted = make_runner(db2, data)
            res = restarted.tick(after_close(D2), portfolios=[FUND])
            assert res[0].status in ("ok", "noop")
            assert db_fingerprint(db2) == reference
            assert restarted.tick(after_close(D2), portfolios=[FUND])[0].status == "noop"
        finally:
            db2.dispose()
    finally:
        db.dispose()


def test_restart_before_every_bar_equals_uninterrupted(
    tmp_path: Path, market_cache: Path, reference: dict[str, str]
) -> None:
    """Restore from the checkpoint before each bar (new engine and runner every day)."""
    db, data, path = _world(tmp_path, market_cache, "daily")
    db.dispose()
    day = D1
    while day <= D2:
        eng = make_engine(path)
        try:
            runner = make_runner(eng, data)
            runner.tick(after_close(day))
        finally:
            eng.dispose()
        day = date.fromordinal(day.toordinal() + 1)
    eng = make_engine(path)
    try:
        assert db_fingerprint(eng) == reference
    finally:
        eng.dispose()


def test_fault_hook_exception_is_a_recorded_bar_failure_not_a_partial_write(
    tmp_path: Path, market_cache: Path, reference: dict[str, str]
) -> None:
    db, data, _ = _world(tmp_path, market_cache, "soft")
    try:
        runner = make_runner(db, data)
        _phases(runner)
        c0 = counts(db, FUND)

        def boom(point: str) -> None:
            if point == "before_commit":
                raise ValueError("disk full")

        runner.fault_hook = boom
        res = runner.tick(after_close(D2), portfolios=[FUND])
        assert res[0].status == "error" and "disk full" in (res[0].error or "")
        assert counts(db, FUND) == c0
        with session_scope(db) as s:
            p = repo.get_portfolio(s, FUND)
            assert p is not None and p.id is not None
            rs = repo.get_runner_state(s, p.id)
            assert rs.last_status == "error" and rs.consecutive_failures == 1
        runner.fault_hook = None  # the cached engine was discarded and is rebuilt
        assert runner.tick(after_close(D2), portfolios=[FUND])[0].status == "ok"
        assert db_fingerprint(db) == reference
    finally:
        db.dispose()


def test_recommitting_a_committed_bar_raises_and_leaves_the_ledger_alone(
    tmp_path: Path, market_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR A3: re-running a committed bar raises; nothing changes."""
    import tradebot.runner.tick as tick_mod

    steps: list[Any] = []

    class Recording(tick_mod.BarDriver):  # type: ignore[misc]
        def step(self, ts: pd.Timestamp) -> Any:
            out = super().step(ts)
            steps.append(out)
            return out

    monkeypatch.setattr(tick_mod, "BarDriver", Recording)
    db, data, _ = _world(tmp_path, market_cache, "dup")
    try:
        runner = make_runner(db, data)
        runner.tick(after_close(date(2019, 1, 5)))
        before = db_fingerprint(db)
        with session_scope(db) as s:
            fund = repo.get_portfolio(s, FUND)
            assert fund is not None and fund.id is not None
        ctx = runner._contexts[fund.id]  # the fund ticked last, so steps[-1] is its last bar
        last = steps[-1]
        with pytest.raises(RunnerError, match="already processed"):
            runner._commit_bar(ctx, last, last.asof, after_close(date(2019, 1, 5)), catchup=False)
        assert db_fingerprint(db) == before
    finally:
        db.dispose()


def test_stale_second_runner_cannot_double_apply(
    tmp_path: Path, market_cache: Path, reference: dict[str, str]
) -> None:
    """Two runners on one DB: the one with the stale in-memory engine is refused."""
    db, data, path = _world(tmp_path, market_cache, "two")
    other = make_engine(path)
    try:
        a = make_runner(db, data)
        b = make_runner(other, data)
        a.tick(after_close(D1))
        b.tick(after_close(D1))  # b restores/caches its contexts at D1 (noop, but cached below)
        for r in (a, b):
            r._context(1)
        a.tick(after_close(date(2019, 1, 6)))  # a moves ahead
        res = b.tick(after_close(date(2019, 1, 9)), portfolios=["bench_spy"])
        assert res[0].status == "error" and "stale" in (res[0].error or "")
        # b's refused bar left nothing behind and its context was discarded
        a.tick(after_close(D2))
        b.tick(after_close(D2))
        assert db_fingerprint(db) == reference
    finally:
        other.dispose()
        db.dispose()


def test_tampered_ledger_is_detected_on_restore_and_the_portfolio_never_trades(
    tmp_path: Path, market_cache: Path
) -> None:
    db, data, _ = _world(tmp_path, market_cache, "tamper")
    try:
        runner = make_runner(db, data)
        runner.tick(after_close(date(2019, 1, 8)))
        with session_scope(db) as s:
            p = repo.get_portfolio(s, FUND)
            assert p is not None and p.id is not None
            row = s.exec(
                select(LedgerEventRow).where(
                    LedgerEventRow.portfolio_id == p.id, LedgerEventRow.seq == 2
                )
            ).one()
            row.event_json = row.event_json.replace('"amount":"', '"amount":"9')
            s.add(row)
            pid = p.id
        before = counts(db, FUND)
        with session_scope(db) as s:
            p = repo.get_portfolio(s, FUND)
            assert p is not None
            with pytest.raises(StateCorruptionError):
                restore_context(s, p)
        fresh = make_runner(db, data)
        res = {r.portfolio: r for r in fresh.tick(after_close(D2))}
        assert res[FUND].status == "error"
        assert res["bench_spy"].status == "ok"  # the others are unaffected
        assert counts(db, FUND) == before
        with session_scope(db) as s:
            assert repo.get_runner_state(s, pid).last_status == "error"
    finally:
        db.dispose()


def test_checkpoint_marker_mismatch_is_corruption(tmp_path: Path, market_cache: Path) -> None:
    db, data, _ = _world(tmp_path, market_cache, "marker")
    try:
        make_runner(db, data).tick(after_close(date(2019, 1, 6)))
        with session_scope(db) as s:
            p = repo.get_portfolio(s, FUND)
            assert p is not None and p.id is not None
            newest = repo.latest_checkpoint(s, p.id)
            assert newest is not None
            s.delete(newest)
        with session_scope(db) as s:
            p = repo.get_portfolio(s, FUND)
            assert p is not None
            with pytest.raises(StateCorruptionError, match="marker"):
                restore_context(s, p)
    finally:
        db.dispose()


def test_orders_fill_on_the_next_bar_at_its_open(tmp_path: Path, market_cache: Path) -> None:
    """AC 4: an order made on bar t fills on that symbol's next bar, at that bar's open."""
    db, data, _ = _world(tmp_path, market_cache, "fills")
    try:
        make_runner(db, data).tick(after_close(date(2019, 1, 10)))
        spy = data.get_bars("SPY", date(2018, 12, 1), date(2019, 1, 31), refresh=False)
        with session_scope(db) as s:
            p = repo.get_portfolio(s, "bench_spy")
            assert p is not None and p.id is not None
            orders = s.exec(select(OrderRow).where(OrderRow.portfolio_id == p.id)).all()
            fills = s.exec(
                select(LedgerEventRow)
                .where(LedgerEventRow.portfolio_id == p.id, LedgerEventRow.type == "FILL")
                .order_by(col(LedgerEventRow.seq))
            ).all()
        assert orders and fills
        by_id = {o.order_id: o for o in orders}
        for ev in fills:
            o = by_id[ev.order_id or ""]
            assert o.created_asof == START and o.fill_asof == date(2019, 1, 3)
            assert ev.asof == o.fill_asof > o.created_asof
            open_ = Decimal(repr(float(spy.loc[pd.Timestamp(ev.asof, tz="UTC"), "open"])))
            assert ev.price is not None
            assert abs(Decimal(ev.price) - open_ * Decimal("1.0005")) < Decimal("0.0001")
        assert all(o.fill_asof is None or o.fill_asof > o.created_asof for o in orders)
    finally:
        db.dispose()


@pytest.mark.slow
def test_subprocess_killed_mid_run_then_restarted(
    tmp_path: Path, market_cache: Path, reference: dict[str, str]
) -> None:
    """``os._exit`` in the middle of a bar transaction, then restart in a new process."""
    import subprocess
    import sys
    import textwrap

    db, data, path = _world(tmp_path, market_cache, "proc")
    db.dispose()
    cache = tmp_path / "cache_proc"
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from datetime import date
        from pathlib import Path
        from runner_support import after_close, make_data, make_runner
        from tradebot.db import make_engine
        db = make_engine(Path({str(path)!r}))
        calls = {{"n": 0}}
        def hook(point):
            if point == "before_commit":
                calls["n"] += 1
                if calls["n"] == 4:
                    os._exit(9)
        make_runner(db, make_data(Path({str(cache)!r})), hook).tick(after_close(date(2019, 1, 12)))
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
    assert proc.returncode == 9, proc.stderr.decode()[-2000:]
    eng = make_engine(path)
    try:
        res = make_runner(eng, make_data(cache)).tick(after_close(D2))
        assert all(r.status in ("ok", "noop") for r in res)
        assert db_fingerprint(eng) == reference
    finally:
        eng.dispose()
