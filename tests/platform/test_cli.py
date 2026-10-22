"""Unified ``tradebot`` CLI (SPEC-004 T13, AC 15 CLI half, AC 16) via ``main([...])``."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from runner_support import DATA_START, START, SYNTH_NOW, after_close, make_data, make_runner, seed
from sqlalchemy import Engine
from sqlmodel import select

from tradebot import cli
from tradebot.config import Settings
from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.db.models import BacktestRun, KillSwitchEvent, LedgerEventRow, RunLog
from tradebot.portfolio.config import default_config
from tradebot.risk.config import RiskConfig
from tradebot.runner import benchmark_definition, fund_definition, restore_context
from tradebot.runner.lock import RunnerLock
from tradebot.strategy.io import STRATEGIES_DIR

NOW = after_close(date(2019, 3, 1))


@pytest.fixture
def offline(market_cache: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's data service at the offline synthetic cache and freeze its clock."""
    monkeypatch.setattr(cli, "_market_data", lambda _s: make_data(market_cache))
    monkeypatch.setattr(cli, "_now", lambda: SYNTH_NOW)
    return market_cache


@pytest.fixture
def cli_db() -> Iterator[Engine]:
    """The DB the CLI will open (``TRADEBOT_DB_PATH`` from the autouse isolation fixture)."""
    engine = make_engine(Settings().db_path)
    create_all(engine)
    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------- AC 16


def test_help_needs_only_base_dependencies() -> None:
    code = (
        "import sys\n"
        "from tradebot.cli import main\n"
        "try:\n    main(['--help'])\nexcept SystemExit as e:\n    assert e.code == 0\n"
        "try:\n    main(['portfolio', '--help'])\nexcept SystemExit as e:\n    assert e.code == 0\n"
        "bad = [m for m in ('fastapi', 'uvicorn', 'apscheduler', 'streamlit', 'plotly', 'httpx', "
        "'pandas') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr


def test_command_tree_has_every_documented_command() -> None:
    parser = cli.build_parser()
    for argv in (
        ["data", "refresh"], ["data", "verify"], ["data", "rebase", "SPY", "--yes"],
        ["db", "init"], ["portfolio", "create", "--preset", "fund"], ["portfolio", "list"],
        ["portfolio", "reset-kill-switch", "p", "--note", "n"], ["runner", "start"],
        ["runner", "tick", "--once", "--now", "2025-01-01T00:00:00"],
        ["runner", "reset-from", "p", "2025-01-01"],
        ["backtest", "run", "f.yaml", "--start", "2019-01-02"],
        ["api", "serve", "--reload"],
    ):  # fmt: skip
        assert callable(parser.parse_args(argv).func), argv


def test_pyproject_registers_the_script() -> None:
    text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    assert 'tradebot = "tradebot.cli:main"' in text


def test_bad_arguments_exit_nonzero() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["portfolio", "create", "--preset", "nope"])
    assert exc.value.code == 2
    assert cli.main(["portfolio", "create"]) == 1  # neither --preset nor --config


# ---------------------------------------------------------------------------- data


def test_data_verify_and_refresh_report_the_cache(offline: Path, capsys: Any) -> None:
    assert cli.main(["data", "verify", "--symbols", "SPY", "BTC/USD"]) == 0
    out = capsys.readouterr().out
    assert "SPY" in out and "BTC/USD" in out and "ok" in out
    assert cli.main(["data", "refresh", "--symbols", "SPY"]) == 0  # offline: cache report only


