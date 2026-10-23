"""The runner: ``Runner.tick`` processes every due bar, one transaction per portfolio per bar.

For portfolio P and its next unprocessed completed bar t (SPEC-004 "Processing a bar"):

0. restore (once per process, and again after any failed bar): latest checkpoint, then
   ``PortfolioEngine.verify()`` and a ledger-row cross-check;
1. load bars through t from ``MarketData`` (a missing bar means ``data_unavailable`` and
   no progress);
2. ``BarDriver.step(t)``: the *only* place anything is filled, marked or decided;
3. persist in one ``BEGIN IMMEDIATE`` transaction: ledger events, order updates, snapshots,
   kill-switch mirror, ``engine_checkpoint`` (state + ``target_book_json``), ``run_log`` and
   ``runner_state.last_processed_bar_ts``.

The runner contains no fill, mark or decide logic. Correctness never depends on when a job
fires: ``tick`` derives the due bars from the calendar and ``runner_state``.

Day-trading portfolios (ADR-0001 A21, ``PortfolioDef.timeframe() == "15m"``) run the same steps
per completed 15-minute bar: the due bars come from :func:`due_bars_15m`, the bars are loaded
with ``timeframe=M15`` and ``bar_ts`` is the bar-open timestamp. One ``tick`` therefore catches
up every completed bar. The scheduler's ``intraday_bar`` job (``scope="intraday"``) calls it at
every bar open + 15 min + 1 min during XNYS sessions.

Fault seam: ``fault_hook(point)`` is called at ``after_driver``, ``before_commit`` and
``after_commit``. An ``Exception`` from the hook is a normal bar failure (recorded); a
``BaseException`` propagates like a process death. Either way nothing is half-written.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

import pandas as pd
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from tradebot.backtest.driver import BarDriver, StepResult
from tradebot.backtest.engine import validate_bars
from tradebot.clock import Clock, SystemClock
from tradebot.config import Settings
from tradebot.data import MarketData
from tradebot.data.errors import DataError
from tradebot.data.types import Timeframe
from tradebot.db import repo
from tradebot.db.engine import session_scope
from tradebot.portfolio.models import EventType
from tradebot.portfolio.money import to_text
from tradebot.runner.bootstrap import restore_context
from tradebot.runner.clock import (
    INTRADAY_DELAY_MIN,
    due_bars_15m,
    due_dates,
    has_bar,
    market_of,
)
from tradebot.runner.state import (
    FaultHook,
    PortfolioContext,
    PortfolioDef,
    PortfolioTick,
    RunnerError,
)

logger = logging.getLogger("tradebot.runner")

Scope = Literal["all", "equity", "crypto", "intraday"]


def _ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d, tz="UTC")


def _naive(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _naive_ts(ts: pd.Timestamp) -> datetime:
    """UTC bar-open ``Timestamp`` -> the naive UTC datetime stored as ``bar_ts``."""
    return ts.tz_convert("UTC").tz_localize(None).to_pydatetime()


def _label(bar_ts: datetime) -> str:
    """Log/error label of a bar: the date for a daily bar, the full timestamp for a 15m one."""
    return bar_ts.date().isoformat() if bar_ts == _naive(bar_ts.date()) else bar_ts.isoformat()


class Runner:
    """Owns the DB engine, the data service and the per-portfolio in-memory contexts."""

    def __init__(
        self,
        db: Engine,
        data: MarketData,
        settings: Settings | None = None,
        clock: Clock | None = None,
        *,
        fault_hook: FaultHook | None = None,
        refresh: bool = True,
    ) -> None:
        self.db = db
        self.data = data
        self.settings = settings or Settings()
        self.clock: Clock = clock or SystemClock()
        self.fault_hook = fault_hook
        self.refresh = refresh
        self._contexts: dict[int, PortfolioContext] = {}

    # ------------------------------------------------------------------ public

    def tick(
        self,
        now: datetime | None = None,
        *,
        portfolios: Sequence[str] | None = None,
        scope: Scope = "all",
    ) -> list[PortfolioTick]:
        """Process every due bar of every (selected) active portfolio, in order.

        ``now`` defaults to the injected clock. Benchmarks run before the fund so the fund's
        snapshot can carry the matching ``benchmark_equity``. ``scope`` restricts to
        equity-only portfolios, to those containing crypto, or (``"intraday"``) to 15m portfolios.
        """
        now = (now or self.clock.now()).astimezone(UTC)
        with session_scope(self.db) as s:
            rows = [
                (int(p.id or 0), p.name, p.kind)
                for p in repo.list_portfolios(s)
                if portfolios is None or p.name in portfolios
            ]
        rows.sort(key=lambda r: (r[2] != "benchmark", r[0]))
        return [r for pid, name, _ in rows if (r := self._tick_portfolio(pid, name, now, scope))]

    def discard_contexts(self) -> None:
        """Forget cached engines (next tick restores from the checkpoints)."""
        self._contexts.clear()

    # ------------------------------------------------------------------ one portfolio

    def _hook(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def _tick_portfolio(
        self, pid: int, name: str, now: datetime, scope: Scope
    ) -> PortfolioTick | None:
        try:
            with session_scope(self.db) as s:
                p = repo.get_portfolio(s, pid)
                assert p is not None
                defn = PortfolioDef.from_json(p.config_json)
                rs = repo.get_runner_state(s, pid)
                last_ts = rs.last_processed_bar_ts
                after = last_ts.date() if last_ts else None
            if defn.timeframe() == "15m":
                if scope == "crypto":
                    return None
                return self._tick_intraday(pid, name, defn, last_ts, now)
            if scope == "intraday":
                return None  # the 15-minute job leaves daily portfolios to the close jobs
            markets = defn.markets()
            if (scope == "equity" and markets != ["equity"]) or (
                scope == "crypto" and "crypto" not in markets
            ):
                return None
            due = due_dates(
                markets,
                start=defn.start,
                after=after,
                now=now,
                equity_delay_min=self.settings.equity_close_delay_min,
            )
            if not due:
                return PortfolioTick(name, "noop")
            if len(due) > self.settings.max_catchup_bars:
                msg = (
                    f"{len(due)} missed bars exceeds MAX_CATCHUP_BARS="
                    f"{self.settings.max_catchup_bars}; use `tradebot runner reset-from`"
                )
                self._record(pid, "error", msg, now)
                logger.error(msg, extra={"portfolio": name})
                return PortfolioTick(name, "catchup_refused", error=msg)
            return self._process(pid, name, defn, due, now)
        except Exception as exc:  # noqa: BLE001 - one broken portfolio must not stop the others
            self._contexts.pop(pid, None)
            msg = f"{type(exc).__name__}: {exc}"
            logger.exception("tick failed", extra={"portfolio": name})
            self._record(pid, "error", msg, now)
            return PortfolioTick(name, "error", error=msg)
        except BaseException:
            self._contexts.pop(pid, None)
            raise

    def _record(self, pid: int, status: str, error: str | None, now: datetime) -> None:
        with session_scope(self.db) as s:
            repo.record_status(s, pid, status=status, error=error, now=repo.naive_utc(now))

    def _context(self, pid: int) -> PortfolioContext:
        ctx = self._contexts.get(pid)
        if ctx is None:
            with session_scope(self.db) as s:
                p = repo.get_portfolio(s, pid)
                assert p is not None
                ctx = restore_context(s, p)
            self._contexts[pid] = ctx
        return ctx

    def _tick_intraday(
        self, pid: int, name: str, defn: PortfolioDef, last_ts: datetime | None, now: datetime
    ) -> PortfolioTick:
        """Process every completed 15m bar not yet processed (same steps as the daily path)."""
        after = pd.Timestamp(last_ts, tz="UTC") if last_ts is not None else None
        cap = self.settings.max_catchup_bars
        due = due_bars_15m(
            start=defn.start, after=after, now=now, delay_min=INTRADAY_DELAY_MIN, limit=cap + 1
        )
        if not due:
            return PortfolioTick(name, "noop")
        if len(due) > cap:
            msg = (
                f"more than {cap} missed 15m bars exceeds MAX_CATCHUP_BARS={cap}; "
                "use `tradebot runner reset-from`"
            )
            self._record(pid, "error", msg, now)
            logger.error(msg, extra={"portfolio": name})
            return PortfolioTick(name, "catchup_refused", error=msg)
        ctx = self._context(pid)
        try:
            frames = self._load_frames(defn, due[-1].date(), Timeframe.M15)
        except DataError as exc:
            self._record(pid, "data_unavailable", f"{type(exc).__name__}: {exc}", now)
            return PortfolioTick(name, "data_unavailable", error=str(exc))
        # A due bar with no data for any symbol is skipped only if a LATER due bar has data: SIP
        # delivers in order, so that bar is never coming, and waiting would strand the positions
        # (audit 007 m7; the driver then flattens them at the next bar's open as FLATTEN_LATE).
        present = [any(ts in f.index for f in frames.values()) for ts in due]
        n_handled = max((k + 1 for k, ok in enumerate(present) if ok), default=0)
        ready: list[pd.Timestamp] = [ts for ts, ok in zip(due, present, strict=True) if ok]
        if not ready:
            msg = f"no data for bar {due[0].isoformat()}"
            self._record(pid, "data_unavailable", msg, now)
            return PortfolioTick(name, "data_unavailable", error=msg)
        driver = BarDriver(ctx.engine, list(defn.sleeves), frames, start=defn.start, book=ctx.book)
        catchup = len(due) > 1
        for ts in ready:
            step = driver.step(ts)
            self._hook("after_driver")
            self._commit_bar(ctx, step, _naive_ts(ts), now, catchup=catchup)
            self._hook("after_commit")
        status: Literal["ok", "data_unavailable"] = (
            "ok" if n_handled == len(due) else "data_unavailable"
        )
        err = None if status == "ok" else f"no data for bar {due[n_handled].isoformat()}"
        if status != "ok":
            self._record(pid, status, err, now)
        return PortfolioTick(name, status, processed=list(ready), error=err)

    def _load_frames(
        self, defn: PortfolioDef, end: date, timeframe: Timeframe = Timeframe.D1
    ) -> dict[str, pd.DataFrame]:
        symbols = defn.required_symbols()
        if timeframe is Timeframe.D1:
            got = self.data.get_bars_many(symbols, defn.data_start, end, refresh=self.refresh)
        else:
            got = self.data.get_bars_many(
                symbols, defn.data_start, end, timeframe=timeframe, refresh=self.refresh
            )
        frames = {str(k): v for k, v in got.items()}
        validate_bars(frames)
        return frames

    @staticmethod
    def _bar_present(defn: PortfolioDef, frames: dict[str, pd.DataFrame], d: date) -> bool:
        """Every market that has a calendar bar on ``d`` must have a data row for ``d``."""
        ts = _ts(d)
        for m in defn.markets():
            if not has_bar(m, d):
                continue
            syms = [s for s in defn.required_symbols() if market_of(s) == m]
            if not any(ts in frames[s].index for s in syms if s in frames):
                return False
        return True

    def _process(
        self, pid: int, name: str, defn: PortfolioDef, due: list[date], now: datetime
    ) -> PortfolioTick:
        ctx = self._context(pid)
        try:
            frames = self._load_frames(defn, due[-1])
        except DataError as exc:
            self._record(pid, "data_unavailable", f"{type(exc).__name__}: {exc}", now)
            return PortfolioTick(name, "data_unavailable", error=str(exc))
        ready: list[date] = []
        for d in due:
            if not self._bar_present(defn, frames, d):
                break
            ready.append(d)
        if not ready:
            msg = f"no data for bar {due[0]}"
            self._record(pid, "data_unavailable", msg, now)
            return PortfolioTick(name, "data_unavailable", error=msg)
        driver = BarDriver(ctx.engine, list(defn.sleeves), frames, start=defn.start, book=ctx.book)
        done: list[date] = []
        catchup = len(due) > 1
        for d in ready:
            step = driver.step(_ts(d))
            self._hook("after_driver")
            self._commit_bar(ctx, step, d, now, catchup=catchup)
            done.append(d)
            self._hook("after_commit")
        status: Literal["ok", "data_unavailable"] = (
            "ok" if len(ready) == len(due) else "data_unavailable"
        )
        err = None if status == "ok" else f"no data for bar {due[len(ready)]}"
        if status != "ok":
            self._record(pid, status, err, now)
        return PortfolioTick(name, status, processed=done, error=err)

    # ------------------------------------------------------------------ the transaction

    def _commit_bar(
        self,
        ctx: PortfolioContext,
        step: StepResult,
        bar: date | datetime,
        now: datetime,
        *,
        catchup: bool,
    ) -> None:
        pid = ctx.portfolio_id
        engine = ctx.engine
        bar_ts = bar if isinstance(bar, datetime) else _naive(bar)  # naive UTC bar label
        d = _label(bar_ts)
        now_n = repo.naive_utc(now)
        new_events = list(engine.ledger)[ctx.n_ledger :]
        if new_events and new_events[0].seq != ctx.n_ledger + 1:
            raise RunnerError("ledger sequence is not contiguous with the persisted ledger")
        changed = [o for o in engine.orders() if ctx.orders.get(o.order_id) != o]
        result = step.bar_result
        snap = result.snapshot
        state_json = engine.export_state().to_json()
        book_json = _book_json(ctx)
        with session_scope(self.db) as s:
            rs = repo.get_runner_state(s, pid)
            if rs.last_processed_bar_ts is not None and bar_ts <= rs.last_processed_bar_ts:
                raise RunnerError(
                    f"bar {d} was already processed (marker {rs.last_processed_bar_ts})"
                )
            if rs.last_processed_bar_ts != ctx.last_bar:
                raise RunnerError(
                    f"stale context: engine is at {ctx.last_bar}, database at "
                    f"{rs.last_processed_bar_ts} (another writer?)"
                )
            try:
                repo.append_ledger_events(s, pid, new_events, ctx.sleeve_ids)
                repo.upsert_orders(s, pid, changed, ctx.sleeve_ids, now_n)
                bench = self._benchmark_equity(s, ctx.defn, bar_ts)
                bench_missing = bench is None and self._benchmark_expected(s, ctx.defn, bar_ts)
                if bench_missing:
                    logger.warning(
                        "benchmark snapshot missing",
                        extra={
                            "portfolio": ctx.defn.name,
                            "benchmark": ctx.defn.benchmark,
                            "bar_ts": d,
                        },
                    )
                repo.write_snapshots(s, pid, bar_ts, snap, ctx.sleeve_ids, benchmark_equity=bench)
                repo.mirror_kill_switch(
                    s,
                    pid,
                    tripped=snap.killed,
                    peak_equity=snap.peak_equity,
                    bar_ts=bar_ts,
                    drawdown=snap.drawdown,
                    events=[e.type.value for e in new_events],
                    now=now_n,
                )
                p = repo.get_portfolio(s, pid)
                assert p is not None
                p.peak_equity = to_text(snap.peak_equity)
                p.status = (
                    "halted" if snap.killed else ("active" if p.status == "halted" else p.status)
                )
                s.add(p)
                for sl in repo.list_sleeves(s, pid):
                    sleeve_snap = next((x for x in snap.sleeves if x.sleeve_id == sl.key), None)
                    if sleeve_snap is not None:
                        sl.cash = to_text(sleeve_snap.cash)
                        s.add(sl)
                repo.write_checkpoint(
                    s, pid, bar_ts, state_json, book_json, len(engine.ledger), now_n
                )
                repo.prune_checkpoints(s, pid, self.settings.checkpoint_keep)
                fills_ev = [e for e in new_events if e.type is EventType.FILL]
                n_fills = len(fills_ev)
                flags = [str(e.metadata.get("flag", "")) for e in fills_ev]
                n_no_bar = sum("FLATTEN_NO_BAR" in f for f in flags)
                n_late = sum("FLATTEN_LATE" in f for f in flags)
                repo.insert_run_log(
                    s,
                    pid,
                    bar_ts,
                    started_at=now_n,
                    finished_at=now_n,
                    n_orders_created=sum(1 for o in changed if o.order_id not in ctx.orders),
                    n_fills=n_fills,
                    detail={
                        "catchup": catchup,
                        "n_ledger_events": len(new_events),
                        "kill_switch_tripped": result.kill_switch_tripped,
                        "rebalance_planned": result.rebalance_planned,
                        **({"flatten_no_bar": n_no_bar} if n_no_bar else {}),
                        **({"flatten_late": n_late} if n_late else {}),
                        **({"benchmark_missing": ctx.defn.benchmark} if bench_missing else {}),
                    },
                )
                rs.last_processed_bar_ts = bar_ts
                rs.last_run_at = now_n
                rs.last_status = "ok"
                rs.last_error = None
                rs.consecutive_failures = 0
                s.add(rs)
                s.flush()
            except IntegrityError as exc:  # a double-run: constraints turn it into a rollback
                raise RunnerError(f"duplicate write for bar {d}: {exc.orig}") from exc
            self._hook("before_commit")
        ctx.n_ledger = len(engine.ledger)
        ctx.last_bar = bar_ts
        for o in changed:
            ctx.orders[o.order_id] = o
        logger.info(
            "bar committed",
            extra={"portfolio": ctx.defn.name, "bar_ts": d, "catchup": catchup},
        )

    @staticmethod
    def _benchmark_expected(s: repo.Session, defn: PortfolioDef, bar_ts: datetime) -> bool:
        """True if ``defn`` has a benchmark that should have a snapshot at ``bar_ts``."""
        if defn.benchmark is None:
            return False
        bench = repo.get_portfolio(s, defn.benchmark)
        if bench is None:
            return True
        markets = PortfolioDef.from_json(bench.config_json).markets()
        return any(has_bar(m, bar_ts.date()) for m in markets)

    @staticmethod
    def _benchmark_equity(s: repo.Session, defn: PortfolioDef, bar_ts: datetime) -> Decimal | None:
        if defn.benchmark is None:
            return None
        bench = repo.get_portfolio(s, defn.benchmark)
        if bench is None or bench.id is None:
            return None
        return repo.total_equity_at(s, bench.id, bar_ts)


def _book_json(ctx: PortfolioContext) -> str:
    import json

    return json.dumps(ctx.book.state(), sort_keys=True)
