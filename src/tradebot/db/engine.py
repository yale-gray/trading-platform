"""SQLite engine factory: WAL, busy timeout, foreign keys, ``BEGIN IMMEDIATE`` (SPEC-004).

``session_scope`` is the only way the runner opens a write transaction: it starts with
``BEGIN IMMEDIATE`` (takes the write lock up front, so a bar transaction never deadlocks
with the API's writes), commits on success and rolls back on any exception, including
``BaseException`` (a simulated crash in tests).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlmodel import Session, SQLModel

from tradebot.db import models as _models  # noqa: F401  (registers the tables)
from tradebot.db.models import SCHEMA_VERSION, SchemaVersion

__all__ = ["create_all", "make_engine", "migrate_asof", "session_scope"]


def make_engine(db_path: str | Path, *, echo: bool = False) -> Engine:
    """Create a SQLite engine for ``db_path`` (file path or ``":memory:"``).

    Every connection gets ``journal_mode=WAL``, ``busy_timeout=5000`` and
    ``foreign_keys=ON``; transactions start with ``BEGIN IMMEDIATE``. Call
    ``engine.dispose()`` when done (tests must, ``filterwarnings = error``).
    """
    path = str(db_path)
    if path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        path = str(Path(path).expanduser())
    engine = create_engine(
        f"sqlite:///{path}", echo=echo, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None  # we emit BEGIN ourselves (see _on_begin)
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_all(engine: Engine) -> None:
    """Create every table and stamp the schema version.

    There is no Alembic in v1: a breaking schema change means rebuilding the DB from the
    ledger or re-running the backtests. A file stamped with another version is refused.
    """
    SQLModel.metadata.create_all(engine)
    migrate_asof(engine)
    with session_scope(engine) as session:
        row = session.get(SchemaVersion, 1)
        if row is None:
            session.add(SchemaVersion(id=1, version=SCHEMA_VERSION))
        elif row.version != SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {row.version} != code version {SCHEMA_VERSION}; "
                "rebuild the database (no migrations in v1)"
            )


# (table, asof column, JSON column, key in the JSON): every column that can hold a 15m bar open.
_ASOF_COLUMNS = (
    ("ledger_event", "asof", "event_json", "asof"),
    ("order", "created_asof", "order_json", "created_asof"),
    ("order", "fill_asof", "order_json", "fill_asof"),
)


def _utc_naive_iso(raw: str) -> str:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def migrate_asof(engine: Engine) -> int:
    """Restore full 15m timestamps in ``asof`` columns written by the old date-only schema.

    Before ADR-0001 A21 follow-up 2 the ledger/order ``asof`` columns were ``Date`` and silently
    truncated a 15m bar open to its day; the lossless value stayed in ``event_json`` /
    ``order_json``. The new ``AsOfType`` stores the timestamp as text, so no table rebuild is
    needed (SQLite ignores the declared ``DATE``): this rewrites the date-only cells whose JSON
    carries a time. Daily rows (JSON holds a plain date) are not touched. Idempotent: a repaired
    cell is longer than 10 characters and is skipped next time. Returns the cells repaired.
    """
    fixed = 0
    with session_scope(engine) as session:
        for table, col, json_col, key in _ASOF_COLUMNS:
            rows = (
                session.connection()
                .exec_driver_sql(
                    f'SELECT id, "{json_col}" FROM "{table}" '
                    f'WHERE length("{col}") = 10 '
                    f"AND json_extract(\"{json_col}\", '$.{key}') LIKE '%T%'"
                )
                .all()
            )
            for row_id, blob in rows:
                raw = json.loads(blob).get(key)
                if raw:
                    session.connection().execute(
                        text(f'UPDATE "{table}" SET "{col}" = :v WHERE id = :i'),
                        {"v": _utc_naive_iso(raw), "i": row_id},
                    )
                    fixed += 1
    return fixed


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """One transaction: ``BEGIN IMMEDIATE`` ... ``COMMIT``, or ``ROLLBACK`` on any exception."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
