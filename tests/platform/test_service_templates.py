"""LaunchAgent templates and ``scripts/service/tradebot-service.sh`` (dry run, no launchctl)."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SERVICE_DIR = REPO / "scripts" / "service"
SCRIPT = SERVICE_DIR / "tradebot-service.sh"
RUNNER_TEMPLATE = SERVICE_DIR / "com.tradebot.runner.plist.template"
API_TEMPLATE = SERVICE_DIR / "com.tradebot.api.plist.template"

RUNNER = ("com.tradebot.runner", ["run", "tradebot", "runner", "start"], "runner")
API = ("com.tradebot.api", ["run", "tradebot", "api", "serve"], "api")


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    """A fake repo root, a fake ``uv`` on PATH, and a LaunchAgents folder, all in tmp_path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    agents = tmp_path / "LaunchAgents"
    return {"repo": repo, "bin": bin_dir, "uv": uv, "agents": agents}


def _run(sandbox: dict[str, Path], *args: str, env_extra: dict[str, str] | None = None):
    env = {
        **os.environ,
        "PATH": f"{sandbox['bin']}{os.pathsep}{os.environ.get('PATH', '')}",
        "LAUNCH_AGENTS_DIR": str(sandbox["agents"]),
        "TRADEBOT_REPO_ROOT": str(sandbox["repo"]),
        "TRADEBOT_SERVICE_DRY_RUN": "1",
    }
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("template", [RUNNER_TEMPLATE, API_TEMPLATE])
def test_templates_are_valid_plist_xml(template: Path) -> None:
    data = plistlib.loads(template.read_bytes())
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["ProgramArguments"][0] == "@UV@"


@pytest.mark.parametrize(("label", "args", "name"), [RUNNER, API])
def test_install_renders_valid_plists(
    sandbox: dict[str, Path], label: str, args: list[str], name: str
) -> None:
    result = _run(sandbox, "install")
    assert result.returncode == 0, result.stderr

    plist_file = sandbox["agents"] / f"{label}.plist"
    raw = plist_file.read_bytes()
    assert b"@UV" not in raw and b"@REPO_ROOT" not in raw and b"@UV_DIR" not in raw  # filled

    data = plistlib.loads(raw)
    assert data["Label"] == label
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True

    argv = data["ProgramArguments"]
    assert Path(argv[0]).is_absolute()
    assert argv[0] == str(sandbox["uv"])
    assert argv[1:] == args

    repo = str(sandbox["repo"])
    assert Path(data["WorkingDirectory"]).is_absolute()
    assert data["WorkingDirectory"] == repo

    for key in ("StandardOutPath", "StandardErrorPath"):
        assert Path(data[key]).is_absolute()
        assert data[key].startswith(f"{repo}/logs/")
    assert data["StandardOutPath"].endswith(f"{name}.out.log")
    assert data["StandardErrorPath"].endswith(f"{name}.err.log")

    path_var = data["EnvironmentVariables"]["PATH"]
    assert "/opt/homebrew/bin" in path_var.split(":")
    assert str(sandbox["bin"]) in path_var.split(":")


def test_install_creates_logs_dir_and_prints_app_url(sandbox: dict[str, Path]) -> None:
    result = _run(sandbox, "install")
    assert result.returncode == 0, result.stderr
    assert (sandbox["repo"] / "logs").is_dir()
    assert "http://127.0.0.1:8000" in result.stdout


def test_uninstall_removes_plists(sandbox: dict[str, Path]) -> None:
    assert _run(sandbox, "install").returncode == 0
    result = _run(sandbox, "uninstall")
    assert result.returncode == 0, result.stderr
    assert not (sandbox["agents"] / "com.tradebot.runner.plist").exists()
    assert not (sandbox["agents"] / "com.tradebot.api.plist").exists()


def test_status_dry_run_reports_both_labels(sandbox: dict[str, Path]) -> None:
    assert _run(sandbox, "install").returncode == 0
    result = _run(sandbox, "status")
    assert result.returncode == 0, result.stderr
    assert "com.tradebot.runner" in result.stdout
    assert "com.tradebot.api" in result.stdout


def test_status_before_install_reports_not_installed(sandbox: dict[str, Path]) -> None:
    result = _run(sandbox, "status")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("not installed") == 2


def test_unknown_or_missing_subcommand_exits_2(sandbox: dict[str, Path]) -> None:
    assert _run(sandbox, "bogus").returncode == 2
    assert _run(sandbox).returncode == 2


def test_repo_path_with_unsafe_character_is_refused(tmp_path: Path) -> None:
    sandbox = {
        "repo": tmp_path / "bad&repo",
        "bin": tmp_path / "bin",
        "uv": tmp_path / "bin" / "uv",
        "agents": tmp_path / "LaunchAgents",
    }
    sandbox["repo"].mkdir()
    (sandbox["repo"] / "pyproject.toml").write_text("")
    sandbox["bin"].mkdir()
    sandbox["uv"].write_text("#!/bin/sh\nexit 0\n")
    sandbox["uv"].chmod(0o755)
    result = _run(sandbox, "install")
    assert result.returncode != 0
    assert not (sandbox["agents"] / "com.tradebot.runner.plist").exists()


@pytest.mark.parametrize("template", [RUNNER_TEMPLATE, API_TEMPLATE])
def test_templates_throttle_restarts(template: Path) -> None:
    assert plistlib.loads(template.read_bytes())["ThrottleInterval"] == 30
