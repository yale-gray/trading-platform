"""``BarDriver``, ``TargetBook`` and the engine-config builder (SPEC-002 section 6, ADR-0001 D3-D5).

``BarDriver.step`` is the single per-bar loop shared by ``run_backtest`` and the paper
runner: process the bar in the engine (fills, marks, stops, rebalance decision), then,
for each strategy sleeve, evaluate the strategy on decision bars and submit target
weights on decision bars or ``rebalance_planned`` bars. Signals made at ``ts`` therefore
fill at the next bar. ``ts.date()`` is converted to ``asof`` here and nowhere else.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from tradebot.data.symbols import asset_class, normalize_symbol
from tradebot.portfolio.config import EngineConfig
from tradebot.portfolio.engine import BarResult, PortfolioEngine, TargetResult
from tradebot.sim.broker import Bar
from tradebot.strategy import indicators
from tradebot.strategy.engine import CompiledStrategy, Features, compile_strategy
from tradebot.strategy.schema import RotationSpec, RulesSpec, StrategySpec
from tradebot.strategy.signals import HeldPosition, SignalSet

if TYPE_CHECKING:
    from tradebot.backtest.engine import SleeveSpec

__all__ = ["BarDriver", "StepResult", "TargetBook", "build_engine_config"]

ATR_PERIOD = 14


# ---------------------------------------------------------------------------- TargetBook


class TargetBook:
    """Per-sleeve target weights from each sleeve's latest decision bar (ADR D3, A1).

    JSON-serialisable via :meth:`state` / the ``state`` constructor argument; the runner
    persists it in its per-bar checkpoint.
    """

    def __init__(self, sleeve_ids: Sequence[str], state: Mapping[str, Any] | None = None) -> None:
        ids = list(sleeve_ids)
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate sleeve ids: {ids}")
        self._weights: dict[str, dict[str, float]] = {sid: {} for sid in ids}
        self._ts: dict[str, pd.Timestamp | None] = {sid: None for sid in ids}
        if state is not None:
            sleeves = state.get("sleeves")
            if not isinstance(sleeves, Mapping):
                raise ValueError("TargetBook state needs a 'sleeves' mapping")
            unknown = sorted(set(sleeves) - set(ids))
            if unknown:
                raise ValueError(f"TargetBook state has unknown sleeves: {unknown}")
            for sid, entry in sleeves.items():
                raw_ts = entry.get("decision_ts")
                self._ts[sid] = pd.Timestamp(raw_ts) if raw_ts is not None else None
                self._weights[sid] = {
                    str(sym): float(w) for sym, w in sorted(entry.get("weights", {}).items())
                }

    def update(self, signals: SignalSet) -> None:
        """Replace the sleeve's book with the weights of the ``long`` intents (never merges).

        A ``SignalSet`` with ``decision=False`` is ignored.
        """
        if signals.sleeve_id not in self._weights:
            raise KeyError(f"unknown sleeve {signals.sleeve_id!r}")
        if not signals.decision:
            return
        book = {
            i.symbol: float(i.weight)
            for i in sorted(signals.intents, key=lambda i: i.symbol)
            if i.target == "long" and i.weight > 0
        }
        self._weights[signals.sleeve_id] = book
        self._ts[signals.sleeve_id] = pd.Timestamp(signals.asof)

    def weights(self, sleeve_id: str) -> dict[str, float]:
        """Copy of the positive target weights (``{}`` if no decision yet)."""
        return dict(self._weights[sleeve_id])

    def has_decision(self, sleeve_id: str) -> bool:
        """True once the sleeve has had a decision bar."""
        return self._ts[sleeve_id] is not None

    def last_decision_ts(self, sleeve_id: str) -> pd.Timestamp | None:
        """Timestamp of the sleeve's latest decision bar, or ``None``."""
        return self._ts[sleeve_id]

    def state(self) -> dict[str, Any]:
        """JSON-serialisable snapshot (floats round-trip exactly through ``json``)."""
        return {
            "sleeves": {
                sid: {
                    "weights": dict(sorted(self._weights[sid].items())),
                    "decision_ts": self._ts[sid].isoformat() if self._ts[sid] is not None else None,
                }
                for sid in sorted(self._weights)
            }
        }

    @classmethod
    def from_state(cls, sleeve_ids: Sequence[str], state: Mapping[str, Any]) -> TargetBook:
        """Rebuild a book from :meth:`state` output."""
        return cls(sleeve_ids, state)


# ---------------------------------------------------------------------------- config


