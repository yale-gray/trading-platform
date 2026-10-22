"""DB engine, schema and repository tests (SPEC-004 T3, AC 13; ADR-0001 D7/D8)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.types import Float, Integer
from sqlmodel import SQLModel, col, select

from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.db.models import (
    SCHEMA_VERSION,
    EngineCheckpoint,
    LedgerEventRow,
    Portfolio,
    RunLog,
    SchemaVersion,
)

NOW = datetime(2025, 1, 1, 12, 0)
EXECUTION_TABLES = {
    "portfolio",
    "sleeve",
    "order",
    "ledger_event",
    "position_snapshot",
    "equity_snapshot",
    "engine_checkpoint",
    "kill_switch_state",
    "runner_state",
    "run_log",
}
MONEY_COLUMNS = {
    "initial_cash",
    "peak_equity",
    "cash",
    "qty",
    "qty_requested",
    "qty_accepted",
    "qty_filled",
    "ref_price",
    "fill_price",
    "fee",
    "price",
    "amount",
    "slippage_cost",
    "realized_pnl",
    "cost_basis",
    "market_value",
    "unrealized",
    "highest_close",
    "stop_level",
    "equity",
    "drawdown",
    "benchmark_equity",
    "trigger_drawdown",
}


def _portfolio(name: str = "p") -> Portfolio:
    return Portfolio(
        name=name,
        kind="fund",
        initial_cash="100000.00",
        started_at=datetime(2019, 1, 2),
        peak_equity="100000.00",
        config_json="{}",
        created_at=NOW,
    )


def test_pragmas_wal_fk_and_busy_timeout(db: Engine, db_path: Path) -> None:
    with db.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
    assert db_path.exists()


def test_no_money_column_is_numeric_and_no_minor_unit_names() -> None:
    """AC 13: money and quantity columns are TEXT; no *_cents / qty_e8 / price_e6."""
    for table in SQLModel.metadata.sorted_tables:
        for column in table.columns:
            assert not column.name.endswith(("_cents", "_e8", "_e6")), column.name
            if table.name in EXECUTION_TABLES and column.name in MONEY_COLUMNS:
                assert not isinstance(column.type, Integer | Float), (table.name, column.name)
                ddl = column.type.compile(dialect=sqlite.dialect())
                assert ddl in ("VARCHAR", "TEXT"), (table.name, column.name, ddl)


def test_decimal_text_roundtrip() -> None:
    for value in ("0", "100000.00", "-12.5", "0.00000001", "123456789.12345678"):
        d = Decimal(value)
        assert repo.text_to_dec(repo.dec_to_text(d)) == d
    assert repo.dec_to_text(Decimal("100000.00")) == "100000.00"


def test_decimal_survives_a_text_column(db: Engine) -> None:
    with session_scope(db) as s:
        p = _portfolio()
        p.initial_cash = repo.dec_to_text(Decimal("99999.99"))
        s.add(p)
    with session_scope(db) as s:
        got = repo.get_portfolio(s, "p")
        assert got is not None
        assert repo.text_to_dec(got.initial_cash) == Decimal("99999.99")


def test_session_scope_rolls_back_on_any_exception(db: Engine) -> None:
    class Boom(BaseException):
        pass

    with pytest.raises(Boom), session_scope(db) as s:
        s.add(_portfolio("gone"))
        s.flush()
        raise Boom
    with session_scope(db) as s:
        assert repo.get_portfolio(s, "gone") is None


def test_begin_immediate_blocks_a_second_writer(db_path: Path, db: Engine) -> None:
    other = make_engine(db_path)
    try:
        with session_scope(db) as s:
            s.add(_portfolio("w1"))
            s.flush()
            # a second writer cannot start while the first holds the write lock
            raw = other.raw_connection()
            cur = raw.cursor()
            cur.execute("PRAGMA busy_timeout=50")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                cur.execute("BEGIN IMMEDIATE")
            raw.close()
    finally:
        other.dispose()


def test_unique_constraints(db: Engine) -> None:
    with session_scope(db) as s:
        s.add(_portfolio("dup"))
    with pytest.raises(IntegrityError), session_scope(db) as s:
        s.add(_portfolio("dup"))

    with session_scope(db) as s:
        p = repo.get_portfolio(s, "dup")
        assert p is not None and p.id is not None
        pid = p.id
        s.add(
            LedgerEventRow(
                portfolio_id=pid,
                seq=1,
                sleeve_key="core",
                asof=datetime(2019, 1, 2).date(),
                stage="SUBMIT",
                type="DEPOSIT",
                amount="1.00",
                event_json="{}",
            )
        )
        s.add(RunLog(portfolio_id=pid, bar_ts=NOW, started_at=NOW, finished_at=NOW))
        s.add(
            EngineCheckpoint(
                portfolio_id=pid,
                bar_ts=NOW,
                state_json="{}",
                target_book_json="{}",
                ledger_seq=1,
                created_at=NOW,
            )
        )
    for dup in (
        LedgerEventRow(
            portfolio_id=pid,
            seq=1,
            sleeve_key="core",
            asof=datetime(2019, 1, 2).date(),
            stage="SUBMIT",
            type="DEPOSIT",
            amount="1.00",
            event_json="{}",
        ),
        RunLog(portfolio_id=pid, bar_ts=NOW, started_at=NOW, finished_at=NOW),
        EngineCheckpoint(
            portfolio_id=pid,
            bar_ts=NOW,
            state_json="{}",
            target_book_json="{}",
            ledger_seq=1,
            created_at=NOW,
        ),
    ):
        with pytest.raises(IntegrityError), session_scope(db) as s:
            s.add(dup)


def test_foreign_keys_are_enforced(db: Engine) -> None:
    with pytest.raises(IntegrityError), session_scope(db) as s:
        s.add(RunLog(portfolio_id=999, bar_ts=NOW, started_at=NOW, finished_at=NOW))


def test_checkpoint_pruning_keeps_newest_and_first_of_month(db: Engine) -> None:
    with session_scope(db) as s:
        p = _portfolio()
        s.add(p)
        s.flush()
        assert p.id is not None
        for day in range(1, 29):
            for month in (1, 2, 3):
                ts = datetime(2024, month, day)
                repo.write_checkpoint(s, p.id, ts, "{}", "{}", 0, NOW)
        deleted = repo.prune_checkpoints(s, p.id, keep=5)
        kept = list(s.exec(select(EngineCheckpoint.bar_ts).order_by(col(EngineCheckpoint.bar_ts))))
        assert deleted == 84 - len(kept)
        assert kept[-5:] == [datetime(2024, 3, d) for d in range(24, 29)]
        for month in (1, 2, 3):
            assert datetime(2024, month, 1) in kept
        latest = repo.latest_checkpoint(s, p.id)
        assert latest is not None and latest.bar_ts == datetime(2024, 3, 28)


def test_replace_checkpoint_overwrites_same_bar(db: Engine) -> None:
    with session_scope(db) as s:
        p = _portfolio()
        s.add(p)
        s.flush()
        assert p.id is not None
        repo.write_checkpoint(s, p.id, NOW, "a", "{}", 1, NOW)
        repo.replace_checkpoint(s, p.id, NOW, "b", "{}", 2, NOW)
        cp = repo.get_checkpoint(s, p.id, NOW)
        assert cp is not None and cp.state_json == "b" and cp.ledger_seq == 2


def test_create_all_is_idempotent_and_refuses_a_foreign_schema_version(db: Engine) -> None:
    create_all(db)
    with session_scope(db) as s:
        row = s.get(SchemaVersion, 1)
        assert row is not None and row.version == SCHEMA_VERSION
        row.version = SCHEMA_VERSION + 1
        s.add(row)
    with pytest.raises(RuntimeError, match="schema version"):
        create_all(db)


def test_tables_exist(db: Engine) -> None:
    names = set(inspect(db).get_table_names())
    assert EXECUTION_TABLES <= names
    assert {"strategy_config", "backtest_run", "runner_heartbeat", "kill_switch_event"} <= names
    with db.connect() as conn:
        assert conn.execute(text("select count(*) from portfolio")).scalar() == 0
