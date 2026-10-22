"""Scheduler wiring and the single-runner lock (SPEC-004 T5)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from runner_support import make_data, make_runner
from sqlalchemy import Engine

from tradebot.config import Settings
from tradebot.db import repo, session_scope
from tradebot.runner.lock import RunnerLock, RunnerLockedError, is_locked, lock_path
from tradebot.runner.scheduler import JOB_IDS, _equity_retry_window, build_scheduler


def test_all_jobs_registered(db: Engine, market_cache: Path) -> None:
    runner = make_runner(db, make_data(market_cache))
    sched = build_scheduler(runner, BackgroundScheduler(timezone="UTC"))
    assert sorted(j.id for j in sched.get_jobs()) == sorted(JOB_IDS)
    for job in sched.get_jobs():
        assert job.max_instances == 1


def test_equity_retry_window_is_new_york_time() -> None:
    assert _equity_retry_window(datetime(2025, 11, 26, 22, 0, tzinfo=UTC))  # 17:00 ET Wed
    assert not _equity_retry_window(datetime(2025, 11, 26, 20, 0, tzinfo=UTC))  # 15:00 ET
    assert not _equity_retry_window(datetime(2025, 11, 29, 22, 0, tzinfo=UTC))  # Saturday


def test_heartbeat_job_writes_the_row(db: Engine, market_cache: Path) -> None:
    runner = make_runner(db, make_data(market_cache))
    sched = build_scheduler(runner, BackgroundScheduler(timezone="UTC"))
    next(j for j in sched.get_jobs() if j.id == "heartbeat").func()
    with session_scope(db) as s:
        from tradebot.db.models import RunnerHeartbeat

        row = s.get(RunnerHeartbeat, 1)
        assert row is not None and row.pid > 0
        assert repo.naive_utc(runner.clock.now()) == row.ts


def test_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    path = lock_path(Settings(db_path=tmp_path / "x.db"))
    assert path == tmp_path / "runner.lock"
    with RunnerLock(path):
        assert is_locked(path)
        with pytest.raises(RunnerLockedError):
            RunnerLock(path).acquire()
    assert not is_locked(path)


@pytest.mark.slow
def test_runner_process_catches_up_and_exits_cleanly_on_sigint(
    tmp_path: Path, market_cache: Path
) -> None:
    """T5 done-when: the runner starts, catches up, and exits 0 on SIGINT."""
    import signal
    import subprocess
    import sys
    import textwrap
    import time

    from runner_support import START, defs, seed

    from tradebot.db import create_all, make_engine

    db_path = tmp_path / "proc.db"
    eng = make_engine(db_path)
    create_all(eng)
    seed(eng, defs(START, fund=False, benchmarks=("SPY",)))
    eng.dispose()
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from datetime import date
        from pathlib import Path
        from runner_support import after_close, make_data
        from tradebot.clock import FixedClock
        from tradebot.config import Settings
        from tradebot.runner.scheduler import run_runner
        logs = Path({str(tmp_path / "logs")!r})
        settings = Settings(db_path=Path({str(db_path)!r}), log_dir=logs)
        raise SystemExit(run_runner(settings, data=make_data(Path({str(market_cache)!r})),
                                    clock=FixedClock(after_close(date(2019, 1, 9)))))
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 40
        done = False
        while time.monotonic() < deadline and proc.poll() is None and not done:
            time.sleep(0.5)
            check = make_engine(db_path)
            try:
                with session_scope(check) as s:
                    p = repo.get_portfolio(s, "bench_spy")
                    assert p is not None and p.id is not None
                    rs = repo.get_runner_state(s, p.id)
                    done = (
                        rs.last_processed_bar_ts is not None
                        and rs.last_processed_bar_ts.date().isoformat() == "2019-01-09"
                    )
            finally:
                check.dispose()
        assert done, "runner did not catch up in time"
        assert is_locked(tmp_path / "runner.lock")  # held while running
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        assert proc.stderr is not None
        proc.stderr.close()
    assert not is_locked(tmp_path / "runner.lock")
