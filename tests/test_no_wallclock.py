"""Grep guard: production code reads time only through the injected Clock (SPEC-005 section 2)."""

from __future__ import annotations

from pathlib import Path

WALLCLOCK_PATTERNS = ("datetime.now(", "datetime.utcnow(", "time.time(", "date.today(")
SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "tradebot"


def find_wallclock_calls(root: Path) -> list[tuple[Path, int, str]]:
    """Return (file, line number, stripped line) for each wall-clock call under ``root``.

    Files named ``clock.py`` are skipped, and comment lines are ignored.
    """
    hits: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "clock.py":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if any(pattern in line for pattern in WALLCLOCK_PATTERNS):
                hits.append((path, lineno, stripped))
    return hits


def test_each_wallclock_pattern_is_detected(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "a = datetime.now()\nb = datetime.utcnow()\nc = time.time()\nd = date.today()\n",
        encoding="utf-8",
    )
    hits = find_wallclock_calls(tmp_path)
    assert [lineno for _, lineno, _ in hits] == [1, 2, 3, 4]


def test_clock_module_and_comment_lines_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "clock.py").write_text("x = datetime.now()\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text(
        "# datetime.now()\n    # time.time()\n# date.today()\n",
        encoding="utf-8",
    )
    assert find_wallclock_calls(tmp_path) == []


def test_real_source_tree_has_no_wallclock_calls() -> None:
    assert find_wallclock_calls(SRC_ROOT) == []
