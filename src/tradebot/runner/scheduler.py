"""APScheduler 3.x wiring for the runner (SPEC-004 "Scheduler jobs").

Every job body is ``Runner.tick`` (or the heartbeat): ``tick`` works out the due bars from
the calendar and ``runner_state``, so the cron times are only nudges and correctness never
depends on a job firing on time. Jobs are re-registered at every start; state lives in the
database, not in a job store.
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import UTC, datetime, timedelta
from types import FrameType
from zoneinfo import ZoneInfo

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from tradebot import __version__
from tradebot.clock import Clock, SystemClock
from tradebot.config import Settings
from tradebot.data import MarketData
from tradebot.data.config import DataConfig
from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.logging_config import configure_logging
from tradebot.runner.clock import INTRADAY_DELAY_MIN, session_bounds
from tradebot.runner.lock import RunnerLock, lock_path
from tradebot.runner.tick import Runner

logger = logging.getLogger("tradebot.runner")

NY = ZoneInfo("America/New_York")
MISFIRE_GRACE_S = 6 * 3600
JOB_IDS = (
    "startup_catchup",
    "equity_close",
    "equity_retry",
    "crypto_close",
    "crypto_retry",
    "intraday_bar",
    "heartbeat",
)

__all__ = ["JOB_IDS", "build_scheduler", "run_once", "run_runner"]


def _equity_retry_window(now: datetime) -> bool:
    """True Mon-Fri from 16:35 to 23:59 New York time (the retry job's guard)."""
    local = now.astimezone(NY)
    return local.weekday() < 5 and (local.hour, local.minute) >= (16, 35)


def _intraday_window(now: datetime) -> bool:
    """True during an XNYS session's tick window (early closes respected).

    From the first bar's completion (open + 15 min + 1 min) to 15 minutes after the close, so a
    late or retried fire still collects the last bar (it is due at close + 1 min). The cron
    trigger fires at :01/:16/:31/:46 every hour 09-16 New York; this guard drops the rest.
    """
    now = now.astimezone(UTC)
    bounds = session_bounds(now.astimezone(NY).date())
    if bounds is None:
        return False
    open_, close = bounds
    first = open_ + timedelta(minutes=15 + INTRADAY_DELAY_MIN)
    return first <= now <= close + timedelta(minutes=15)


def intraday_trigger() -> CronTrigger:
    """Bar open + 15 min + 1 min for 15m bars: :01, :16, :31, :46 on weekdays, 09-16 New York."""
    return CronTrigger(day_of_week="mon-fri", hour="9-16", minute="1,16,31,46", timezone=NY)


def build_scheduler(
    runner: Runner, scheduler: BaseScheduler | None = None, *, clock: Clock | None = None
) -> BaseScheduler:
    """Register the seven jobs on ``scheduler`` (a new ``BlockingScheduler`` by default)."""
    sched = scheduler or BlockingScheduler(timezone="UTC")
    clk = clock or runner.clock
    common = {"misfire_grace_time": MISFIRE_GRACE_S, "coalesce": True, "max_instances": 1}

    def heartbeat() -> None:
        with session_scope(runner.db) as s:
            repo.write_heartbeat(s, now=repo.naive_utc(clk.now()), version=__version__)

    def equity_retry() -> None:
        if _equity_retry_window(clk.now()):
            runner.tick(scope="equity")

    def intraday_bar() -> None:
        if _intraday_window(clk.now()):
            runner.tick(scope="intraday")

    sched.add_job(runner.tick, DateTrigger(), id="startup_catchup", **common)
    sched.add_job(
        runner.tick,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=35, timezone=NY),
        id="equity_close",
        kwargs={"scope": "equity"},
        **common,
    )
    sched.add_job(equity_retry, IntervalTrigger(minutes=15), id="equity_retry", **common)
    sched.add_job(
        runner.tick,
        CronTrigger(hour=0, minute=10, timezone="UTC"),
        id="crypto_close",
        kwargs={"scope": "crypto"},
        **common,
    )
    sched.add_job(
        runner.tick,
        IntervalTrigger(minutes=15),
        id="crypto_retry",
        kwargs={"scope": "crypto"},
        **common,
    )
    sched.add_job(intraday_bar, intraday_trigger(), id="intraday_bar", **common)
    sched.add_job(heartbeat, IntervalTrigger(minutes=5), id="heartbeat", **common)
    return sched


def run_runner(
    settings: Settings | None = None,
    *,
    data: MarketData | None = None,
    clock: Clock | None = None,
) -> int:
    """Start the blocking scheduler; returns 0 after a clean SIGINT/SIGTERM shutdown.

    Takes the ``runner.lock`` flock first (``RunnerLockedError`` if another runner is live).
    """
    settings = settings or Settings()
    configure_logging(settings)
    clk = clock or SystemClock()
    with RunnerLock(lock_path(settings)):
        db = make_engine(settings.db_path)
        try:
            create_all(db)
            md = data or MarketData(DataConfig(cache_dir=settings.cache_dir), clock=clk)
            runner = Runner(db, md, settings, clk)
            sched = build_scheduler(runner, clock=clk)
            stop = threading.Event()

            def _shutdown(signum: int, _frame: FrameType | None) -> None:
                logger.info("signal received, shutting down", extra={"portfolio": "-"})
                stop.set()
                sched.shutdown(wait=False)

            signal.signal(signal.SIGINT, _shutdown)
            signal.signal(signal.SIGTERM, _shutdown)
            logger.info("runner started", extra={"portfolio": "-"})
            sched.start()  # blocks until shutdown
        finally:
            db.dispose()
    return 0


def run_once(
    settings: Settings | None = None,
    *,
    data: MarketData | None = None,
    clock: Clock | None = None,
    now: datetime | None = None,
) -> list[str]:
    """One ``tick`` under the runner lock (CLI ``runner tick``). Returns one line per portfolio."""
    settings = settings or Settings()
    clk = clock or SystemClock()
    with RunnerLock(lock_path(settings)):
        db = make_engine(settings.db_path)
        try:
            create_all(db)
            md = data or MarketData(DataConfig(cache_dir=settings.cache_dir), clock=clk)
            results = Runner(db, md, settings, clk).tick(now)
        finally:
            db.dispose()
    return [
        f"{r.portfolio}: {r.status} ({len(r.processed)} bars)" + (f" {r.error}" if r.error else "")
        for r in results
    ]
