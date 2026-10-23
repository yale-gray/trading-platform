"""Public interest-rate series committed with the package (refreshed by hand)."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pandas as pd

__all__ = ["DTB3_PATH", "load_dtb3"]

DTB3_PATH = Path(__file__).parent / "dtb3.csv"
"""FRED DTB3: 3-month Treasury bill, secondary market, discount basis, percent per year."""


def load_dtb3(path: Path | None = None) -> pd.Series:
    """Daily 3-month T-bill rate as a decimal annual rate (0.04 = 4%), indexed by UTC midnight.

    Business days FRED leaves blank (holidays, ``.``) are forward-filled; leading blanks are
    dropped.
    """
    rows: dict[pd.Timestamp, float] = {}
    with (path or DTB3_PATH).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("DTB3") or "").strip()
            d = date.fromisoformat(row["observation_date"][:10])
            ts = pd.Timestamp(d, tz="UTC")
            try:
                rows[ts] = float(raw) / 100.0
            except ValueError:
                rows[ts] = float("nan")
    s = pd.Series(rows, dtype="float64").sort_index().ffill().dropna()
    s.name = "dtb3"
    return s
