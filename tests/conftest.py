"""Shared test configuration: offline guard, Hypothesis profiles, isolated paths, fixtures.

Rules enforced here (SPEC-005 section 2): no test touches the network, the wall clock,
or the user's real cache/DB. Fixtures are synthetic and committed under ``tests/fixtures``.
"""

from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import settings
from support.netguard import NetworkAccessError

if TYPE_CHECKING:
    from tradebot.clock import Clock


def _blocked(*_args: object, **_kwargs: object) -> None:
    raise NetworkAccessError(
        f"network access blocked in tests: {os.environ.get('PYTEST_CURRENT_TEST', '<unknown>')}"
    )


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guarded_connect(self: socket.socket, address: object) -> None:
    if self.family == socket.AF_UNIX:
        _real_connect(self, address)  # type: ignore[arg-type]
        return
    _blocked()


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    if self.family == socket.AF_UNIX:
        return _real_connect_ex(self, address)  # type: ignore[arg-type]
    _blocked()
    return 0  # pragma: no cover - _blocked always raises


# Hypothesis profiles (SPEC-005 section 1). CI sets HYPOTHESIS_PROFILE=ci.
settings.register_profile("ci", max_examples=200, deadline=None, derandomize=True, print_blob=True)
settings.register_profile("dev", max_examples=50, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(scope="session", autouse=True)
def _block_network() -> Iterator[None]:
    """Make every outbound network entry point raise NetworkAccessError for the whole session."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", _guarded_connect)
        mp.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
        mp.setattr(socket, "create_connection", _blocked)
        mp.setattr(socket, "getaddrinfo", _blocked)
        mp.setattr(socket, "gethostbyname", _blocked)
        mp.setattr(socket, "gethostbyname_ex", _blocked)
        mp.setattr(socket, "gethostbyaddr", _blocked)

        try:
            import yfinance
        except ImportError:
            pass
        else:
            mp.setattr(yfinance, "download", _blocked)
            mp.setattr(yfinance, "Ticker", _blocked)

        try:
            import ccxt
        except ImportError:
            pass
        else:
            mp.setattr(ccxt.Exchange, "fetch", _blocked)

        yield


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point cache and DB settings at tmp_path so no test touches the user's real data."""
    monkeypatch.setenv("TRADEBOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TRADEBOT_DB_PATH", str(tmp_path / "tradebot.db"))


@pytest.fixture(scope="session")
def fixtures_root() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def market_cache(fixtures_root: Path, tmp_path: Path) -> Path:
    """Writable copy of the clean synthetic market cache."""
    dest = tmp_path / "market"
    shutil.copytree(fixtures_root / "market", dest)
    return dest


@pytest.fixture
def edge_cache(fixtures_root: Path, tmp_path: Path) -> Path:
    """Writable copy of the edge-case synthetic market cache."""
    dest = tmp_path / "market_edge"
    shutil.copytree(fixtures_root / "market_edge", dest)
    return dest


@pytest.fixture
def fixed_clock() -> Clock:
    from tradebot.clock import FixedClock  # lazy: clock.py may be written by another task

    return FixedClock(datetime(2024, 12, 31, 23, 0, tzinfo=UTC))
