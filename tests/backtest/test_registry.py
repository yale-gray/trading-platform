"""Trial registry (ADR-0001 A24)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tradebot.backtest.registry import DEFAULT_REGISTRY_PATH, TrialRecord, TrialRegistry


def _rec(i: str, sr: float | None = 0.01) -> TrialRecord:
    return TrialRecord(id=i, date="2026-10-23", status="failed", sharpe_period=sr)


def test_append_persists_and_refuses_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    reg = TrialRegistry.load(path)
    assert len(reg) == 0
    reg.append(_rec("a"))
    reg.append(_rec("b", None))
    with pytest.raises(ValueError, match="already registered"):
        reg.append(_rec("a"))
    again = TrialRegistry.load(path)
    assert [r.id for r in again.records] == ["a", "b"]
    assert "a" in again and again.get("b") is not None and again.get("zzz") is None
    assert again.sharpes() == [0.01]  # null Sharpes are skipped
    assert len(json.loads(path.read_text())) == 2


def test_committed_registry_is_seeded_with_the_four_failed_trials() -> None:
    reg = TrialRegistry.load(DEFAULT_REGISTRY_PATH)
    ids = {r.id for r in reg.records}
    assert ids >= {
        "stocks_dt_dip",
        "stocks_dt_breakout",
        "etf_dt_late_momentum",
        "etf_dt_opening_drive",
    }
    for r in reg.records:
        assert r.commit and r.status == "failed"
        assert r.sharpe_annual is not None and r.sharpe_period is not None
        assert math.isclose(r.sharpe_period * math.sqrt(252), r.sharpe_annual, abs_tol=1e-3)


def test_protocol_sharpes_exclude_seeded_rows_and_excluded_ids() -> None:
    reg = TrialRegistry(
        [
            TrialRecord(id="s", date="2026-10-23", status="failed", sharpe_period=-0.2),
            TrialRecord(id="a", date="2026-10-23", sharpe_period=0.01, protocol=True),
            TrialRecord(id="b", date="2026-10-23", sharpe_period=0.02, protocol=True),
        ]
    )
    assert len(reg) == 3 and reg.sharpes() == [-0.2, 0.01, 0.02]
    assert reg.protocol_sharpes() == [0.01, 0.02]
    assert reg.protocol_sharpes({"a"}) == [0.02]
    reg.replace(TrialRecord(id="s", date="2026-10-24", sharpe_period=0.5, protocol=True))
    assert len(reg) == 3 and reg.get("s").sharpe_period == 0.5  # type: ignore[union-attr]
