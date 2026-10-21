"""Unit tests for devconsole/lib/runners.py (SPEC-006 AC 10, 11).

No test runs the real pytest or git: ``subprocess.run`` is always replaced by a fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devconsole.lib import runners
from devconsole.lib.runners import Commit, parse_git_log, parse_junit, recent_commits, run_tests

pytestmark = pytest.mark.unit

# Hand count of JUNIT: 4 testcases -> passed 1 (test_ok), failed 1 (test_fail),
# errors 1 (test_err), skipped 1 (test_skip). Suite time 1.250 + 0.0 -> duration 1.25.
JUNIT = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites>
<testsuite name="pytest" tests="4" errors="1" failures="1" skipped="1" time="1.250">
  <testcase classname="tests.devconsole.test_a" name="test_ok" time="0.100"/>
  <testcase classname="tests.devconsole.test_a" name="test_fail" time="0.200">
    <failure message="assert 1 == 2&#10;extra detail">Traceback (most recent call last)</failure>
  </testcase>
  <testcase classname="tests.devconsole.test_b" name="test_err" time="0.300">
    <error message="">RuntimeError: boom&#10;  second line</error>
  </testcase>
  <testcase classname="tests.devconsole.test_b" name="test_skip" time="0.000">
    <skipped message="not today"/>
  </testcase>
</testsuite>
</testsuites>
"""

_UNIT = "\x1f"


# --------------------------------------------------------------------------- junit parsing


def test_parse_junit_counts_pass_fail_skip_error() -> None:
    run = parse_junit(JUNIT)

    assert (run.passed, run.failed, run.skipped, run.errors) == (1, 1, 1, 1)
    assert run.duration_s == 1.25
    assert run.finished_note == "ok"


def test_parse_junit_extracts_failure_ids_and_first_message_lines() -> None:
    run = parse_junit(JUNIT)

    assert run.failures == (
        ("tests.devconsole.test_a::test_fail", "assert 1 == 2"),
        ("tests.devconsole.test_b::test_err", "RuntimeError: boom"),
    )


def test_parse_junit_accepts_a_bare_testsuite_root() -> None:
    xml = '<testsuite time="0.5"><testcase classname="c" name="n"/></testsuite>'
    run = parse_junit(xml)
    assert (run.passed, run.failed, run.duration_s) == (1, 0, 0.5)


def test_parse_junit_truncates_long_messages() -> None:
    long_message = "x" * 1000
    xml = (
        '<testsuite time="0"><testcase classname="c" name="n">'
        f'<failure message="{long_message}"/></testcase></testsuite>'
    )
    ((_, message),) = parse_junit(xml).failures
    assert len(message) == 300
    assert message.endswith("…")


def test_parse_junit_malformed_xml_raises_parse_error() -> None:
    import xml.etree.ElementTree as ET

    with pytest.raises(ET.ParseError):
        parse_junit("<testsuite><testcase")


# --------------------------------------------------------------------------- git log parsing


def test_parse_git_log_empty_output_is_empty_list() -> None:
    assert parse_git_log("") == []
    assert parse_git_log("\n\n   \n") == []


def test_parse_git_log_reads_fields_and_keeps_unicode_subjects() -> None:
    raw = f"abc1234{_UNIT}Ada Lovelace{_UNIT}2026-10-01{_UNIT}Fix naïve café 日本語\n"
    assert parse_git_log(raw) == [
        Commit(
            sha="abc1234", author="Ada Lovelace", date="2026-10-01", subject="Fix naïve café 日本語"
        )
    ]


def test_parse_git_log_keeps_pipes_inside_subject() -> None:
    raw = f"def5678{_UNIT}Bob{_UNIT}2026-10-02{_UNIT}feat: a | b | c\n"
    (commit,) = parse_git_log(raw)
    assert commit.subject == "feat: a | b | c"