def test_data_verify_fails_on_a_problem(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from tradebot.data.quality import QualityReport

    class Fake:
        def verify(self, _s: object) -> dict[str, QualityReport]:
            return {"SPY": QualityReport("SPY", 0, status="corrupt")}

    monkeypatch.setattr(cli, "_market_data", lambda _s: Fake())
    assert cli.main(["data", "verify"]) == 1
    assert "corrupt" in capsys.readouterr().out


def test_data_rebase_confirms_unless_yes(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    calls: list[str] = []

    class Fake:
        def rebase(self, symbol: str) -> int:
            calls.append(symbol)
            return 7

    monkeypatch.setattr(cli, "_market_data", lambda _s: Fake())
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    assert cli.main(["data", "rebase", "SPY"]) == 1 and calls == []
    assert cli.main(["data", "rebase", "SPY", "--yes"]) == 0 and calls == ["SPY"]
    assert "new generation 7" in capsys.readouterr().out


# ---------------------------------------------------------------------------- db, portfolio


def test_db_init_and_portfolio_create_list(offline: Path, capsys: Any) -> None:
    assert cli.main(["db", "init"]) == 0
    assert cli.main(["portfolio", "create", "--preset", "bench_spy", "--start", "2019-01-02"]) == 0
    assert cli.main(["portfolio", "create", "--preset", "bench_btc"]) == 0  # latest completed bar
    assert cli.main(["portfolio", "create", "--preset", "fund", "--name", "f2"]) == 0
    assert cli.main(["portfolio", "create", "--preset", "bench_spy"]) == 1  # name exists
    assert "already exists" in capsys.readouterr().err
    capsys.readouterr()
    assert cli.main(["portfolio", "list"]) == 0
    out = capsys.readouterr().out
    for name in ("bench_spy", "bench_btc", "f2"):
        assert name in out
    engine = make_engine(Settings().db_path)
    try:
        with session_scope(engine) as s:
            starts = {p.name: p.started_at.date() for p in repo.list_portfolios(s)}
    finally:
        engine.dispose()
    assert starts["bench_spy"] == date(2019, 1, 2)
    assert starts["bench_btc"] == date(2024, 12, 31)  # SYNTH_NOW is 2025-01-01 12:00 UTC
    assert starts["f2"] == date(2024, 12, 31)


def test_portfolio_create_from_config_file(
    offline: Path, tmp_path: Path, cli_db: Engine, capsys: Any
) -> None:
    cfg = tmp_path / "def.json"
    cfg.write_text(benchmark_definition("SPY", date(2019, 1, 2), data_start=DATA_START).to_json())
    assert cli.main(["portfolio", "create", "--config", str(cfg), "--name", "custom"]) == 0
    with session_scope(cli_db) as s:
        p = repo.get_portfolio(s, "custom")
        assert p is not None and p.kind == "benchmark" and p.started_at.date() == date(2019, 1, 2)


# ---------------------------------------------------------------------------- runner


def test_runner_tick_processes_due_bars_then_noops(
    offline: Path, cli_db: Engine, capsys: Any
) -> None:
    assert cli.main(["portfolio", "create", "--preset", "bench_spy", "--start", "2019-01-02"]) == 0
    stamp = after_close(date(2019, 1, 4)).isoformat()
    assert cli.main(["runner", "tick", "--once", "--now", stamp]) == 0
    assert "bench_spy: ok (3 bars)" in capsys.readouterr().out
    assert cli.main(["runner", "tick", "--now", stamp]) == 0
    assert "noop" in capsys.readouterr().out


def test_runner_tick_refuses_while_the_runner_lock_is_held(
    offline: Path, cli_db: Engine, capsys: Any
) -> None:
    from tradebot.runner.lock import lock_path

    with RunnerLock(lock_path(Settings())):
        assert cli.main(["runner", "tick"]) == 1
    assert "runner.lock" in capsys.readouterr().err


# ---------------------------------------------------------------------------- AC 15 (CLI half)


@pytest.fixture
def spy(cli_db: Engine, offline: Path) -> Engine:
    seed(cli_db, [benchmark_definition("SPY", START, data_start=DATA_START)])
    make_runner(cli_db, make_data(offline)).tick(after_close(date(2019, 1, 10)))
    return cli_db


@pytest.fixture
def tripped(cli_db: Engine, offline: Path) -> Engine:
    hair = default_config().model_copy(
        update={"risk": RiskConfig(kill_switch_drawdown=Decimal("0.0005"))}
    )
    seed(cli_db, [fund_definition(START, data_start=DATA_START, engine=hair, benchmark=None)])
    make_runner(cli_db, make_data(offline)).tick(NOW)
    return cli_db


def test_reset_kill_switch_refuses_while_the_runner_lock_is_held(
    tripped: Engine, capsys: Any
) -> None:
    from tradebot.runner.lock import lock_path

    argv = ["portfolio", "reset-kill-switch", "paper_fund", "--note", "reviewed", "--yes"]
    with RunnerLock(lock_path(Settings())):
        assert cli.main(argv) == 1
    assert "runner" in capsys.readouterr().err
    with session_scope(tripped) as s:
        p = repo.get_portfolio(s, "paper_fund")
        assert p is not None and p.id is not None
        ks = repo.get_kill_switch(s, p.id)
        assert ks is not None and ks.tripped
        assert [e.kind for e in s.exec(select(KillSwitchEvent))] == ["trip"]


def test_reset_kill_switch_writes_one_reset_event_and_a_fresh_checkpoint(
    tripped: Engine, offline: Path
) -> None:
    with session_scope(tripped) as s:
        p = repo.get_portfolio(s, "paper_fund")
        assert p is not None and p.id is not None
        assert repo.acknowledge_kill_switch(s, p.id, "seen", repo.naive_utc(NOW))  # API-style ack
        n_before = repo.ledger_count(s, p.id)
    argv = ["portfolio", "reset-kill-switch", "paper_fund", "--note", "reviewed", "--yes"]
    assert cli.main(argv) == 0
    with session_scope(tripped) as s:
        p = repo.get_portfolio(s, "paper_fund")
        assert p is not None and p.id is not None
        ks = repo.get_kill_switch(s, p.id)
        assert ks is not None and not ks.tripped
        assert [e.kind for e in s.exec(select(KillSwitchEvent))] == ["trip", "ack", "reset"]
        assert repo.ledger_count(s, p.id) == n_before + 1
        last = s.exec(
            select(LedgerEventRow)
            .where(LedgerEventRow.portfolio_id == p.id)
            .order_by(LedgerEventRow.seq.desc())  # type: ignore[attr-defined]
        ).first()
        assert last is not None and last.type == "KILL_SWITCH_RESET"
        assert not restore_context(s, p).engine.snapshot().killed
    # the lock was released: the runner can continue from the reset state
    res = make_runner(tripped, make_data(offline)).tick(after_close(date(2019, 3, 3)))
    assert res[0].status == "ok"


def test_reset_of_an_untripped_switch_is_refused(spy: Engine, capsys: Any) -> None:
    argv = ["portfolio", "reset-kill-switch", "bench_spy", "--note", "n", "--yes"]
    assert cli.main(argv) == 1
    assert "not tripped" in capsys.readouterr().err


def test_reset_kill_switch_aborts_without_confirmation(
    tripped: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: "no")
    assert cli.main(["portfolio", "reset-kill-switch", "paper_fund", "--note", "n"]) == 1
    with session_scope(tripped) as s:
        p = repo.get_portfolio(s, "paper_fund")
        assert p is not None and p.id is not None
        ks = repo.get_kill_switch(s, p.id)
        assert ks is not None and ks.tripped


# ---------------------------------------------------------------------------- reset-from


def test_reset_from_skips_the_gap_and_resumes_at_date(
    spy: Engine, offline: Path, capsys: Any
) -> None:
    assert cli.main(["runner", "reset-from", "bench_spy", "2019-01-22"]) == 0
    assert "next bar processed is 2019-01-22" in capsys.readouterr().out
    with session_scope(spy) as s:
        p = repo.get_portfolio(s, "bench_spy")
        assert p is not None and p.id is not None
        rs = repo.get_runner_state(s, p.id)
        assert rs.last_processed_bar_ts is not None and rs.last_processed_bar_ts.date() == date(
            2019, 1, 21
        )
        log = s.exec(select(RunLog).where(RunLog.outcome == "reset")).one()
        assert log.bar_ts.date() == date(2019, 1, 21)
        ctx = restore_context(s, p)  # marker, checkpoint and ledger agree
        assert not [o for o in ctx.engine.orders() if o.status.value == "PENDING"]
    res = make_runner(spy, make_data(offline)).tick(after_close(date(2019, 1, 23)))
    assert res[0].status == "ok" and res[0].processed == [date(2019, 1, 22), date(2019, 1, 23)]


def test_reset_from_rejects_a_date_in_the_past_and_unknown_names(spy: Engine, capsys: Any) -> None:
    assert cli.main(["runner", "reset-from", "bench_spy", "2019-01-10"]) == 1
    assert "after the last processed bar" in capsys.readouterr().err
    capsys.readouterr()
    assert cli.main(["runner", "reset-from", "bench_spy", "2099-01-01"]) == 1
    assert "future" in capsys.readouterr().err
    assert cli.main(["runner", "reset-from", "nope", "2019-02-01"]) == 1
    from tradebot.runner.lock import lock_path

    with RunnerLock(lock_path(Settings())):
        assert cli.main(["runner", "reset-from", "bench_spy", "2019-02-01"]) == 1


# ---------------------------------------------------------------------------- backtest, api


def test_backtest_run_prints_metrics_and_saves(offline: Path, cli_db: Engine, capsys: Any) -> None:
    argv = [
        "backtest", "run", str(STRATEGIES_DIR / "core_momentum.yaml"),
        "--start", "2019-01-02", "--end", "2019-06-28", "--save",
    ]  # fmt: skip
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "total_return" in out and "SPY total_return" in out and "saved as backtest run" in out
    with session_scope(cli_db) as s:
        run = s.exec(select(BacktestRun)).one()
        assert run.status == "done" and run.benchmark_symbol == "SPY" and run.result_json


def test_api_serve_starts_uvicorn_on_the_configured_host(
    cli_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    assert cli.main(["api", "serve", "--port", "8123"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8123
    assert seen["app"].title == "tradebot"
