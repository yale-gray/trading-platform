"""Per-package coverage floor checker (SPEC-005 section 9, item 4).

Reads a coverage.py JSON report (``pytest --cov --cov-report=json`` writes
``coverage.json``) and applies the per-package floors. Packages with no
statements in the report are skipped. Prints a table and exits 1 on failure.

Usage: python scripts/check_coverage.py [coverage.json]
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE_ROOT = "tradebot"
TOTAL_KEY = "(total)"


@dataclass(frozen=True)
class Floor:
    line: int
    branch: int | None = None


# Floors from SPEC-005 section 9 item 4. Packages not listed have no floor.
# "(total)" is the whole tradebot package.
# Deliberate PM choice: there is no separate "metrics" floor. Metrics live in
# backtest/metrics.py, so they are covered by the strict backtest floor (95/90).
FLOORS: dict[str, Floor] = {
    "sim": Floor(line=95, branch=90),
    "portfolio": Floor(line=95, branch=90),
    "risk": Floor(line=95, branch=90),
    "backtest": Floor(line=95, branch=90),
    "strategy": Floor(line=90),
    "data": Floor(line=90),
    TOTAL_KEY: Floor(line=80),
}

# Label for the top-level modules row (src/tradebot/*.py); package_of returns "" for them.
TOP_LEVEL_LABEL = "(top-level)"


@dataclass
class Totals:
    statements: int = 0
    covered_lines: int = 0
    branches: int = 0
    covered_branches: int = 0

    def add(self, other: Totals) -> None:
        self.statements += other.statements
        self.covered_lines += other.covered_lines
        self.branches += other.branches
        self.covered_branches += other.covered_branches


@dataclass(frozen=True)
class Row:
    package: str
    totals: Totals | None
    floor: Floor | None
    status: str  # PASS, FAIL, SKIP (no code), NO FLOOR
    reason: str = ""


def load_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read coverage report {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ValueError(f"{path} is not a coverage.py JSON report (no 'files' object)")
    return data


def package_of(file_path: str) -> str | None:
    """Return the tradebot sub-package for a file path, '' for top-level modules,
    or None when the file is not under tradebot.

    The package root is the last ``src/tradebot`` pair in the path, so a checkout
    directory that is itself named ``tradebot`` cannot shift the match.
    """
    parts = file_path.replace("\\", "/").split("/")
    idx = None
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == PACKAGE_ROOT and parts[i - 1] == "src":
            idx = i
            break
    if idx is None:
        return None
    rest = parts[idx + 1 :]
    if len(rest) <= 1:
        return ""
    return rest[0]


def _totals_from_summary(summary: dict[str, Any]) -> Totals:
    return Totals(
        statements=int(summary.get("num_statements", 0)),
        covered_lines=int(summary.get("covered_lines", 0)),
        branches=int(summary.get("num_branches", 0)),
        covered_branches=int(summary.get("covered_branches", 0)),
    )


def aggregate(report: dict[str, Any]) -> dict[str, Totals]:
    """Sum file summaries per sub-package, plus a TOTAL_KEY entry for all of tradebot."""
    per_package: dict[str, Totals] = {}
    total = Totals()
    for file_path, info in report["files"].items():
        package = package_of(file_path)
        if package is None:
            continue
        file_totals = _totals_from_summary(info.get("summary", {}))
        per_package.setdefault(package, Totals()).add(file_totals)
        total.add(file_totals)
    per_package[TOTAL_KEY] = total
    return per_package


def _meets(covered: int, count: int, floor_pct: int) -> bool:
    # Integer comparison: covered / count >= floor_pct / 100, with no float rounding.
    return covered * 100 >= floor_pct * count


def evaluate(totals_by_package: dict[str, Totals]) -> list[Row]:
    rows: list[Row] = []
    names = sorted(set(totals_by_package) | set(FLOORS), key=lambda n: (n == TOTAL_KEY, n))
    for name in names:
        floor = FLOORS.get(name)
        totals = totals_by_package.get(name)
        if totals is None or totals.statements == 0:
            if floor is None:
                continue
            rows.append(Row(name, None, floor, "SKIP", "no code"))
            continue
        if floor is None:
            rows.append(Row(name, totals, None, "NO FLOOR"))
            continue
        reasons: list[str] = []
        if not _meets(totals.covered_lines, totals.statements, floor.line):
            reasons.append(f"line < {floor.line}%")
        if floor.branch is not None and not _meets(
            totals.covered_branches, totals.branches, floor.branch
        ):
            reasons.append(f"branch < {floor.branch}%")
        status = "FAIL" if reasons else "PASS"
        rows.append(Row(name, totals, floor, status, ", ".join(reasons)))
    return rows


def _pct(covered: int, count: int) -> str:
    if count == 0:
        return "n/a"
    return f"{covered * 100 / count:.2f}"


def render_table(rows: list[Row]) -> str:
    header = ["package", "lines", "line %", "floor", "branch %", "floor", "status"]
    body: list[list[str]] = []
    for row in rows:
        status = row.status + (f" ({row.reason})" if row.reason else "")
        label = row.package or TOP_LEVEL_LABEL
        if row.totals is None or row.totals.statements == 0:
            body.append([label, "-", "-", "-", "-", "-", status])
            continue
        t = row.totals
        line_floor = f">= {row.floor.line}" if row.floor else "-"
        branch_floor = (
            f">= {row.floor.branch}" if row.floor and row.floor.branch is not None else "-"
        )
        body.append(
            [
                label,
                f"{t.covered_lines}/{t.statements}",
                _pct(t.covered_lines, t.statements),
                line_floor,
                _pct(t.covered_branches, t.branches),
                branch_floor,
                status,
            ]
        )
    table = [header, *body]
    widths = [max(len(r[i]) for r in table) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip() for r in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else Path("coverage.json")
    try:
        report = load_report(path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rows = evaluate(aggregate(report))
    print(render_table(rows))
    failed = [r.package for r in rows if r.status == "FAIL"]
    if failed:
        print(f"\ncoverage floors FAILED for: {', '.join(failed)}")
        return 1
    print("\ncoverage floors passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
