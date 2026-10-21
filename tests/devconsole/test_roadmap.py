"""Unit tests for devconsole/lib/roadmap.py: ROADMAP.md progress parsing (SPEC-006 AC 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from devconsole.lib.roadmap import RoadmapSection, parse_roadmap

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]

# Hand count of SAMPLE:
#   "Wave 1 (done 2026-10-01)": 3 items, [x] x2 and [X] x1 -> done 3, todo 0, total 3
#   "Wave 2 (next)": [~] x1, [ ] x2 -> in_progress 1, todo 2, total 3
#   "Wave 3": no checkboxes at all -> not reported
#   "Notes": a checkbox inside a fenced block is ignored; a plain bullet is not a checkbox
#   "### Sub": a level-3 heading is not a section, so its checkbox belongs to "Wave 2 (next)"
#   "Wave 4 (done)": "*" bullets, [x] x1 -> done 1, total 1
SAMPLE = """\
# Roadmap

## Wave 1 (done 2026-10-01)
- [x] Data layer
* [x] Indicators
- [X] Simulator

## Wave 2 (next)
- [~] Engine in progress
- [ ] Backtester
### Sub
  - [ ] Nested item still belongs to Wave 2

## Wave 3
Just prose, no checkboxes here.

## Notes
```
- [x] fenced example, must be ignored
```
- plain bullet, not a checkbox
- [ ] checkbox after the fence still counts

## Wave 4 (done)
* [x] Only item
"""


def _by_title(sections: list[RoadmapSection]) -> dict[str, RoadmapSection]:
    return {s.title: s for s in sections}


def test_sample_sections_appear_in_document_order_without_empty_ones() -> None:
    titles = [s.title for s in parse_roadmap(SAMPLE)]
    assert titles == ["Wave 1", "Wave 2", "Notes", "Wave 4"]


def test_status_hint_is_the_trailing_parenthetical() -> None:
    sections = _by_title(parse_roadmap(SAMPLE))
    assert sections["Wave 1"].status_hint == "done 2026-10-01"
    assert sections["Wave 2"].status_hint == "next"
    assert sections["Notes"].status_hint == ""


def test_counts_match_hand_count() -> None:
    wave1 = _by_title(parse_roadmap(SAMPLE))["Wave 1"]
    assert (wave1.done, wave1.in_progress, wave1.todo, wave1.total) == (3, 0, 0, 3)
    assert wave1.fraction == 1.0


def test_in_progress_and_todo_counts_and_subheading_item() -> None:
    wave2 = _by_title(parse_roadmap(SAMPLE))["Wave 2"]
    # [~] Engine, [ ] Backtester, [ ] Nested item under ### Sub
    assert (wave2.done, wave2.in_progress, wave2.todo, wave2.total) == (0, 1, 2, 3)
    assert wave2.fraction == 0.0
    assert wave2.items == (
        ("~", "Engine in progress"),
        (" ", "Backtester"),
        (" ", "Nested item still belongs to Wave 2"),
    )


def test_fence_ignored_and_plain_bullet_not_counted() -> None:
    notes = _by_title(parse_roadmap(SAMPLE))["Notes"]
    # Only the checkbox after the closing fence counts; the fenced [x] does not.
    assert notes.items == ((" ", "checkbox after the fence still counts"),)
    assert (notes.done, notes.todo) == (0, 1)


def test_star_bullets_and_single_item_section() -> None:
    wave4 = _by_title(parse_roadmap(SAMPLE))["Wave 4"]
    assert (wave4.done, wave4.total, wave4.fraction) == (1, 1, 1.0)


def test_empty_input_gives_no_sections() -> None:
    assert parse_roadmap("") == []


def test_text_without_checkboxes_gives_no_sections() -> None:
    assert parse_roadmap("# Title\n\n## Only prose\nNothing to count.\n") == []


def test_fraction_is_zero_when_total_is_zero() -> None:
    empty = RoadmapSection(title="x", status_hint="", done=0, in_progress=0, todo=0, items=())
    assert empty.total == 0
    assert empty.fraction == 0.0


def test_real_roadmap_parses_with_non_negative_counts() -> None:
    """Loose check against the live docs/ROADMAP.md (wording may change)."""
    sections = parse_roadmap((_ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8"))
    assert len(sections) >= 2
    for section in sections:
        assert min(section.done, section.in_progress, section.todo) >= 0
        assert section.total == section.done + section.in_progress + section.todo
