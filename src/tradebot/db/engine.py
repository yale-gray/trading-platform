"""SQLite engine factory: WAL, busy timeout, foreign keys, ``BEGIN IMMEDIATE`` (SPEC-004).

``session_scope`` is the only way the runner opens a write transaction: it starts with
``BEGIN IMMEDIATE`` (takes the write lock up front, so a bar transaction never deadlocks
with the API's writes), commits on success and rolls back on any exception, including
``BaseException`` (a simulated crash in tests).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlmodel import Session, SQLModel

from tradebot.db import models as _models  # noqa: F401  (registers the tables)
from tradebot.db.models import SCHEMA_VERSION, SchemaVersion

__all__ = ["create_all", "make_engine", "session_scope"]


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
    with session_scope(engine) as session:
        row = session.get(SchemaVersion, 1)
        if row is None:
            session.add(SchemaVersion(id=1, version=SCHEMA_VERSION))
        elif row.version != SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {row.version} != code version {SCHEMA_VERSION}; "
                "rebuild the database (no migrations in v1)"
            )


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
