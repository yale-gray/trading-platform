"""Golden-backtest harness (SPEC-005 section 5).

Builds the four pinned configs on the committed synthetic fixtures (read through the real
``MarketData`` cache path), runs them, and turns the result into the golden dict shape.
Never rewrites a golden silently: writing needs ``--update-golden`` *and* an existing note file
(see ``conftest.py``), and ``unapproved_goldens`` fails CI for a golden whose hash no
PM-approved note lists.

Start date: SPEC-005 says 2018-01-02, the first fixture bar, but SPEC-002 section 6 requires
``warmup_bars`` of history before ``start`` (enforced by ``run_backtest``), so every golden
starts 2019-01-02 (about 250 bars of warm-up) and ends 2024-12-31.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from tradebot.backtest.driver import BarDriver, build_engine_config
from tradebot.backtest.engine import (
    BacktestConfig,
    BacktestReport,
    SleeveSpec,
    run_with_benchmark,
)
from tradebot.clock import FixedClock
from tradebot.data import MarketData
from tradebot.data.config import DataConfig
from tradebot.data.providers.fixture_provider import FixtureProvider
from tradebot.portfolio.config import EngineConfig, default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType, FillSide
from tradebot.risk.config import RiskConfig
from tradebot.strategy.io import load_strategy

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "market"
STRATEGIES = ROOT / "strategies"
GOLDEN_DIR = Path(__file__).resolve().parent
NOTES_DIR = ROOT / "docs" / "reviews" / "golden"

SCHEMA_VERSION = 1
START = date(2019, 1, 2)
END = date(2024, 12, 31)
IDS = ("G1", "G2", "G3", "G4")
SLEEVE_OF = {"G1": "mean_reversion", "G2": "core_momentum", "G3": "crypto_trend"}
FIXTURE_SYMBOLS = [
    "SPY",
    "QQQ",
    "IWM",
    "EFA",
    "EEM",
    "TLT",
    "GLD",
    "XLK",
    "XLF",
    "XLE",
    "XLV",
    "XLI",
    "XLY",
    "XLP",
    "XLU",
    "XLB",
    "IEF",
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
]
FLOAT_TOL = 1e-9
_METRIC_FIELDS = (
    "total_return",
    "cagr",
    "max_drawdown",
    "sharpe",
    "sortino",
    "win_rate",
    "exposure_avg",
)

_BARS: dict[str, pd.DataFrame] | None = None
_TMP: tempfile.TemporaryDirectory[str] | None = None


# ---------------------------------------------------------------------------- data


def load_bars() -> dict[str, pd.DataFrame]:
    """All 20 fixture symbols through the real cache path (offline, FixtureProvider)."""
    global _BARS, _TMP
    if _BARS is None:
        _TMP = tempfile.TemporaryDirectory(prefix="golden-cache-")
        md = MarketData(
            DataConfig(cache_dir=Path(_TMP.name)),
            providers=[FixtureProvider(FIXTURES)],
            clock=FixedClock(datetime(2025, 1, 1, 12, 0, tzinfo=UTC)),
        )
        got = md.get_bars_many(FIXTURE_SYMBOLS, date(2018, 1, 2), END)
        _BARS = {str(k): v for k, v in got.items()}
    return _BARS


# ---------------------------------------------------------------------------- configs


def engine_for(sleeve_id: str | None = None) -> EngineConfig:
    """Default 50/20/20/10 fund (G4) or one sleeve at weight 1 with the fund caps relaxed."""
    base = default_config()
    if sleeve_id is None:
        return base
    sleeve = next(s for s in base.sleeves if s.sleeve_id == sleeve_id)
    relaxed = RiskConfig(
        max_asset_fraction=Decimal(1),
        max_crypto_fraction=Decimal(1),
        kill_switch_drawdown=Decimal(1),
    )
    return base.model_copy(
        update={
            "sleeves": (sleeve.model_copy(update={"target_weight": Decimal(1)}),),
            "risk": relaxed,
        }
    )


def golden_config(gid: str, engine: EngineConfig | None = None) -> BacktestConfig:
    """The pinned config for G1..G4 (optionally with another ``EngineConfig`` for meta-tests)."""
    if gid == "G4":
        eng = engine or engine_for(None)
        sleeves = [
            SleeveSpec(
                sleeve_id=s.sleeve_id,
                strategy=(
                    load_strategy(STRATEGIES / f"{s.sleeve_id}.yaml")
                    if s.sleeve_id != "cash"
                    else None
                ),
            )
            for s in eng.sleeves
        ]
        return BacktestConfig(sleeves=sleeves, start=START, end=END, engine=eng)
    sid = SLEEVE_OF[gid]
    eng = engine or engine_for(sid)
    sleeves = [SleeveSpec(sleeve_id=sid, strategy=load_strategy(STRATEGIES / f"{sid}.yaml"))]
    return BacktestConfig(sleeves=sleeves, start=START, end=END, engine=eng)


# ---------------------------------------------------------------------------- run + dict


@dataclass
class GoldenRun:
    """A golden run: the benchmarked report plus the engine's final state and fills."""

    cfg: BacktestConfig
    report: BacktestReport
    final_equity: Decimal
    final_cash: Decimal
    final_qty: dict[str, Decimal]
    fills: list[dict[str, str]]


