"""DB and runner fixtures for ``tests/platform`` (SPEC-004 T11/T12, ADR-0001 A4)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from tradebot.data import MarketData
from tradebot.db import create_all, make_engine


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    """File-based SQLite (WAL) in ``tmp_path``; disposed so no connection leaks."""
    engine = make_engine(tmp_path / "runner.db")
    create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "runner.db"


@pytest.fixture
def data(market_cache: Path) -> MarketData:
    from runner_support import make_data

    return make_data(market_cache)
