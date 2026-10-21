"""A golden file may only change with a PM-approved note listing its hash (SPEC-005 section 5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from golden import harness as h

pytestmark = pytest.mark.golden


def test_every_golden_hash_is_listed_in_an_approved_note() -> None:
    assert sorted(p.name for p in h.GOLDEN_DIR.glob("G*.json")) == [f"{g}.json" for g in h.IDS]
    assert h.unapproved_goldens() == [], "golden edited without a PM-approved note (golden-sha256)"


def _copy_goldens(dest: Path) -> None:
    dest.mkdir()
    for p in h.GOLDEN_DIR.glob("G*.json"):
        (dest / p.name).write_bytes(p.read_bytes())


def _note(path: Path, golden_dir: Path, *, approved: bool) -> None:
    hashes = " ".join(f"{p.stem}={h.file_sha256(p)}" for p in sorted(golden_dir.glob("G*.json")))
    lines = ["# Golden change test", f"golden-sha256: {hashes}"]
    if approved:
        lines.append("Approved-by: PM 2026-10-21")
    path.write_text("\n".join(lines) + "\n")


def test_hand_edited_golden_fails_the_check(tmp_path: Path) -> None:
    goldens, notes = tmp_path / "g", tmp_path / "n"
    _copy_goldens(goldens)
    notes.mkdir()
    _note(notes / "ok.md", goldens, approved=True)
    assert h.unapproved_goldens(goldens, notes) == []
    (goldens / "G2.json").write_text((goldens / "G2.json").read_text() + " ")
    assert h.unapproved_goldens(goldens, notes) == ["G2.json"]


def test_note_without_approval_does_not_count(tmp_path: Path) -> None:
    goldens, notes = tmp_path / "g", tmp_path / "n"
    _copy_goldens(goldens)
    notes.mkdir()
    _note(notes / "draft.md", goldens, approved=False)
    assert h.unapproved_goldens(goldens, notes) == sorted(p.name for p in goldens.glob("G*.json"))
