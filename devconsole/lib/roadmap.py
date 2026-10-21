"""Parse docs/ROADMAP.md into per-section progress counts. Pure; no streamlit."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^##(?!#)\s+(.+?)\s*$")
_CHECKBOX = re.compile(r"^\s*[-*]\s+\[([ xX~])\]\s*(.*?)\s*$")
_PAREN_TAIL = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_STATE = {"x": "x", "X": "x", "~": "~", " ": " "}


@dataclass(frozen=True)
class RoadmapSection:
    title: str
    status_hint: str  # "" or the trailing parenthetical, e.g. "done 2026-10-20", "next"
    done: int
    in_progress: int
    todo: int
    items: tuple[tuple[str, str], ...]  # (state "x" | "~" | " ", text)

    @property
    def total(self) -> int:
        return self.done + self.in_progress + self.todo

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 0.0


def _split_heading(text: str) -> tuple[str, str]:
    match = _PAREN_TAIL.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return text.strip(), ""


def parse_roadmap(text: str) -> list[RoadmapSection]:
    """Every '## ' heading that holds at least one checkbox line, in document order.

    A checkbox counts only when it starts the line (after an optional bullet).
    Checkboxes inside fenced code blocks are ignored.
    """
    sections: list[RoadmapSection] = []
    heading: str | None = None
    items: list[tuple[str, str]] = []
    in_fence = False

    def flush() -> None:
        if heading is None or not items:
            return
        title, hint = _split_heading(heading)
        states = [state for state, _ in items]
        sections.append(
            RoadmapSection(
                title=title,
                status_hint=hint,
                done=states.count("x"),
                in_progress=states.count("~"),
                todo=states.count(" "),
                items=tuple(items),
            )
        )

    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading_match = _HEADING.match(line)
        if heading_match:
            flush()
            heading = heading_match.group(1)
            items = []
            continue
        box = _CHECKBOX.match(line)
        if box and heading is not None:
            items.append((_STATE[box.group(1)], box.group(2)))
    flush()
    return sections
