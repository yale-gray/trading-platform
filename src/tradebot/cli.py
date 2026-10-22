"""Unified ``tradebot`` command line (SPEC-004 T13, ADR D11).

argparse, two-level command tree. Handlers import their heavy dependencies (fastapi,
apscheduler, pandas, the backtester) lazily, so ``tradebot --help`` and ``tradebot db init``
work with only the base dependencies. ``main(argv=None) -> int`` is the test seam.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tradebot.config import Settings
    from tradebot.data import MarketData

PRESETS = ("fund", "bench_spy", "bench_btc")
MODES = ("single", "walk_forward", "sensitivity")


# ---------------------------------------------------------------------------- helpers


def _settings() -> Settings:
    from tradebot.config import Settings

    return Settings()


def _market_data(settings: Settings) -> MarketData:
    """The data service for CLI commands (tests monkeypatch this to an offline instance)."""
    from tradebot.data import MarketData
    from tradebot.data.config import DataConfig

    return MarketData(DataConfig(cache_dir=settings.cache_dir))


def _now() -> datetime:
    from tradebot.clock import SystemClock

    return SystemClock().now()


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from exc


def _table(rows: Sequence[Sequence[Any]], header: Sequence[str]) -> str:
    cells = [[str(c) for c in r] for r in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(header)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in cells]
    return "\n".join(line.rstrip() for line in lines)


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _open_db(settings: Settings, *, create: bool = True) -> Any:
    from tradebot.db import create_all, make_engine

    engine = make_engine(settings.db_path)
    if create:
        create_all(engine)
    return engine


# ---------------------------------------------------------------------------- data


def _cmd_data_refresh(args: argparse.Namespace) -> int:
    reports = _market_data(_settings()).refresh(args.symbols or None)
    return _print_reports(reports, fail_on_error=False)


def _cmd_data_verify(args: argparse.Namespace) -> int:
    reports = _market_data(_settings()).verify(args.symbols or None)
    return _print_reports(reports, fail_on_error=True)


def _print_reports(reports: dict[Any, Any], *, fail_on_error: bool) -> int:
    rows = [
        (str(sym), r.status, r.n_bars, len(r.gaps), len(r.warnings)) for sym, r in reports.items()
    ]
    print(_table(rows, ("symbol", "status", "bars", "gaps", "warnings")))
    for sym, r in reports.items():
        for w in r.warnings:
            print(f"  {sym}: {w}")
    bad = [str(s) for s, r in reports.items() if r.status != "ok"]
    if bad and fail_on_error:
        print(f"{len(bad)} symbol(s) with problems: {', '.join(bad)}", file=sys.stderr)
        return 1
    return 0


def _cmd_data_rebase(args: argparse.Namespace) -> int:
    if not _confirm(
        f"Rebase {args.symbol}: re-fetch the full history into a NEW generation?", args.yes
    ):
        return _err("aborted")
    gen = _market_data(_settings()).rebase(args.symbol)
    print(f"{args.symbol}: new generation {gen}")
    return 0


# ---------------------------------------------------------------------------- db


def _cmd_db_init(_args: argparse.Namespace) -> int:
    settings = _settings()
    engine = _open_db(settings)
    engine.dispose()
    print(f"database ready at {settings.db_path}")
    return 0


# ---------------------------------------------------------------------------- portfolio


def _latest_complete_bar(markets: Sequence[str], now: datetime, delay_min: int) -> date:
    """Latest bar date already complete at ``now`` for every market of the portfolio."""
    from tradebot.runner.clock import bar_complete_time

    d = now.astimezone(UTC).date()
    for _ in range(14):
        times = [t for m in markets if (t := bar_complete_time(m, d, delay_min)) is not None]
        if times and max(times) <= now:
            return d
        d -= timedelta(days=1)
    raise RuntimeError("no completed bar found in the last 14 days")


def _cmd_portfolio_create(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from sqlalchemy.exc import IntegrityError

    from tradebot.db import session_scope
    from tradebot.runner import benchmark_definition, create_portfolio, fund_definition
    from tradebot.runner.state import PortfolioDef

    if bool(args.preset) == bool(args.config):
        return _err("give exactly one of --preset or --config FILE")
    settings = _settings()
    now = _now()
    start = args.start

    if args.preset:
        symbol = {"bench_spy": "SPY", "bench_btc": "BTC/USD"}.get(args.preset)
        markets = ["equity"] if symbol == "SPY" else ["crypto"] if symbol else ["crypto", "equity"]
        start = start or _latest_complete_bar(markets, now, settings.equity_close_delay_min)
        defn = (
            fund_definition(start, name=args.name or "paper_fund")
            if symbol is None
            else benchmark_definition(symbol, start, name=args.name)
        )
    else:
        defn = PortfolioDef.from_json(Path(args.config).read_text(encoding="utf-8"))
        if args.name:
            defn = replace(defn, name=args.name)
        if start:
            defn = replace(defn, start=start)

    engine = _open_db(settings)
    try:
        with session_scope(engine) as s:
            p = create_portfolio(s, defn, now)
            pid = p.id
    except IntegrityError:
        return _err(f"portfolio {defn.name!r} already exists")
    finally:
        engine.dispose()
    print(f"created portfolio {defn.name!r} (id {pid}, {defn.kind}) starting {defn.start}")
    return 0


def _cmd_portfolio_list(_args: argparse.Namespace) -> int:
    from tradebot.db import repo, session_scope

    engine = _open_db(_settings())
    try:
        with session_scope(engine) as s:
            rows = []
            for p in repo.list_portfolios(s, include_archived=True):
                assert p.id is not None
                st = repo.get_runner_state(s, p.id)
                ks = repo.get_kill_switch(s, p.id)
                last = (
                    st.last_processed_bar_ts.date().isoformat() if st.last_processed_bar_ts else "-"
                )
                rows.append(
                    (
                        p.id,
                        p.name,
                        p.kind,
                        p.status,
                        last,
                        st.last_status,
                        "TRIPPED" if ks and ks.tripped else "ok",
                    )
                )
    finally:
        engine.dispose()
    print(_table(rows, ("id", "name", "kind", "status", "last_bar", "runner", "kill_switch")))
    return 0


def _cmd_portfolio_reset_kill_switch(args: argparse.Namespace) -> int:
    import getpass

    from tradebot.runner import RunnerError, reset_kill_switch
    from tradebot.runner.lock import RunnerLock, RunnerLockedError, lock_path

    settings = _settings()
    lock = RunnerLock(lock_path(settings))
    try:
        lock.acquire()  # held for the whole reset: refuses while a runner is live
    except RunnerLockedError:
        return _err("the runner is running (runner.lock is held); stop it before resetting")
    engine = _open_db(settings)
    try:
        if not _confirm(f"Reset the kill switch of {args.portfolio!r}? Trading resumes.", args.yes):
            return _err("aborted")
        try:
            asof = reset_kill_switch(
                engine, args.portfolio, operator=getpass.getuser(), note=args.note, now=_now()
            )
        except (RunnerError, ValueError, RuntimeError) as exc:
            return _err(str(exc))
        print(f"kill switch of {args.portfolio!r} reset on bar {asof}")
        return 0
    finally:
        engine.dispose()
        lock.release()


# ---------------------------------------------------------------------------- runner


def _cmd_runner_start(_args: argparse.Namespace) -> int:
    from tradebot.runner.lock import RunnerLockedError
    from tradebot.runner.scheduler import run_runner

    try:
        return run_runner(_settings())
    except RunnerLockedError as exc:
        return _err(str(exc))


def _cmd_runner_tick(args: argparse.Namespace) -> int:
    from tradebot.runner.lock import RunnerLockedError
    from tradebot.runner.scheduler import run_once

    settings = _settings()
    now = None
    if args.now:
        now = datetime.fromisoformat(args.now)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    try:
        lines = run_once(settings, data=_market_data(settings), now=now)
    except RunnerLockedError as exc:
        return _err(str(exc))
    print("\n".join(lines) if lines else "no portfolios")
    return 1 if any(": error" in ln or "catchup_refused" in ln for ln in lines) else 0


def _cmd_runner_reset_from(args: argparse.Namespace) -> int:
    """Accept a gap: skip the missed bars and resume at DATE (see ``runner.reset_from``)."""
    import getpass

    from tradebot.db import repo, session_scope
    from tradebot.runner import PortfolioDef, RunnerError, reset_from
    from tradebot.runner.lock import RunnerLock, RunnerLockedError, lock_path

    settings = _settings()
    lock = RunnerLock(lock_path(settings))
    try:
        lock.acquire()
    except RunnerLockedError:
        return _err("the runner is running (runner.lock is held); stop it first")
    engine = _open_db(settings)
    try:
        try:
            res = reset_from(
                engine,
                args.portfolio,
                args.date,
                operator=getpass.getuser(),
                note="cli reset-from",
                now=_now(),
            )
        except (RunnerError, ValueError, RuntimeError) as exc:
            return _err(str(exc))
        print(
            f"{args.portfolio}: bars after {res.skipped_after} skipped, "
            f"{len(res.cancelled_orders)} pending order(s) cancelled; "
            f"next bar processed is {args.date}"
        )
        with session_scope(engine) as s:
            p = repo.get_portfolio(s, args.portfolio)
            bench = PortfolioDef.from_json(p.config_json).benchmark if p is not None else None
        if bench:
            print(
                f"note: benchmark {bench!r} is not reset; run 'runner reset-from {bench} "
                f"{args.date}' too, or this portfolio's benchmark line is unavailable after the gap"
            )
        return 0
    finally:
        engine.dispose()
        lock.release()


# ---------------------------------------------------------------------------- backtest


def _cmd_backtest_run(args: argparse.Namespace) -> int:
    import json

    from tradebot.api.jobs import compute_backtest, persist_done
    from tradebot.strategy.io import dump_strategy, load_strategy

    settings = _settings()
    spec = load_strategy(Path(args.strategy_file))
    end = args.end or _now().date()
    outcome = compute_backtest(spec, args.start, end, {}, args.mode, _market_data(settings))
    m = outcome.report.metrics
    rows = [
        ("total_return", f"{m.total_return:.2%}"),
        ("cagr", f"{m.cagr:.2%}"),
        ("max_drawdown", f"{m.max_drawdown:.2%}"),
        ("sharpe", f"{m.sharpe:.2f}"),
        ("sortino", f"{m.sortino:.2f}"),
        ("trades", m.trade_count),
    ]
    for sym, bm in sorted(outcome.report.benchmark_metrics.items()):
        rows.append((f"{sym} total_return", f"{bm.total_return:.2%}"))
    print(_table(rows, ("metric", "value")))
    if args.mode != "single":
        gate = outcome.payload.get("validation", {})
        print(
            f"validation: {json.dumps(gate.get('gate', gate.get('split', {})), default=str)[:400]}"
        )
    if args.save:
        from tradebot.db import repo, session_scope
        from tradebot.db.models import BacktestRun

        engine = _open_db(settings)
        try:
            now = repo.naive_utc(_now())
            with session_scope(engine) as s:
                cfg = repo.get_or_create_strategy_config(
                    s,
                    name=spec.id,
                    market=spec.asset_class,
                    spec_json=dump_strategy(spec, "json"),
                    now=now,
                )
                run = BacktestRun(
                    strategy_config_id=cfg.id,
                    status="running",
                    started_at=now,
                    created_at=now,
                    params_json=json.dumps(
                        {
                            "strategy_id": cfg.id,
                            "start": args.start.isoformat(),
                            "end": end.isoformat(),
                            "mode": args.mode,
                            "overrides": {},
                        },
                        sort_keys=True,
                    ),
                )
                s.add(run)
                s.flush()
                run_id = int(run.id or 0)
            persist_done(engine, run_id, outcome, now)
        finally:
            engine.dispose()
        print(f"saved as backtest run {run_id}")
    return 0


# ---------------------------------------------------------------------------- api


def _cmd_api_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = _settings()
    host = args.host or settings.api_host
    port = args.port or settings.api_port
    if args.reload:
        uvicorn.run("tradebot.api.app:create_app", factory=True, host=host, port=port, reload=True)
    else:
        from tradebot.api.app import create_app

        uvicorn.run(create_app(settings), host=host, port=port)
    return 0


# ---------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    """The full command tree (no heavy imports)."""
    p = argparse.ArgumentParser(
        prog="tradebot", description="Paper-trading bot and platform (simulated money only)."
    )
    sub = p.add_subparsers(dest="group", required=True, metavar="GROUP")

    def group(name: str, help_: str) -> Any:
        g = sub.add_parser(name, help=help_, description=help_)
        return g.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    d = group("data", "market data cache")
    c = d.add_parser("refresh", help="fetch the missing tail for each symbol")
    c.add_argument("--symbols", nargs="+", metavar="S")
    c.set_defaults(func=_cmd_data_refresh)
    c = d.add_parser("verify", help="check cache integrity and quality (nonzero exit on errors)")
    c.add_argument("--symbols", nargs="+", metavar="S")
    c.set_defaults(func=_cmd_data_verify)
    c = d.add_parser("rebase", help="re-fetch a symbol's full history into a new generation")
    c.add_argument("symbol")
    c.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    c.set_defaults(func=_cmd_data_rebase)

    g = group("db", "database")
    c = g.add_parser("init", help="create the database and schema")
    c.set_defaults(func=_cmd_db_init)

    g = group("portfolio", "paper portfolios (created here only, never by the API)")
    c = g.add_parser("create", help="create a portfolio from a preset or a config file")
    c.add_argument("--preset", choices=PRESETS)
    c.add_argument("--name")
    c.add_argument("--config", metavar="FILE", help="PortfolioDef JSON (as stored in the DB)")
    c.add_argument("--start", type=_date, metavar="DATE", help="inception bar (default: latest)")
    c.set_defaults(func=_cmd_portfolio_create)
    c = g.add_parser("list", help="list portfolios")
    c.set_defaults(func=_cmd_portfolio_list)
    c = g.add_parser(
        "reset-kill-switch", help="the only way to reset a tripped kill switch (runner stopped)"
    )
    c.add_argument("portfolio")
    c.add_argument("--note", required=True)
    c.add_argument("--yes", action="store_true")
    c.set_defaults(func=_cmd_portfolio_reset_kill_switch)

    g = group("runner", "the paper runner")
    c = g.add_parser("start", help="run the scheduler (same as python -m tradebot.runner)")
    c.set_defaults(func=_cmd_runner_start)
    c = g.add_parser("tick", help="process due bars once and exit")
    c.add_argument("--once", action="store_true", help="accepted for clarity; tick is always once")
    c.add_argument("--now", metavar="ISO", help="pretend it is this UTC instant")
    c.set_defaults(func=_cmd_runner_tick)
    c = g.add_parser("reset-from", help="skip a gap above MAX_CATCHUP_BARS and resume at DATE")
    c.add_argument("portfolio")
    c.add_argument("date", type=_date, metavar="DATE")
    c.set_defaults(func=_cmd_runner_reset_from)

    g = group("backtest", "backtests")
    c = g.add_parser("run", help="run a strategy file against the cached data")
    c.add_argument("strategy_file")
    c.add_argument("--start", type=_date, required=True)
    c.add_argument("--end", type=_date)
    c.add_argument("--mode", choices=MODES, default="single")
    c.add_argument("--save", action="store_true", help="store a backtest_run row")
    c.set_defaults(func=_cmd_backtest_run)

    g = group("api", "the HTTP API")
    c = g.add_parser("serve", help="uvicorn on the configured host/port")
    c.add_argument("--host")
    c.add_argument("--port", type=int)
    c.add_argument("--reload", action="store_true")
    c.set_defaults(func=_cmd_api_serve)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - a CLI reports any failure as one line + exit 1
        return _err(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
