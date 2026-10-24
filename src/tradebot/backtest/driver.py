"""``BarDriver``, ``TargetBook`` and the engine-config builder (SPEC-002 section 6, ADR-0001 D3-D5).

``BarDriver.step`` is the single per-bar loop shared by ``run_backtest`` and the paper
runner: process the bar in the engine (fills, marks, stops, rebalance decision), then,
for each strategy sleeve, evaluate the strategy on decision bars and submit target
weights on decision bars or ``rebalance_planned`` bars. Signals made at ``ts`` therefore
fill at the next bar. ``ts`` is converted to ``asof`` here and nowhere else: ``ts.date()`` for
daily strategies, the UTC bar-open ``datetime`` for 15-minute ones (ADR-0001 A21).

Intraday mode (``timeframe: 15m``): the driver knows the session structure from the calendar of
the bars. On a session's last bar it tells the engine (``session_end=True``, which flattens an
``intraday_flatten`` account at that bar's close) and submits no new targets, and it clears the
target books so nothing is bought into the close. With 500 symbols it runs the engine in
``lean`` mode, building ``Bar`` objects only for symbols the engine asks about.

Decision lag (ADR-0001 A22): with ``decision_lag_bars = L`` a signal computed on bar t's close is
submitted while stepping bar t+L, so it fills at bar t+1+L's open. The default is
``INTRADAY_DECISION_LAG`` = 2 for 15m strategies (the free SIP feed arrives after bar t+1 has
opened) and 0 for daily ones, so backtest and runner behave identically. A clock-only sleeve
(``StrategySpec.clock_only()``, A28) has nothing to wait for and uses lag 0 unless the caller
passes ``decision_lag_bars`` explicitly. In a flattening account a decision never reaches back
across a session boundary, and if a session's last bar never arrived the first bar of the next
session sells whatever is still held at its open (``FLATTEN_LATE``, audit 007 m7). A swing
account (15m, ``intraday_flatten=False``, A27) holds overnight: its decisions may cross sessions
and pending orders carry to the next session's first bar.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from tradebot.data.calendar import bar_close_time
from tradebot.data.symbols import asset_class, normalize_symbol
from tradebot.portfolio.asof import AsOf, asof_timestamp
from tradebot.portfolio.config import EngineConfig
from tradebot.portfolio.engine import BarResult, PortfolioEngine, TargetResult
from tradebot.portfolio.models import EventType
from tradebot.sim.broker import Bar
from tradebot.strategy import indicators
from tradebot.strategy.engine import CompiledStrategy, Features, compile_strategy
from tradebot.strategy.schema import RotationSpec, RulesSpec, StrategySpec
from tradebot.strategy.signals import HeldPosition, SignalSet

if TYPE_CHECKING:
    from tradebot.backtest.engine import SleeveSpec

__all__ = ["BarDriver", "StepResult", "TargetBook", "build_engine_config"]

ATR_PERIOD = 14
BAR_15M = pd.Timedelta(minutes=15)
_NY = "America/New_York"


# ---------------------------------------------------------------------------- TargetBook


# A22: SIP bars arrive ~31 min after they open (free plan), i.e. after bar t+2 opens,
# so the earliest achievable live fill for a bar-t signal is bar t+3's open.
INTRADAY_DECISION_LAG = 2


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

    def clear(self, sleeve_id: str) -> None:
        """Drop the sleeve's target weights (end of an intraday session); keeps its stamp."""
        if sleeve_id not in self._weights:
            raise KeyError(f"unknown sleeve {sleeve_id!r}")
        self._weights[sleeve_id] = {}

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

    asof: AsOf
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

    Raises ``ValueError`` if the sleeve ids differ from ``base``, ``intraday_flatten`` is set
    but a strategy is not a 15m ``bar``-schedule one, a stop period is not 14,
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
            if base.intraday_flatten and (
                resolved.timeframe != "15m" or resolved.schedule.freq != "bar"
            ):
                raise ValueError(
                    f"{sc.sleeve_id}: intraday_flatten needs a 15m strategy scheduled by 'bar'"
                )
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
                slots = (
                    int(resolved.position.max_positions)  # type: ignore[arg-type]
                    if resolved.position.max_positions is not None
                    else len(resolved.universe)
                )
                per = 1.0 / slots if w == "equal" else float(w)
                if slots * per > 1.0 + 1e-9:
                    what = "len(universe)" if slots == len(resolved.universe) else "max_positions"
                    raise ValueError(
                        f"{sc.sleeve_id}: {what} * weight = {slots * per:.4f} exceeds 1"
                    )
        new.append(sc.model_copy(update={"trailing_stop_atr_mult": mult}))
    return base.model_copy(update={"sleeves": tuple(new)})


