"""Overview page: what this console is, roadmap progress, test runner, recent commits."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from devconsole.lib.roadmap import RoadmapSection, parse_roadmap
from devconsole.lib.runners import TestRun, recent_commits, run_tests
from devconsole.ui import explainer
from tradebot.clock import SystemClock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KEY = "overview_last_test_run"
_KEY_AT = "overview_last_test_at"
_KEY_BUSY = "overview_tests_running"

_DOCS = (
    ("Strategy", "docs/STRATEGY.md"),
    ("Roadmap", "docs/ROADMAP.md"),
    ("ADR-0001 (architecture)", "docs/adr/ADR-0001-architecture-and-module-contracts.md"),
    ("Process log", "docs/process-log.md"),
    ("Spec 001 (market data)", "docs/specs/001-market-data.md"),
    ("Spec 002 (strategy and backtest)", "docs/specs/002-strategy-backtest.md"),
    ("Spec 003 (execution, portfolio and risk)", "docs/specs/003-execution-portfolio-risk.md"),
    ("Spec 004 (platform)", "docs/specs/004-platform.md"),
    ("Spec 005 (test strategy)", "docs/specs/005-test-strategy.md"),
    ("Spec 006 (dev console)", "docs/specs/006-dev-console.md"),
)


def pick_sections(
    sections: list[RoadmapSection],
) -> tuple[RoadmapSection | None, RoadmapSection | None]:
    """Return (now building, up next) from the parsed roadmap."""
    now = next(
        (s for s in sections if "in progress" in s.status_hint.lower()),
        None,
    ) or next((s for s in sections if s.in_progress > 0), None)
    nxt = next((s for s in sections if s.status_hint.lower() == "next"), None)
    if nxt is None:
        nxt = next((s for s in sections if s is not now and s.done < s.total), None)
    return now, nxt


def run_tests_handler(repo_root: Path = _REPO_ROOT) -> None:
    """Run the test suite and store the outcome in session state."""
    st.session_state[_KEY_BUSY] = True
    try:
        with st.spinner("Running the automated checks…"):
            result = run_tests(repo_root)
    finally:
        st.session_state[_KEY_BUSY] = False
    if result is None:
        st.session_state[_KEY] = None
        st.session_state["overview_already_running"] = True
        return
    st.session_state["overview_already_running"] = False
    st.session_state[_KEY] = result
    st.session_state[_KEY_AT] = SystemClock().now()


def render_test_results(result: TestRun, when: datetime | None) -> None:
    """Show counts, failures and the time of a finished run."""
    if result.finished_note != "ok":
        st.error(f"The test run did not finish cleanly: {result.finished_note}")
    c1, c2, c3 = st.columns(3)
    c1.metric("Passed", result.passed)
    c2.metric("Failed", result.failed + result.errors)
    c3.metric("Skipped", result.skipped)
    stamp = when.strftime("%Y-%m-%d %H:%M:%S") if when else "unknown"
    st.caption(f"Last run: {stamp} ({result.duration_s:.1f}s)")
    if result.failures:
        with st.expander(f"Failures ({len(result.failures)})"):
            for test_id, message in result.failures:
                st.markdown(f"- `{test_id}`: {message}")


def _is_setup(section: RoadmapSection) -> bool:
    """Phase 0 and 0.5 are one-off setup, so they are collapsed at the bottom."""
    return section.title.startswith("Phase 0")


def _render_progress(section: RoadmapSection) -> None:
    label = f"{section.title} ({section.status_hint})" if section.status_hint else section.title
    st.progress(section.fraction, text=f"{label}: {section.done}/{section.total}")


def _render_roadmap(sections: list[RoadmapSection]) -> None:
    now, nxt = pick_sections(sections)
    if now is not None:
        st.subheader(f"Now building: {now.title}")
    if nxt is not None:
        st.markdown(f"**Up next:** {nxt.title}")
    for s in sections:
        if not _is_setup(s):
            _render_progress(s)
    setup = [s for s in sections if _is_setup(s)]
    if setup:
        with st.expander("Setup (Phase 0 and 0.5)"):
            for s in setup:
                _render_progress(s)


def _render_tests() -> None:
    st.subheader("Automated checks")
    st.caption(
        '"Passed" means a check compared the bot\'s answer to a known-correct answer '
        "and they matched."
    )
    st.caption("Tests use generated practice data and never touch the internet.")
    busy = bool(st.session_state.get(_KEY_BUSY))
    if st.button("Run tests", disabled=busy):
        run_tests_handler()
    if st.session_state.get("overview_already_running"):
        st.warning("A test run is already in progress. Try again in a moment.")
    result = st.session_state.get(_KEY)
    if result is not None:
        render_test_results(result, st.session_state.get(_KEY_AT))
    elif not st.session_state.get("overview_already_running"):
        st.info("No results yet. Press Run tests (takes about 15 seconds).")


def _render_commits() -> None:
    st.subheader("Recent commits")
    commits, error = recent_commits(_REPO_ROOT)
    if error:
        st.info(f"Git history is not available: {error}")
        return
    st.dataframe(
        [{"Commit": c.sha, "Date": c.date, "Message": c.subject} for c in commits],
        hide_index=True,
        width="stretch",
    )


def render() -> None:
    """Render the Overview page."""
    st.title("Overview")
    explainer(
        "This is a **local developer console** for the trading bot project. It lets you look "
        "at data, strategies and project health. **Nothing here trades or changes any data.** "
        "The bot cannot trade at all until Wave 3 (the engine) is built."
    )
    try:
        text = (_REPO_ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
        sections = parse_roadmap(text)
    except OSError as exc:
        sections = []
        st.info(f"Roadmap not available: {exc}")
    if sections:
        _render_roadmap(sections)
    _render_tests()
    _render_commits()
    _render_key_docs()


def _render_key_docs() -> None:
    st.subheader("Key documents")
    for name, rel in _DOCS:
        st.markdown(f"**{name}**: `{rel}`")
        with st.expander("View"):
            try:
                body = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            except OSError as exc:
                st.info(f"Could not read {rel}: {exc}")
                continue
            st.markdown(body)
