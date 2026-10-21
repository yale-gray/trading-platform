"""Read-only subprocess runners for the dev console (SPEC-006). No streamlit imports here.

Only two commands are ever run: the project test suite (into a temp dir outside the repo) and
``git log`` for recent history. Both use list arguments, never a shell.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

_TEST_RUN_LOCK = threading.Lock()
_TESTS_TIMEOUT_DEFAULT_S = 600
_GIT_TIMEOUT_S = 15
_TAIL_CHARS = 300
_MESSAGE_CHARS = 300
_COMMIT_FIELD_SEP = "\x1f"


@dataclass(frozen=True)
class TestRun:
    passed: int
    failed: int
    skipped: int
    errors: int
    duration_s: float
    failures: tuple[tuple[str, str], ...]  # (test id, short message); failures and errors
    finished_note: str  # "ok" | "pytest crashed: <stderr tail>" | "timed out" | other reason


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    date: str
    subject: str


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _tail(text: str | None) -> str:
    stripped = (text or "").strip()
    return stripped[-_TAIL_CHARS:]


def _empty_run(note: str, duration_s: float = 0.0) -> TestRun:
    return TestRun(
        passed=0,
        failed=0,
        skipped=0,
        errors=0,
        duration_s=duration_s,
        failures=(),
        finished_note=note,
    )


def parse_junit(xml_text: str) -> TestRun:
    """Parse pytest's junit XML into counts and failure details.

    Raises ``xml.etree.ElementTree.ParseError`` if ``xml_text`` is not well-formed XML.
    """
    root = ET.fromstring(xml_text)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))

    duration = 0.0
    for suite in suites:
        try:
            duration += float(suite.get("time", "0") or 0)
        except ValueError:
            pass

    passed = failed = skipped = errors = 0
    failures: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        name = case.get("name", "")
        classname = case.get("classname", "")
        test_id = f"{classname}::{name}" if classname else name

        failure = case.find("failure")
        error = case.find("error")
        skip = case.find("skipped")
        if failure is not None:
            failed += 1
            failures.append((test_id, _short(_message_of(failure), _MESSAGE_CHARS)))
        elif error is not None:
            errors += 1
            failures.append((test_id, _short(_message_of(error), _MESSAGE_CHARS)))
        elif skip is not None:
            skipped += 1
        else:
            passed += 1

    return TestRun(
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        duration_s=round(duration, 3),
        failures=tuple(failures),
        finished_note="ok",
    )


def _message_of(element: ET.Element) -> str:
    message = element.get("message") or ""
    if message.strip():
        return _first_line(message)
    return _first_line(element.text)


def run_tests(repo_root: Path, timeout_s: int = _TESTS_TIMEOUT_DEFAULT_S) -> TestRun | None:
    """Run the test suite once and return its results.

    Returns ``None`` immediately if another run already holds the module lock. The junit file
    goes into a temp dir outside the repo, which is removed afterwards.
    """
    if not _TEST_RUN_LOCK.acquire(blocking=False):
        return None

    tmp_dir: str | None = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="devconsole-junit-")
        junit_path = Path(tmp_dir) / "junit.xml"
        cmd = ["uv", "run", "pytest", "-q", f"--junitxml={junit_path}"]

        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except FileNotFoundError:
            return _empty_run("uv is not on PATH; install uv to run the tests")
        except subprocess.TimeoutExpired:
            return _empty_run("timed out", time.monotonic() - started)
        duration = time.monotonic() - started

        if not junit_path.is_file():
            return _empty_run(f"pytest crashed: {_tail(proc.stderr or proc.stdout)}", duration)
        try:
            xml_text = junit_path.read_text(encoding="utf-8")
            run = parse_junit(xml_text)
        except (ET.ParseError, OSError, UnicodeDecodeError):
            return _empty_run(
                f"pytest crashed: junit output unreadable; {_tail(proc.stderr or proc.stdout)}",
                duration,
            )

        # Exit codes 0 (all passed) and 1 (test failures) are normal results.
        if proc.returncode not in (0, 1):
            note = f"pytest crashed: {_tail(proc.stderr or proc.stdout)}"
        else:
            note = "ok"
        return TestRun(
            passed=run.passed,
            failed=run.failed,
            skipped=run.skipped,
            errors=run.errors,
            duration_s=round(duration, 3),
            failures=run.failures,
            finished_note=note,
        )
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _TEST_RUN_LOCK.release()


def parse_git_log(raw: str) -> list[Commit]:
    """Parse output of the ``recent_commits`` git format (fields separated by ``\\x1f``)."""
    commits: list[Commit] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split(_COMMIT_FIELD_SEP, 3)
        if len(parts) != 4:
            continue
        sha, author, date, subject = parts
        commits.append(Commit(sha=sha, author=author, date=date, subject=subject))
    return commits


def recent_commits(repo_root: Path, n: int = 10) -> tuple[list[Commit], str | None]:
    """Return up to ``n`` recent commits, or ``([], reason)`` if git history is unavailable."""
    cmd = [
        "git",
        "log",
        "-n",
        str(int(n)),
        "--date=short",
        "--pretty=format:%h%x1f%an%x1f%ad%x1f%s",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return [], "git history unavailable: git is not installed or the folder is missing"
    except subprocess.TimeoutExpired:
        return [], "git history unavailable: git timed out"
    except OSError as exc:
        return [], f"git history unavailable: {exc}"

    if proc.returncode != 0:
        reason = _first_line(proc.stderr) or f"git exited with code {proc.returncode}"
        return [], f"git history unavailable: {reason}"
    return parse_git_log(proc.stdout), None