def _engine_run(
    cfg: BacktestConfig, bars: Mapping[str, pd.DataFrame]
) -> tuple[PortfolioEngine, list[dict[str, str]]]:
    """Replay ``run_backtest``'s loop to reach the engine (its snapshot and orders)."""
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
    driver = BarDriver(engine, cfg.sleeves, bars, start=cfg.start)
    for _ in driver.run(end=cfg.end):
        pass
    return engine, fills_of(engine)


def fills_of(engine: PortfolioEngine) -> list[dict[str, str]]:
    """Every FILL as a string dict (symbol, side, qty, price, fee, signal_ts, fill_ts)."""
    orders = {o.order_id: o for o in engine.orders()}
    out: list[dict[str, str]] = []
    for ev in engine.ledger:
        if ev.type is not EventType.FILL:
            continue
        assert ev.order_id is not None and ev.side is not None
        o = orders[ev.order_id]
        out.append(
            {
                "seq": str(ev.seq),
                "sleeve": ev.sleeve_id,
                "symbol": str(ev.symbol),
                "side": "BUY" if ev.side is FillSide.BUY else "SELL",
                "qty": str(ev.qty),
                "price": str(ev.price),
                "fee": str(o.fee),
                "signal_ts": o.created_asof.isoformat(),
                "fill_ts": ev.asof.isoformat(),
            }
        )
    return out


_RUNS: dict[str, GoldenRun] = {}


def run_golden(gid: str) -> GoldenRun:
    """Run (memoised per process) one golden config with its benchmark."""
    if gid not in _RUNS:
        bars = load_bars()
        cfg = golden_config(gid)
        report = run_with_benchmark(cfg, bars)
        engine, fills = _engine_run(cfg, bars)
        snap = engine.snapshot()
        qty: dict[str, Decimal] = {}
        for sl in snap.sleeves:
            for p in sl.positions:
                if p.qty:
                    qty[p.symbol] = qty.get(p.symbol, Decimal(0)) + p.qty
        _RUNS[gid] = GoldenRun(cfg, report, snap.equity, snap.cash, qty, fills)
    return _RUNS[gid]


def _f(x: float) -> float | None:
    return float(x) if math.isfinite(x) else None


def _metrics_dict(m: Any) -> dict[str, float | None]:
    return {k: _f(getattr(m, k)) for k in _METRIC_FIELDS} | {"trade_count": int(m.trade_count)}


