"""FastAPI dependencies: the DB session and shared app state."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlmodel import Session

from tradebot.config import Settings
from tradebot.db.engine import session_scope


def get_db(request: Request) -> Iterator[Session]:
    """One transaction per request (reads and the ack write share it)."""
    with session_scope(request.app.state.engine) as session:
        yield session


def get_settings(request: Request) -> Settings:
    """The app's settings."""
    return request.app.state.settings  # type: ignore[no-any-return]


Db = Annotated[Session, Depends(get_db)]
"""Request-scoped DB session parameter type."""
