"""Parse audit reports (docs/reviews/*.md) and the process log table. Pure; no streamlit.

Never raises on odd input: unknown formats give UNKNOWN verdicts or empty results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

UNKNOWN = "UNKNOWN"
TEMPLATE_NAME = "_TEMPLATE.md"

_VERDICT = re.compile(r"\*\*Verdict:\s*(PASS-WITH-NOTES|PASS|FAIL)\.?\*\*")
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_DATE_LINE = re.compile(r"^\s*[-*]\s+\*\*Date:\*\*\s*(.*)$", re.MULTILINE)
_CODE_LINE = re.compile(r"^\s*[-*]\s+\*\*Code:\*\*\s*(.*)$", re.MULTILINE)
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


@dataclass(frozen=True)
class ReviewSummary:
    file: str
    title: str
    scope: str
    date: str  # ISO "YYYY-MM-DD" or ""
    initial_verdict: str  # "PASS" | "PASS-WITH-NOTES" | "FAIL" | "UNKNOWN"
    latest_verdict: str
    reaudited: bool


def _first(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def parse_review(file: str, text: str) -> ReviewSummary:
    """Initial verdict is the first '**Verdict: X**' in the file (the header); the latest
    is the last one (a re-audit section supersedes it)."""
    text = text or ""
    title = _first(_H1, text)
    code = _first(_CODE_LINE, text)
    date_value = _first(_DATE_LINE, text)
    date_match = _ISO_DATE.search(date_value)
    verdicts = _VERDICT.findall(text)
    initial = verdicts[0] if verdicts else UNKNOWN
    latest = verdicts[-1] if verdicts else UNKNOWN
    return ReviewSummary(
        file=file,
        title=title,
        scope=code or title,
        date=date_match.group(1) if date_match else "",
        initial_verdict=initial,
        latest_verdict=latest,
        reaudited=len(verdicts) > 1,  # a later verdict sits at a different position
    )


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    # Split on unescaped pipes only, then unescape "\|" inside cells.
    cells = re.split(r"(?<!\\)\|", body)
    return [cell.replace("\\|", "|").strip() for cell in cells]


def parse_process_log(text: str) -> pd.DataFrame:
    """First markdown table with a header row and a '---' separator row.

    Columns come from the header row; rows are padded or truncated to its width.
    Returns an empty DataFrame when no such table exists.
    """
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1):
        header_line, sep_line = lines[i].strip(), lines[i + 1].strip()
        if not (header_line.startswith("|") and sep_line.startswith("|")):
            continue
        sep_cells = _split_row(sep_line)
        if not sep_cells or not all(_SEPARATOR_CELL.match(c) for c in sep_cells):
            continue
        columns = _split_row(header_line)
        rows: list[list[str]] = []
        for row_line in lines[i + 2 :]:
            if not row_line.strip().startswith("|"):
                break
            cells = _split_row(row_line)
            cells = (cells + [""] * len(columns))[: len(columns)]
            rows.append(cells)
        return pd.DataFrame(rows, columns=columns)
    return pd.DataFrame()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def list_reviews(reviews_dir: Path) -> list[ReviewSummary]:
    """Summaries for every *.md report except _TEMPLATE.md, newest date first.

    A missing directory gives []. Unreadable files are listed with UNKNOWN verdicts.
    """
    try:
        paths = sorted(p for p in reviews_dir.glob("*.md") if p.name != TEMPLATE_NAME)
    except OSError:
        return []
    summaries = [parse_review(p.name, _read(p)) for p in paths if p.is_file()]
    return sorted(summaries, key=lambda s: (s.date, s.file), reverse=True)