def fills_sha(fills: list[dict[str, str]]) -> str:
    """sha256 of the canonical (sorted-key) JSON of the full fill list."""
    text = json.dumps(fills, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def golden_dict(gid: str, run: GoldenRun) -> dict[str, Any]:
    """The pinned shape (exact Decimal strings, float metrics, benchmark block)."""
    rep = run.report
    report_json = json.loads(rep.to_json())
    return {
        "schema_version": SCHEMA_VERSION,
        "id": gid,
        "start": START.isoformat(),
        "end": END.isoformat(),
        "config_hash": rep.result.config_hash,
        "data_fingerprint": rep.result.data_fingerprint,
        "final_equity": str(run.final_equity),
        "final_cash": str(run.final_cash),
        "final_qty": {k: str(v) for k, v in sorted(run.final_qty.items())},
        "fill_count": len(run.fills),
        "first_fills": run.fills[:5],
        "last_fills": run.fills[-5:],
        "fills_sha256": fills_sha(run.fills),
        "ledger_sha256": report_json["ledger_sha256"],
        "n_signals": rep.result.n_signals,
        "metrics": _metrics_dict(rep.metrics),
        "benchmark": {k: _metrics_dict(v) for k, v in sorted(rep.benchmark_metrics.items())},
    }


# ---------------------------------------------------------------------------- compare


def diff_golden(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """Field-by-field differences (exact for strings/ints, ``FLOAT_TOL`` for floats)."""
    out: list[str] = []
    _walk("", expected, actual, out)
    ef, af = expected.get("first_fills", []), actual.get("first_fills", [])
    for i, (e, a) in enumerate(zip(ef, af, strict=False)):
        if e != a:
            out.append(f"first diverging fill (first_fills[{i}]): expected {e} actual {a}")
            break
    return out


def _walk(path: str, e: Any, a: Any, out: list[str]) -> None:
    if isinstance(e, dict) and isinstance(a, dict):
        for k in sorted(set(e) | set(a)):
            if k not in e:
                out.append(f"{path}{k}: unexpected key in actual")
            elif k not in a:
                out.append(f"{path}{k}: missing in actual")
            else:
                _walk(f"{path}{k}.", e[k], a[k], out)
    elif isinstance(e, list) and isinstance(a, list):
        if len(e) != len(a):
            out.append(f"{path[:-1]}: length {len(e)} expected, {len(a)} actual")
        for i, (x, y) in enumerate(zip(e, a, strict=False)):
            _walk(f"{path[:-1]}[{i}].", x, y, out)
    elif isinstance(e, float) or isinstance(a, float):
        if e is None or a is None:
            if e is not a:
                out.append(f"{path[:-1]}: expected {e} actual {a}")
        elif abs(float(e) - float(a)) > FLOAT_TOL:
            out.append(f"{path[:-1]}: expected {e!r} actual {a!r} (|diff| > {FLOAT_TOL})")
    elif e != a:
        out.append(f"{path[:-1]}: expected {e!r} actual {a!r}")


def _has_benchmark_block(d: Mapping[str, Any]) -> bool:
    return bool(d.get("benchmark"))


def assert_matches_golden(gid: str, actual: Mapping[str, Any]) -> None:
    """Compare against ``tests/golden/<gid>.json``; raises ``AssertionError`` with a full diff."""
    path = GOLDEN_DIR / f"{gid}.json"
    expected = json.loads(path.read_text())
    assert _has_benchmark_block(expected), f"{gid}.json has no benchmark block"
    assert _has_benchmark_block(actual), f"{gid} report has no benchmark block"
    diffs = diff_golden(expected, actual)
    assert not diffs, f"{gid} differs from golden:\n  " + "\n  ".join(diffs)


# ---------------------------------------------------------------------------- update + notes


def write_golden(gid: str, data: Mapping[str, Any], note: Path | None) -> Path:
    """Write ``<gid>.json``; refuses unless ``note`` is an existing file."""
    if note is None or not note.is_file():
        raise PermissionError("golden update needs --golden-note pointing at an existing note")
    path = GOLDEN_DIR / f"{gid}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path


def file_sha256(path: Path) -> str:
    """sha256 of the file bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


_SHA = re.compile(r"\bG\d+=([0-9a-f]{64})\b")


def approved_hashes(notes_dir: Path = NOTES_DIR) -> set[str]:
    """Golden hashes listed in notes that carry an ``Approved-by: PM`` line."""
    out: set[str] = set()
    for note in sorted(notes_dir.glob("*.md")):
        text = note.read_text()
        if not re.search(r"^Approved-by:\s*PM\b", text, flags=re.MULTILINE):
            continue
        for line in text.splitlines():
            if line.startswith("golden-sha256:"):
                out.update(_SHA.findall(line))
    return out


def unapproved_goldens(golden_dir: Path = GOLDEN_DIR, notes_dir: Path = NOTES_DIR) -> list[str]:
    """Golden files whose current hash no PM-approved note lists."""
    approved = approved_hashes(notes_dir)
    return [p.name for p in sorted(golden_dir.glob("G*.json")) if file_sha256(p) not in approved]
