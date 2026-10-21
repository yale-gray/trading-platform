"""Unit tests for scripts/check_coverage.py (SPEC-005 T9, section 9 item 4)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_coverage.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_coverage", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cc = _load_module()

# file path -> (statements, covered lines, branches, covered branches)
FileSpec = tuple[int, int, int, int]


def _report(files: dict[str, FileSpec]) -> dict[str, object]:
    out: dict[str, object] = {}
    for path, (stmts, covered, branches, covered_branches) in files.items():
        out[path] = {
            "summary": {
                "num_statements": stmts,
                "covered_lines": covered,
                "num_branches": branches,
                "covered_branches": covered_branches,
            }
        }
    return {"files": out}


def _write(tmp_path: Path, report: dict[str, object]) -> Path:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _passing_files() -> dict[str, FileSpec]:
    # Every floored package at 100%, plus an unfloored api package at 100%.
    return {
        "src/tradebot/sim/engine.py": (100, 100, 20, 20),
        "src/tradebot/portfolio/money.py": (50, 50, 10, 10),
        "src/tradebot/risk/limits.py": (40, 40, 8, 8),
        "src/tradebot/strategy/indicators.py": (60, 60, 12, 12),
        "src/tradebot/data/provider.py": (30, 30, 6, 6),
        "src/tradebot/api/routes.py": (20, 20, 4, 4),
    }


def test_pass_when_all_floors_met(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, _report(_passing_files()))
    assert cc.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "FAIL" not in out
    assert "coverage floors passed" in out


def test_fail_when_line_floor_missed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = _passing_files()
    # sim at 94/100 = 94% line, below the 95% floor.
    files["src/tradebot/sim/engine.py"] = (100, 94, 20, 20)
    path = _write(tmp_path, _report(files))
    assert cc.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL (line < 95%)" in out
    assert "coverage floors FAILED for: sim" in out


def test_fail_when_branch_floor_missed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = _passing_files()
    # risk: lines fine, branches 17/20 = 85% below the 90% branch floor.
    files["src/tradebot/risk/limits.py"] = (40, 40, 20, 17)
    path = _write(tmp_path, _report(files))
    assert cc.main([str(path)]) == 1
    assert "FAIL (branch < 90%)" in capsys.readouterr().out


def test_floor_boundary_is_inclusive(tmp_path: Path) -> None:
    files = _passing_files()
    # Exactly 95% lines and 90% branches on sim.
    files["src/tradebot/sim/engine.py"] = (100, 95, 10, 9)
    path = _write(tmp_path, _report(files))
    assert cc.main([str(path)]) == 0


def test_package_with_no_code_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = _passing_files()
    # strategy holds only an empty __init__.py: zero statements, must not divide by zero
    # or fail the 90% floor.
    files["src/tradebot/strategy/__init__.py"] = (0, 0, 0, 0)
    del files["src/tradebot/strategy/indicators.py"]
    path = _write(tmp_path, _report(files))
    assert cc.main([str(path)]) == 0
    out = capsys.readouterr().out
    strategy_rows = [line for line in out.splitlines() if line.startswith("strategy")]
    assert strategy_rows and "SKIP (no code)" in strategy_rows[0]


def test_missing_packages_are_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Only sim has code. Every other floored package is absent from the report.
    files = {"src/tradebot/sim/engine.py": (100, 100, 20, 20)}
    path = _write(tmp_path, _report(files))
    assert cc.main([str(path)]) == 0
    out = capsys.readouterr().out
    for name in ("portfolio", "risk", "strategy", "data"):
        rows = [line for line in out.splitlines() if line.startswith(name)]
        assert rows and "SKIP (no code)" in rows[0]


def test_whole_package_floor_applies_to_total(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "src/tradebot/sim/engine.py": (100, 100, 0, 0),
        # api has no per-package floor but still counts toward the total.
        "src/tradebot/api/routes.py": (100, 0, 0, 0),
    }
    path = _write(tmp_path, _report(files))
    # total 100/200 = 50% < 80%
    assert cc.main([str(path)]) == 1
    assert "(total)" in capsys.readouterr().out


def test_unfloored_package_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "src/tradebot/sim/engine.py": (100, 100, 0, 0),
        "src/tradebot/api/routes.py": (10, 0, 0, 0),
    }
    path = _write(tmp_path, _report(files))
    # total 100/110 = 90.9% >= 80%; api at 0% is reported but has no floor.
    assert cc.main([str(path)]) == 0
    api_rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("api")]
    assert api_rows and "NO FLOOR" in api_rows[0]


def test_top_level_module_counts_only_toward_total() -> None:
    assert cc.package_of("src/tradebot/clock.py") == ""
    assert cc.package_of("src/tradebot/sim/engine.py") == "sim"
    assert cc.package_of("src/other/thing.py") is None


def test_checkout_dir_named_tradebot_does_not_shift_package() -> None:
    assert cc.package_of("/Users/dev/tradebot/src/tradebot/sim/engine.py") == "sim"
    assert cc.package_of("tradebot/src/tradebot/risk/limits.py") == "risk"
    assert cc.package_of("C:\\work\\tradebot\\src\\tradebot\\data\\provider.py") == "data"
    assert cc.package_of("/Users/dev/tradebot/src/other/thing.py") is None


def test_checkout_dir_named_tradebot_end_to_end(tmp_path: Path) -> None:
    # The checkout directory is named tradebot; the real package is still floored.
    files = {
        "/Users/dev/tradebot/src/tradebot/sim/engine.py": (100, 94, 20, 20),
    }
    path = _write(tmp_path, _report(files))
    assert cc.main([str(path)]) == 1


def test_metrics_has_no_separate_floor() -> None:
    assert "metrics" not in cc.FLOORS
    assert cc.package_of("src/tradebot/backtest/metrics.py") == "backtest"


def test_metrics_only_file_gets_no_floor_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A zero-coverage metrics module under a non-floored package must not fail the run.
    files = {
        "src/tradebot/sim/engine.py": (100, 100, 20, 20),
        "src/tradebot/metrics/stats.py": (10, 0, 0, 0),
    }
    path = _write(tmp_path, _report(files))
    assert cc.main([str(path)]) == 0
    rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("metrics")]
    assert rows and "NO FLOOR" in rows[0]


def test_backtest_floor_applies_to_metrics_module(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = _passing_files()
    # metrics.py lives in backtest/, so its low coverage fails the strict backtest floor.
    files["src/tradebot/backtest/metrics.py"] = (50, 40, 10, 10)
    path = _write(tmp_path, _report(files))
    assert cc.main([str(path)]) == 1
    assert "coverage floors FAILED for: backtest" in capsys.readouterr().out


def test_top_level_row_is_labelled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = _passing_files()
    files["src/tradebot/clock.py"] = (10, 10, 0, 0)
    path = _write(tmp_path, _report(files))
    assert cc.main([str(path)]) == 0
    out = capsys.readouterr().out
    top = [line for line in out.splitlines() if line.startswith("(top-level)")]
    assert top and "NO FLOOR" in top[0]
    assert "  \n" not in out and not any(line.startswith(" ") for line in out.splitlines())


def test_invalid_report_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, {"totals": {}})
    assert cc.main([str(path)]) == 2
    assert "not a coverage.py JSON report" in capsys.readouterr().err


def test_missing_report_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cc.main([str(tmp_path / "nope.json")]) == 2
    assert "cannot read coverage report" in capsys.readouterr().err