# ---------------------------------------------------------------------------- driver


@dataclass
class _Slot:
    sleeve_id: str
    compiled: CompiledStrategy
    feats: Features
    lag: int = 0
    clock_only: bool = False


def _opt(x: float) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


class _LazyBars(Mapping[str, Bar]):
    """The bars of one timestamp, built on demand (the engine only probes the symbols it needs)."""

    def __init__(self, driver: BarDriver, ts: pd.Timestamp) -> None:
        self._d = driver
        self._ts = ts

    def __contains__(self, sym: object) -> bool:
        return isinstance(sym, str) and self._d._row(sym, self._ts) >= 0

    def __getitem__(self, sym: str) -> Bar:
        bar = self._d._bar(sym, self._ts)
        if bar is None:
            raise KeyError(sym)
        return bar

    def last_known(self, sym: str) -> Bar | None:
        """The symbol's latest bar at or before this timestamp that the run has processed.

        The engine marks a targeted symbol that has no bar *now* with this, exactly as full
        marking would have: it kept the close of the symbol's last processed bar.
        """
        return self._d._last_bar(sym, self._ts)

    def __iter__(self) -> Iterator[str]:
        return (s for s in self._d._symbols if s in self)

    def __len__(self) -> int:
        return sum(1 for _ in self)


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
        lean: bool | None = None,
        decision_lag_bars: int | None = None,
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
            self._slots.append(_Slot(sid, compiled, feats, clock_only=compiled.spec.clock_only()))
            required.update(compiled.required)
        self._symbols = sorted(required)
        timeframes = {self._slot_timeframe(sl) for sl in self._slots}
        if len(timeframes) > 1:
            raise ValueError(f"strategies mix timeframes {sorted(timeframes)}")
        self._intraday = timeframes == {"15m"}
        self._lean = self._intraday if lean is None else lean
        if decision_lag_bars is not None and int(decision_lag_bars) < 0:
            raise ValueError(f"decision_lag_bars must be >= 0, got {decision_lag_bars}")
        # base lag of a data-reading sleeve; a clock-only sleeve may use 0 (A28), see _Slot.lag
        self._lag = (
            int(decision_lag_bars)
            if decision_lag_bars is not None
            else (INTRADAY_DECISION_LAG if self._intraday else 0)
        )
        for sl in self._slots:
            if decision_lag_bars is not None:
                sl.lag = int(decision_lag_bars)
            elif self._intraday and not sl.clock_only:
                sl.lag = INTRADAY_DECISION_LAG
            else:
                sl.lag = 0  # daily, or a clock-only rule (A28)
        self._flatten = bool(getattr(getattr(engine, "config", None), "intraday_flatten", False))
        # Causal per-symbol arrays; vol20/atr14 are attached to each Bar (ADR D4). Built per
        # symbol on first use: a 500-symbol intraday run only ever needs the traded ones.
        self._arrays: dict[str, dict[str, np.ndarray]] = {}
        self._index: dict[str, pd.DatetimeIndex] = {}
        self._ns: dict[str, np.ndarray] = {}  # bar-open nanoseconds per symbol (sorted)
        for sym in self._symbols:
            self._index[sym] = self._frames[sym].index
            self._ns[sym] = self._frames[sym].index.asi8
        cal: pd.DatetimeIndex | None = None
        for sym in self._symbols:
            cal = self._index[sym] if cal is None else cal.union(self._index[sym])
        self._cal: pd.DatetimeIndex = (
            pd.DatetimeIndex([], tz="UTC") if cal is None else pd.DatetimeIndex(cal).sort_values()
        )
        self._session_end_ns: set[int] = (
            self._find_session_ends(self._cal) if self._intraday else set()
        )

    @staticmethod
    def _slot_timeframe(slot: _Slot) -> str:
        return str(slot.compiled.spec.timeframe)

    @staticmethod
    def _find_session_ends(cal: pd.DatetimeIndex) -> set[int]:
        """Bar-open ns of each session's last bar.

        A session's last bar is the last timestamp of its New York day, except the final day of
        the data: that one only counts when its last bar closes the exchange session (the data
        may stop mid-day while a live run is under way). An early close is read from the XNYS
        calendar; a day the calendar does not know is trusted as complete.
        """
        if len(cal) == 0:
            return set()
        days = cal.tz_convert(_NY).normalize()
        ends: set[int] = set()
        for k in range(len(cal)):
            if k + 1 < len(cal) and days[k + 1] != days[k]:
                ends.add(int(cal.asi8[k]))
        last = len(cal) - 1
        day = days[last].date()
        try:
            complete = cal[last] + BAR_15M >= pd.Timestamp(bar_close_time("SPY", day))
        except ValueError:
            complete = True
        if complete:
            ends.add(int(cal.asi8[last]))
        return ends

    def is_session_end(self, ts: pd.Timestamp) -> bool:
        """True if ``ts`` is the last bar of its session (intraday mode only)."""
        return int(pd.Timestamp(ts).value) in self._session_end_ns

    @property
    def book(self) -> TargetBook:
        """The driver's target book (persist ``book.state()`` per bar)."""
        return self._book

    def master_calendar(
        self, end: date | None = None, after: AsOf | None = None
    ) -> pd.DatetimeIndex:
        """Sorted union of required symbols' timestamps in ``[start, end]``, dates > ``after``."""
        cal = self._cal
        if len(cal) == 0:
            return cal
        dates = np.array([d for d in cal.date])
        keep = dates >= self._start
        if end is not None:
            keep &= dates <= end
        if after is not None:
            if isinstance(after, datetime):  # intraday engine: resume after a bar timestamp
                keep &= cal > asof_timestamp(after)
            else:
                keep &= dates > after
        return pd.DatetimeIndex(cal[keep]).sort_values()

    def _row(self, sym: str, ts: pd.Timestamp) -> int:
        """Row of ``ts`` in ``sym``'s bars, or -1."""
        ns = self._ns.get(sym)
        if ns is None:
            return -1
        t = int(ts.value)
        i = int(np.searchsorted(ns, t))
        return i if i < len(ns) and ns[i] == t else -1

    def _sym_arrays(self, sym: str) -> dict[str, np.ndarray]:
        a = self._arrays.get(sym)
        if a is None:
            df = self._frames[sym]
            a = self._arrays[sym] = {
                "open": df["open"].to_numpy(dtype="float64"),
                "high": df["high"].to_numpy(dtype="float64"),
                "low": df["low"].to_numpy(dtype="float64"),
                "close": df["close"].to_numpy(dtype="float64"),
                "vol20": indicators.vol(df["close"], 20).to_numpy(),
                "atr14": indicators.atr(df["high"], df["low"], df["close"], ATR_PERIOD).to_numpy(),
            }
        return a

    def _last_bar(self, sym: str, ts: pd.Timestamp) -> Bar | None:
        """Latest bar of ``sym`` at or before ``ts`` and on/after the run's first day, or None."""
        ns = self._ns.get(sym)
        if ns is None:
            return None
        i = int(np.searchsorted(ns, int(ts.value), side="right")) - 1
        if i < 0 or self._index[sym][i].date() < self._start:
            return None
        return self._bar(sym, self._index[sym][i])

    def _bar(self, sym: str, ts: pd.Timestamp) -> Bar | None:
        i = self._row(sym, ts)
        if i < 0:
            return None
        a = self._sym_arrays(sym)
        return Bar(
            symbol=sym,
            open=float(a["open"][i]),
            high=float(a["high"][i]),
            low=float(a["low"][i]),
            close=float(a["close"][i]),
            vol20=_opt(a["vol20"][i]),
            atr14=_opt(a["atr14"][i]),
        )

    def _bars_at(self, ts: pd.Timestamp) -> Mapping[str, Bar]:
        if self._lean:
            return _LazyBars(self, ts)
        out: dict[str, Bar] = {}
        for sym in self._symbols:
            bar = self._bar(sym, ts)
            if bar is not None:
                out[sym] = bar
        return out

    def _delisted_at(self, ts: pd.Timestamp) -> dict[str, bool]:
        """Held symbols without a bar on this daily master-calendar bar (A33 B1).

        Maps each to its write-off flag: its last close is at least 50% below the close five
        bars before its last bar. Empty unless ``EngineConfig.force_sell_missing`` is set;
        never used by intraday runs. Causal: only past bars of the symbol are read.
        """
        cfg = getattr(self._engine, "config", None)
        if self._intraday or not getattr(cfg, "force_sell_missing", False):
            return {}
        out: dict[str, bool] = {}
        for sym in self._engine.held_symbols():
            if self._row(sym, ts) >= 0 or sym not in self._ns:
                continue
            i = int(np.searchsorted(self._ns[sym], int(ts.value), side="right")) - 1
            if i < 0:
                continue
            close = self._sym_arrays(sym)["close"]
            ref = float(close[max(i - 5, 0)])
            out[sym] = ref > 0 and float(close[i]) / ref - 1.0 <= -0.5
        return out

    def step(self, ts: pd.Timestamp) -> StepResult:
        """Process ONE bar: engine first (fills), then signals and target submission."""
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            raise ValueError("ts must be tz-aware (UTC)")
        day = ts.date()
        asof: AsOf = ts.to_pydatetime() if self._intraday else day  # the only conversion (ADR D1)
        session_end = self._intraday and self.is_session_end(ts)
        extra: dict[str, Any] = {}
        if self._lean:
            extra["lean"] = True
        if session_end:
            extra["session_end"] = True
        if self._flatten and self._stale_session(ts):
            extra["late_flatten"] = True
        delisted = self._delisted_at(ts)
        if delisted:
            extra["delisted"] = delisted
        result = self._engine.process_bar(asof, self._bars_at(ts), **extra)
        if session_end and self._flatten:
            # Positions were just sold at this close; buying now would carry overnight.
            for sid in self._strategy_ids:
                self._book.clear(sid)
            return StepResult(asof=asof, ts=ts, bar_result=result, signals={}, targets={})
        snapshot = self._engine.snapshot()
        positions = {s.sleeve_id: s.positions for s in snapshot.sleeves}
        # After a kill-switch re-arm every sleeve gets one decision (monthly sleeves would
        # otherwise stay flat until their next decision bar).
        rearmed = any(e.type is EventType.KILL_SWITCH_REARMED for e in result.new_ledger_events)
        signals: dict[str, SignalSet] = {}
        targets: dict[str, TargetResult] = {}
        for slot in self._slots:
            sid = slot.sleeve_id
            i = int(slot.feats.calendar.get_indexer([ts])[0])
            force = rearmed or not self._book.has_decision(sid)
            dts = self._decision_bar(slot, i, ts)  # the bar whose close the signal is read from
            decision = (
                dts is not None
                and dts.date() >= self._start
                and i - slot.lag >= slot.compiled.warmup_bars - 1
                and (force or slot.compiled.is_decision_bar(dts, slot.feats))
            )
            if decision:
                assert dts is not None
                held = {
                    p.symbol: HeldPosition(symbol=p.symbol, entry_ts=asof_timestamp(p.entry_asof))
                    for p in positions[sid]
                    if p.qty > 0 and p.entry_asof is not None
                }
                sig = slot.compiled.evaluate(slot.feats, dts, held, force_decision=force)
                self._book.update(sig)
                signals[sid] = sig
            if decision or result.rebalance_planned:
                targets[sid] = self._engine.submit_target_weights(
                    sid, self._book.weights(sid), asof
                )
        return StepResult(asof=asof, ts=ts, bar_result=result, signals=signals, targets=targets)

    def _decision_bar(self, slot: _Slot, i: int, ts: pd.Timestamp) -> pd.Timestamp | None:
        """The bar to evaluate while stepping ``ts`` (row ``i``): ``lag`` rows earlier, or None.

        None if ``ts`` is not in the strategy calendar, the row does not exist, or (intraday,
        flattening account) the decision bar belongs to an earlier session than ``ts``; a swing
        account (A27) lets decisions cross sessions.
        """
        if i < 0 or i - slot.lag < 0:
            return None
        if slot.lag == 0:
            return ts
        dts = pd.Timestamp(slot.feats.calendar[i - slot.lag])
        if (
            self._intraday
            and self._flatten
            and dts.tz_convert(_NY).date() != ts.tz_convert(_NY).date()
        ):
            return None
        return dts

    def _stale_session(self, ts: pd.Timestamp) -> bool:
        """True if the engine's last processed bar is from an earlier NY session than ``ts``."""
        last = self._engine.last_processed_asof
        if last is None or not isinstance(last, datetime):
            return False
        return asof_timestamp(last).tz_convert(_NY).date() < ts.tz_convert(_NY).date()

    def run(self, end: date | None = None, after: AsOf | None = None) -> Iterator[StepResult]:
        """Step every master-calendar bar in order (backtest loop and runner catch-up).

        ``after`` defaults to the engine's ``last_processed_asof`` so a restored engine
        replays only new bars.
        """
        if after is None:
            after = self._engine.last_processed_asof
        for ts in self.master_calendar(end=end, after=after):
            yield self.step(ts)
