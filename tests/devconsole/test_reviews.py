"""Unit tests for devconsole/lib/reviews.py: audit verdicts and the process log (SPEC-006 AC 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from devconsole.lib.reviews import (
    UNKNOWN,
    list_reviews,
    parse_process_log,
    parse_review,
)

pytestmark = pytest.mark.unit

# An audit whose header says PASS-WITH-NOTES, followed by a re-audit section that says PASS.
REAUDITED = """\
# Audit: Data quality

- **Date:** 2026-10-01
- **Code:** src/tradebot/data/quality.py

**Verdict: PASS-WITH-NOTES.**

Findings listed above.

## Re-audit 2026-10-10

Both notes are fixed.

**Verdict: PASS.**
"""

# An audit with a single verdict in its header and no re-audit section.
SINGLE = """\
# Audit: Clock

- **Date:** 2026-09-15
- **Code:** src/tradebot/clock.py

**Verdict: FAIL.**
"""


def test_reaudit_latest_verdict_supersedes_initial() -> None:
    summary = parse_review("2026-10-01-quality.md", REAUDITED)

    assert summary.initial_verdict == "PASS-WITH-NOTES"
    assert summary.latest_verdict == "PASS"
    assert summary.reaudited is True
    assert summary.file == "2026-10-01-quality.md"
    assert summary.title == "Audit: Data quality"
    assert summary.scope == "src/tradebot/data/quality.py"
    assert summary.date == "2026-10-01"


def test_single_verdict_means_latest_equals_initial() -> None:
    summary = parse_review("clock.md", SINGLE)

    assert summary.initial_verdict == "FAIL"
    assert summary.latest_verdict == "FAIL"
    assert summary.reaudited is False
    assert summary.date == "2026-09-15"


def test_no_verdict_gives_unknown() -> None:
    summary = parse_review("empty.md", "# Audit: Nothing yet\n\nNo verdict written.\n")

    assert summary.initial_verdict == UNKNOWN
    assert summary.latest_verdict == UNKNOWN
    assert summary.reaudited is False
    assert summary.date == ""


def test_scope_falls_back_to_title_without_code_line() -> None:
    summary = parse_review("x.md", "# Audit: Thing\n\n**Verdict: PASS.**\n")
    assert summary.scope == "Audit: Thing"


def test_list_reviews_skips_template_and_sorts_newest_first(tmp_path: Path) -> None:
    (tmp_path / "_TEMPLATE.md").write_text(SINGLE, encoding="utf-8")
    (tmp_path / "old.md").write_text(SINGLE, encoding="utf-8")  # 2026-09-15
    (tmp_path / "new.md").write_text(REAUDITED, encoding="utf-8")  # 2026-10-01
    (tmp_path / "notes.txt").write_text(REAUDITED, encoding="utf-8")  # not a report

    summaries = list_reviews(tmp_path)

    assert [s.file for s in summaries] == ["new.md", "old.md"]
    assert all(s.file != "_TEMPLATE.md" for s in summaries)


def test_list_reviews_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_reviews(tmp_path / "missing") == []


def test_list_reviews_unreadable_file_is_unknown_not_raise(tmp_path: Path) -> None:
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")

    summaries = list_reviews(tmp_path)

    assert [s.file for s in summaries] == ["binary.md"]
    assert summaries[0].latest_verdict == UNKNOWN


# --------------------------------------------------------------------------- process log


PROCESS_LOG = """\
# Process log

| Date | Step | Note |
|---|---|---|
| 2026-10-01 | Spec approved | ok |
| 2026-10-02 | Patch | a \\| b |
| 2026-10-03 | Short row |
"""


def test_process_log_columns_come_from_header() -> None:
    df = parse_process_log(PROCESS_LOG)
    assert list(df.columns) == ["Date", "Step", "Note"]
    assert len(df) == 3


def test_process_log_escaped_pipe_stays_in_cell() -> None:
    df = parse_process_log(PROCESS_LOG)
    assert df.iloc[1]["Step"] == "Patch"
    assert df.iloc[1]["Note"] == "a | b"


def test_process_log_short_row_is_padded() -> None:
    df = parse_process_log(PROCESS_LOG)
    assert df.iloc[2]["Date"] == "2026-10-03"
    assert df.iloc[2]["Note"] == ""


def test_process_log_stops_at_first_non_table_line() -> None:
    text = "| A | B |\n|---|---|\n| 1 | 2 |\nafter the table\n| 3 | 4 |\n"
    df = parse_process_log(text)
    assert df.values.tolist() == [["1", "2"]]


def test_process_log_without_table_is_empty() -> None:
    assert parse_process_log("just prose\n| not | a table |\n").empty


@pytest.mark.parametrize("junk", ["", "|", "|||", "| a |\n| b |", "\x00�|---|", "| - |\n|--|"])
def test_process_log_junk_never_raises(junk: str) -> None:
    assert parse_process_log(junk).empty


# --------------------------------------------------------------------------- junk input


@pytest.mark.parametrize(
    "junk",
    [
        "",
        "\x00\x01\x02",
        "**Verdict:**",
        "**Verdict: MAYBE.**",
        "- **Date:** not a date",
        "# " + "x" * 10_000,
        "�" * 500,
        "**Verdict: PASS**\n**Verdict: FAIL**\n",
    ],
)
def test_parse_review_junk_never_raises(junk: str) -> None:
    summary = parse_review("junk.md", junk)
    assert summary.file == "junk.md"
    assert summary.initial_verdict in {"PASS", "PASS-WITH-NOTES", "FAIL", UNKNOWN}
    assert summary.latest_verdict in {"PASS", "PASS-WITH-NOTES", "FAIL", UNKNOWN}


def test_parse_review_accepts_none_text() -> None:
    summary = parse_review("none.md", None)  # type: ignore[arg-type]
    assert summary.initial_verdict == UNKNOWN
    assert summary.reaudited is False
