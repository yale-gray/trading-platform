"""``--update-golden`` option for the golden tests (SPEC-005 section 5).

Usage::

    uv run pytest tests/golden --update-golden --golden-note=docs/reviews/golden/<date>-<id>.md

Both are required, and the note file must already exist; otherwise the harness refuses to write.
The option is only registered when ``tests/golden`` is on the command line (or the cwd), which
is exactly when it is used.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("golden")
    group.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/golden/G*.json from the current code (needs --golden-note)",
    )
    group.addoption(
        "--golden-note",
        action="store",
        default=None,
        help="path of the existing PM-approval note that justifies the update",
    )


@pytest.fixture(scope="session")
def update_golden(request: pytest.FixtureRequest) -> Path | None:
    """The note path when an update is requested (and valid), else ``None``."""
    if not request.config.getoption("--update-golden"):
        return None
    note = request.config.getoption("--golden-note")
    if not note or not Path(note).is_file():
        raise pytest.UsageError("--update-golden needs --golden-note=<existing note file>")
    return Path(note)