def test_parse_git_log_skips_malformed_lines() -> None:
    raw = (
        "garbage line with no separators\n"
        f"abc{_UNIT}only two\n"
        f"ok1{_UNIT}A{_UNIT}2026-01-01{_UNIT}ok\n"
    )
    commits = parse_git_log(raw)
    assert [c.sha for c in commits] == ["ok1"]


# --------------------------------------------------------------------------- recent_commits


def test_recent_commits_parses_successful_git_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"cmd": cmd, **kwargs})
        out = f"abc{_UNIT}Ada{_UNIT}2026-10-01{_UNIT}Initial\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    commits, reason = recent_commits(Path("/repo"), n=5)

    assert reason is None
    assert commits == [Commit(sha="abc", author="Ada", date="2026-10-01", subject="Initial")]
    cmd = calls[0]["cmd"]
    assert cmd[:4] == ["git", "log", "-n", "5"]  # type: ignore[index]
    assert calls[0]["cwd"] == "/repo"
    assert calls[0]["check"] is False


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (FileNotFoundError("git"), "git is not installed"),
        (subprocess.TimeoutExpired(cmd="git", timeout=15), "git timed out"),
        (OSError("disk on fire"), "disk on fire"),
    ],
)
def test_recent_commits_failures_return_message_not_raise(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, fragment: str
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", fake_run)

    commits, reason = recent_commits(Path("/repo"))

    assert commits == []
    assert reason is not None
    assert reason.startswith("git history unavailable")
    assert fragment in reason


def test_recent_commits_nonzero_exit_uses_stderr_first_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, 128, stdout="", stderr="fatal: not a git repository\nmore detail\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    commits, reason = recent_commits(Path("/not/a/repo"))

    assert commits == []
    assert reason == "git history unavailable: fatal: not a git repository"


# --------------------------------------------------------------------------- run_tests


def test_run_tests_returns_none_when_lock_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess must not run while the lock is held")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    assert runners._TEST_RUN_LOCK.acquire(blocking=False)
    try:
        assert run_tests(tmp_path) is None
    finally:
        runners._TEST_RUN_LOCK.release()


def test_run_tests_lock_is_released_after_a_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("uv")

    monkeypatch.setattr(subprocess, "run", fake_run)

    first = run_tests(tmp_path)
    second = run_tests(tmp_path)

    assert first is not None and second is not None
    assert not runners._TEST_RUN_LOCK.locked()


def test_run_tests_uv_missing_gives_clear_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> None:
        assert cmd[0] == "uv"
        raise FileNotFoundError("uv")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_tests(tmp_path)

    assert result is not None
    assert result.finished_note == "uv is not on PATH; install uv to run the tests"
    assert (result.passed, result.failed, result.skipped, result.errors) == (0, 0, 0, 0)
    assert result.failures == ()


def test_run_tests_timeout_gives_timed_out_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_tests(tmp_path, timeout_s=1)

    assert result is not None
    assert result.finished_note == "timed out"


def test_run_tests_parses_junit_written_by_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = cmd
        seen["cwd"] = kwargs["cwd"]
        junit = next(a for a in cmd if a.startswith("--junitxml="))
        Path(junit.removeprefix("--junitxml=")).write_text(JUNIT, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_tests(tmp_path)

    assert result is not None
    assert seen["cmd"][:4] == ["uv", "run", "pytest", "-q"]  # type: ignore[index]
    assert seen["cwd"] == str(tmp_path)
    assert (result.passed, result.failed, result.skipped, result.errors) == (1, 1, 1, 1)
    assert result.finished_note == "ok"
    junit_path = Path(next(a for a in seen["cmd"] if a.startswith("--junitxml=")).split("=", 1)[1])  # type: ignore[union-attr]
    assert not junit_path.parent.exists()  # temp dir removed afterwards


def test_run_tests_crash_without_junit_reports_stderr_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="INTERNALERROR boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_tests(tmp_path)

    assert result is not None
    assert result.finished_note.startswith("pytest crashed: ")
    assert "INTERNALERROR boom" in result.finished_note