class StepResult(BaseModel):
    """What one ``BarDriver.step`` did."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    asof: date
    ts: pd.Timestamp
    bar_result: BarResult
    signals: dict[str, SignalSet] = {}  # sleeves that were on a decision bar
    targets: dict[str, TargetResult] = {}  # sleeves for which submit_target_weights was called


def _classes(spec: StrategySpec) -> list[str]:
    symbols = list(spec.universe)
    if isinstance(spec, RotationSpec) and spec.fallback is not None:
        symbols.append(spec.fallback.symbol)
    return symbols


def build_engine_config(base: EngineConfig, sleeves: Sequence[SleeveSpec]) -> EngineConfig:
    """Copy of ``base`` with each sleeve's ATR-stop multiple set (``None`` when no stop).

    Raises ``ValueError`` if the sleeve ids differ from ``base``, a stop period is not 14,
    a strategy has more than one stop, a universe symbol is outside the sleeve's allowed
    asset classes, or a rules sleeve's ``len(universe) * weight`` exceeds 1.
    """
    ids = [s.sleeve_id for s in sleeves]
    if sorted(ids) != sorted(s.sleeve_id for s in base.sleeves) or len(set(ids)) != len(ids):
        raise ValueError(
            f"sleeve ids differ: spec {sorted(ids)} vs engine "
            f"{sorted(s.sleeve_id for s in base.sleeves)}"
        )
    by_id = {s.sleeve_id: s for s in sleeves}
    new: list[Any] = []
    for sc in base.sleeves:
        strategy = by_id[sc.sleeve_id].strategy
        mult: Decimal | None = None
        if strategy is not None:
            resolved = strategy.resolve()
            if len(resolved.stops) > 1:
                raise ValueError(f"{sc.sleeve_id}: more than one stop is not supported")
            if resolved.stops:
                stop = resolved.stops[0]
                if int(stop.period) != ATR_PERIOD:
                    raise ValueError(
                        f"{sc.sleeve_id}: atr_trailing period must be {ATR_PERIOD}, "
                        f"got {stop.period}"
                    )
                mult = Decimal(str(stop.mult))
            for sym in _classes(resolved):
                ac = asset_class(normalize_symbol(sym))
                if ac not in sc.allowed_asset_classes:
                    raise ValueError(
                        f"{sc.sleeve_id}: {sym} ({ac.value}) is outside the sleeve's allowed "
                        "asset classes"
                    )
            if isinstance(resolved, RulesSpec):
                w = resolved.position.weight
                per = 1.0 / len(resolved.universe) if w == "equal" else float(w)
                if len(resolved.universe) * per > 1.0 + 1e-9:
                    raise ValueError(
                        f"{sc.sleeve_id}: len(universe) * weight = "
                        f"{len(resolved.universe) * per:.4f} exceeds 1"
                    )
        new.append(sc.model_copy(update={"trailing_stop_atr_mult": mult}))
    return base.model_copy(update={"sleeves": tuple(new)})


# ---------------------------------------------------------------------------- driver


@dataclass
class _Slot:
    sleeve_id: str
    compiled: CompiledStrategy
    feats: Features


def _opt(x: float) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


class BarDriver:
    """The one per-bar loop (backtest and runner). Compiles a fresh strategy per driver."""

    def __init__(
        self,
        engine: PortfolioEngine,
        sleeves: Sequence[SleeveSpec],
        bars: Mapping[str, pd.DataFrame],
        *,
        start: date,
        book: TargetBook | None = None,
    ) -> None:
        self._engine = engine
        self._start = start
        engine_ids = set(engine.sleeve_ids)
        ids = [s.sleeve_id for s in sleeves]
        if set(ids) != engine_ids or len(set(ids)) != len(ids):
            raise ValueError("sleeve ids differ between the driver and the engine")
        self._strategy_ids = sorted(s.sleeve_id for s in sleeves if s.strategy is not None)
        self._book = book if book is not None else TargetBook(self._strategy_ids)
        missing_book = sorted(set(self._strategy_ids) - set(self._book.state()["sleeves"]))
        if missing_book:
            raise ValueError(f"book lacks strategy sleeves {missing_book}")
        self._frames: dict[str, pd.DataFrame] = {
            str(normalize_symbol(k)): v for k, v in bars.items()
        }
        self._slots: list[_Slot] = []
        required: set[str] = set()
        by_id = {s.sleeve_id: s for s in sleeves}
        for sid in self._strategy_ids:
            strategy = by_id[sid].strategy
            assert strategy is not None
            compiled = compile_strategy(strategy, sid)  # fresh per driver (audit F1)
            feats = compiled.prepare(self._frames)
            self._slots.append(_Slot(sid, compiled, feats))
            required.update(compiled.required)
        self._symbols = sorted(required)
        # Causal per-symbol arrays; vol20/atr14 are attached to each Bar (ADR D4).
        self._arrays: dict[str, dict[str, np.ndarray]] = {}
        self._index: dict[str, pd.DatetimeIndex] = {}
        for sym in self._symbols:
            df = self._frames[sym]
            self._index[sym] = df.index
            self._arrays[sym] = {
                "open": df["open"].to_numpy(dtype="float64"),
                "high": df["high"].to_numpy(dtype="float64"),
                "low": df["low"].to_numpy(dtype="float64"),
                "close": df["close"].to_numpy(dtype="float64"),
                "vol20": indicators.vol(df["close"], 20).to_numpy(),
                "atr14": indicators.atr(df["high"], df["low"], df["close"], ATR_PERIOD).to_numpy(),
            }

    @property
    def book(self) -> TargetBook:
        """The driver's target book (persist ``book.state()`` per bar)."""
        return self._book

    def master_calendar(
        self, end: date | None = None, after: date | None = None
    ) -> pd.DatetimeIndex:
        """Sorted union of required symbols' timestamps in ``[start, end]``, dates > ``after``."""
        cal: pd.DatetimeIndex | None = None
        for sym in self._symbols:
            cal = self._index[sym] if cal is None else cal.union(self._index[sym])
        if cal is None:
            return pd.DatetimeIndex([], tz="UTC")
        dates = np.array([d for d in cal.date])
        keep = dates >= self._start
        if end is not None:
            keep &= dates <= end
        if after is not None:
            keep &= dates > after
        return pd.DatetimeIndex(cal[keep]).sort_values()

    def _bars_at(self, ts: pd.Timestamp) -> dict[str, Bar]:
        out: dict[str, Bar] = {}
        for sym in self._symbols:
            i = int(self._index[sym].get_indexer([ts])[0])
            if i < 0:
                continue
            a = self._arrays[sym]
            out[sym] = Bar(
                symbol=sym,
                open=float(a["open"][i]),
                high=float(a["high"][i]),
                low=float(a["low"][i]),
                close=float(a["close"][i]),
                vol20=_opt(a["vol20"][i]),
                atr14=_opt(a["atr14"][i]),
            )
        return out

    def step(self, ts: pd.Timestamp) -> StepResult:
        """Process ONE bar: engine first (fills), then signals and target submission."""
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            raise ValueError("ts must be tz-aware (UTC)")
        asof = ts.date()  # the only Timestamp -> date conversion (ADR D1)
        result = self._engine.process_bar(asof, self._bars_at(ts))
        snapshot = self._engine.snapshot()
        positions = {s.sleeve_id: s.positions for s in snapshot.sleeves}
        signals: dict[str, SignalSet] = {}
        targets: dict[str, TargetResult] = {}
        for slot in self._slots:
            sid = slot.sleeve_id
            i = int(slot.feats.calendar.get_indexer([ts])[0])
            force = not self._book.has_decision(sid)
            decision = (
                i >= 0
                and asof >= self._start
                and i >= slot.compiled.warmup_bars - 1
                and (force or slot.compiled.is_decision_bar(ts, slot.feats))
            )
            if decision:
                held = {
                    p.symbol: HeldPosition(
                        symbol=p.symbol, entry_ts=pd.Timestamp(p.entry_asof, tz="UTC")
                    )
                    for p in positions[sid]
                    if p.qty > 0 and p.entry_asof is not None
                }
                sig = slot.compiled.evaluate(slot.feats, ts, held, force_decision=force)
                self._book.update(sig)
                signals[sid] = sig
            if decision or result.rebalance_planned:
                targets[sid] = self._engine.submit_target_weights(
                    sid, self._book.weights(sid), asof
                )
        return StepResult(asof=asof, ts=ts, bar_result=result, signals=signals, targets=targets)

    def run(self, end: date | None = None, after: date | None = None) -> Iterator[StepResult]:
        """Step every master-calendar bar in order (backtest loop and runner catch-up).

        ``after`` defaults to the engine's ``last_processed_asof`` so a restored engine
        replays only new bars.
        """
        if after is None:
            after = self._engine.last_processed_asof
        for ts in self.master_calendar(end=end, after=after):
            yield self.step(ts)
