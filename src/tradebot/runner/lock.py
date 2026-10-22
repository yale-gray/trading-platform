"""Single-runner lock: an exclusive ``fcntl.flock`` on ``runner.lock`` next to the database.

The scheduler holds it for its whole life; the CLI ``reset-kill-switch`` / ``reset-from``
commands call :func:`is_locked` and refuse while a runner is live.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import IO

from tradebot.config import Settings

__all__ = ["RunnerLock", "RunnerLockedError", "is_locked", "lock_path"]


class RunnerLockedError(RuntimeError):
    """Another runner process holds the lock."""


def lock_path(settings: Settings) -> Path:
    """``runner.lock`` in the same directory as the database file."""
    return Path(settings.db_path).with_name("runner.lock")


class RunnerLock:
    """Non-blocking exclusive file lock (released on ``release`` or process death)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        """Take the lock or raise :class:`RunnerLockedError`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 - held for the lock's lifetime
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise RunnerLockedError(f"runner lock {self.path} is held by another process") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        """Release the lock (no-op if not held)."""
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> RunnerLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def is_locked(path: Path) -> bool:
    """True if some other holder has the lock right now (probes and releases)."""
    probe = RunnerLock(path)
    try:
        probe.acquire()
    except RunnerLockedError:
        return True
    probe.release()
    return False
