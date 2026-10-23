"""Trial registry (ADR-0001 A24): every strategy id ever pre-registered and tested.

N for the deflated Sharpe ratio is the registry length. The file is JSON so it diffs well in
review; ``append`` refuses a duplicate id.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = ["DEFAULT_REGISTRY_PATH", "TrialRecord", "TrialRegistry"]

DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "research" / ("trial-registry.json")
)


class TrialRecord(BaseModel):
    """One pre-registered trial. Sharpes are net of costs; ``sharpe_period`` is per daily period."""

    model_config = ConfigDict(frozen=True)

    id: str
    commit: str | None = None  # pre-registration commit
    date: str  # ISO date of the test
    status: Literal["failed", "passed", "registered", "advanced"] = "registered"
    sharpe_period: float | None = None
    sharpe_annual: float | None = None
    n_obs: int | None = None
    batch: str | None = None
    protocol: bool = False  # run under the A24-A30 protocol (only these feed the DSR variance)
    notes: str = ""


class TrialRegistry:
    """The ordered list of trials, backed by a JSON file when ``path`` is set."""

    def __init__(self, records: list[TrialRecord] | None = None, path: Path | None = None) -> None:
        self.records: list[TrialRecord] = list(records or [])
        self.path = path

    @classmethod
    def load(cls, path: Path | None = None) -> TrialRegistry:
        """Read the registry file (a missing file is an empty registry)."""
        p = path or DEFAULT_REGISTRY_PATH
        if not p.exists():
            return cls([], p)
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls([TrialRecord(**r) for r in raw], p)

    def save(self) -> None:
        """Write the registry back to its file."""
        if self.path is None:
            raise ValueError("registry has no path")
        data = [r.model_dump() for r in self.records]
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def append(self, record: TrialRecord) -> None:
        """Add ``record`` (and save when file-backed); a duplicate id raises ``ValueError``."""
        if record.id in self:
            raise ValueError(f"trial {record.id!r} is already registered")
        self.records.append(record)
        if self.path is not None:
            self.save()

    def replace(self, record: TrialRecord) -> None:
        """Overwrite the record with the same id (e.g. a registered trial now tested)."""
        i = next((k for k, r in enumerate(self.records) if r.id == record.id), None)
        if i is None:
            raise ValueError(f"trial {record.id!r} is not registered")
        self.records[i] = record
        if self.path is not None:
            self.save()

    def get(self, trial_id: str) -> TrialRecord | None:
        """The record for ``trial_id`` or ``None``."""
        return next((r for r in self.records if r.id == trial_id), None)

    def __contains__(self, trial_id: object) -> bool:
        return any(r.id == trial_id for r in self.records)

    def __len__(self) -> int:
        return len(self.records)

    def sharpes(self) -> list[float]:
        """Per-period Sharpes of every trial that has one."""
        return [
            r.sharpe_period
            for r in self.records
            if r.sharpe_period is not None and math.isfinite(r.sharpe_period)
        ]

    def protocol_sharpes(self, exclude_ids: set[str] | frozenset[str] = frozenset()) -> list[float]:
        """Per-period Sharpes of protocol-run trials only (the seeded failures are excluded)."""
        return [
            r.sharpe_period
            for r in self.records
            if r.protocol
            and r.id not in exclude_ids
            and r.sharpe_period is not None
            and math.isfinite(r.sharpe_period)
        ]
