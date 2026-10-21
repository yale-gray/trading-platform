"""Quality & reviews page: audit reports and the process log. Read-only."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from devconsole import ui
from devconsole.lib.reviews import UNKNOWN, ReviewSummary, list_reviews, parse_process_log

_REPO_ROOT = Path(__file__).resolve().parents[2]
REVIEWS_DIR = _REPO_ROOT / "docs" / "reviews"
PROCESS_LOG = _REPO_ROOT / "docs" / "process-log.md"

# Colour plus a text label, so the verdict is never conveyed by colour alone.
_VERDICT_STATUS = {
    "PASS": ":green[PASS]",
    "PASS-WITH-NOTES": ":orange[PASS-WITH-NOTES]",
    "FAIL": ":red[FAIL]",
    UNKNOWN: ":gray[UNKNOWN]",
}


def _read_text(path: Path) -> str | None:
    """File text, or None when the file is missing or unreadable. Never raises."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _cell(value: str) -> str:
    """Make a value safe inside a markdown table cell."""
    text = value.replace("\n", " ").replace("|", "\\|").strip()
    return text or "-"


def _status(verdict: str) -> str:
    return _VERDICT_STATUS.get(verdict, _VERDICT_STATUS[UNKNOWN])


def _reviews_table(reviews: list[ReviewSummary]) -> str:
    lines = [
        "| Report | Scope | Date | First verdict | Latest verdict | Re-audited |",
        "|---|---|---|---|---|---|",
    ]
    for review in reviews:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(f"`{review.file}`"),
                    _cell(review.scope),
                    _cell(review.date),
                    _status(review.initial_verdict),
                    _status(review.latest_verdict),
                    "Yes" if review.reaudited else "No",
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _render_reviews() -> None:
    st.subheader("Audit reports")
    reviews = list_reviews(REVIEWS_DIR)
    if not reviews:
        st.info("No audit reports yet. They appear here once files exist in `docs/reviews/`.")
        return
    st.markdown(_reviews_table(reviews))

    chosen = st.selectbox(
        "View a report",
        [review.file for review in reviews],
        key="quality_report",
        help="The full markdown of the selected audit report.",
    )
    text = _read_text(REVIEWS_DIR / chosen)
    if text is None:
        st.info(f"`{chosen}` could not be read.")
        return
    with st.container(border=True):
        st.markdown(text)


def _render_process_log() -> None:
    st.subheader("Process log")
    text = _read_text(PROCESS_LOG)
    if text is None:
        st.info("No process log yet. It is expected at `docs/process-log.md`.")
        return
    table = parse_process_log(text)
    if table.empty:
        st.info("The process log has no table yet.")
        return
    if table.columns.duplicated().any():
        table.columns = [f"{name} ({i})" for i, name in enumerate(table.columns)]
    st.caption("Rework = how many times a task was sent back.")
    st.dataframe(table, hide_index=True)


def render() -> None:
    st.title("Quality & reviews")
    ui.explainer(
        "An **audit** is an independent check of code or a spec by a reviewer who did not "
        "write it. The **review** is the written report of what the auditor found. Each report "
        "ends with one verdict:\n\n"
        "- **PASS**: no open problems.\n"
        "- **PASS-WITH-NOTES**: safe to continue, with a follow-up list.\n"
        "- **FAIL**: must be fixed before we build on it.\n\n"
        "A **re-audit** is a later check of the same work after fixes. The latest verdict "
        "is the one that counts."
    )
    _render_reviews()
    _render_process_log()
