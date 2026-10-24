"""Strategy compilation and evaluation (SPEC-002 sections 1, 2, 2a).

``prepare`` is vectorised and causal (value at t uses bars <= t only; indicator computation is
de-duplicated). ``evaluate`` is pure: it reads feature row ``asof`` and the ``held`` mapping only.
It never computes ``vol20``/``atr14`` (``BarDriver`` does).

NaN semantics: any comparison with a NaN operand is *unknown*; unknown propagates through
``not`` (three-valued logic) and is coerced to False at the top, so a symbol with too little
history can neither be selected nor entered. Hold-while (omitted ``exit``) exits whenever entry
is *not confirmed True*, so a held symbol whose data goes missing is not held on faith.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from tradebot.data.calendar import sessions as xnys_sessions
from tradebot.data.events import fomc_dates
from tradebot.data.symbols import normalize_symbol
from tradebot.data.universe import sp500_member_flags
from tradebot.strategy.indicators import INDICATORS
from tradebot.strategy.schema import (
    AllOf,
    Always,
    AnyOf,
    CalendarOperand,
    Condition,
    CrossesAbove,
    CrossesBelow,
    Eq,
    EventOperand,
    Ge,
    Gt,
    IndOperand,
    Le,
    Lt,
    Not,
    PosOperand,
    PriceOperand,
    RotationSpec,
    RulesSpec,
    SessionOperand,
    StopRequest,
    StrategySpec,
    UniverseOperand,
)
from tradebot.strategy.signals import HeldPosition, Intent, SignalSet

_NY = "America/New_York"
_Tri = bool | None  # None = unknown (NaN involved)
_OHLCV = ("open", "high", "low", "close", "volume")
_CMP = {Gt: "gt", Ge: "ge", Lt: "lt", Le: "le", Eq: "eq"}
_CMP_FN: dict[str, Callable[[float, float], bool]] = {
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}


def _canon(sym: str) -> str:
    return str(normalize_symbol(sym))


@dataclass
class Features:
    """Causal feature arrays aligned to the strategy calendar (one float array per operand)."""

    calendar: pd.DatetimeIndex
    arrays: dict[tuple, np.ndarray] = field(default_factory=dict)
    decision_flags: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))


# An operand reader: (features, context symbol, calendar position, bars_held) -> float
_Reader = Callable[[Features, str, int, int | None], float]


def _op_key(op: Any, ctx: str) -> tuple | None:
    """Feature key for an operand evaluated in context symbol ``ctx`` (None for literals/pos)."""
    if isinstance(op, PriceOperand):
        return (_canon(op.symbol) if op.symbol else ctx, "price", op.price)
    if isinstance(op, IndOperand):
        sym = _canon(op.symbol) if op.symbol else ctx
        return (
            sym, "ind", op.ind, op.of, op.period, op.skip, op.fast, op.slow, op.signal, op.k,
            op.field,
        )  # fmt: skip
    if isinstance(op, SessionOperand):
        return (_canon(op.symbol) if op.symbol else ctx, "session", op.session, op.bars)
    if isinstance(op, CalendarOperand):
        return (ctx, "calendar", op.calendar)
    if isinstance(op, EventOperand):
        return (ctx, "event", op.event, op.field)
    if isinstance(op, UniverseOperand):
        return (ctx, "universe", op.universe)
    return None


def _compile_operand(op: Any) -> _Reader:
    if isinstance(op, PosOperand):

        def read_pos(f: Features, ctx: str, i: int, bh: int | None) -> float:
            return float(bh) if bh is not None else math.nan

        return read_pos
    if isinstance(
        op,
        (PriceOperand, IndOperand, SessionOperand, CalendarOperand, EventOperand, UniverseOperand),
    ):
        keys: dict[str, tuple] = {}  # context symbol -> feature key (built once, not per read)

        def read_feat(f: Features, ctx: str, i: int, bh: int | None) -> float:
            if i < 0:
                return math.nan
            key = keys.get(ctx)
            if key is None:
                key = keys[ctx] = _op_key(op, ctx)  # type: ignore[assignment]
            return float(f.arrays[key][i])

        return read_feat
    value = float(op)

    def read_const(f: Features, ctx: str, i: int, bh: int | None) -> float:
        return value

    return read_const


# A compiled condition: (features, ctx symbol, position, bars_held) -> True | False | None
_Cond = Callable[[Features, str, int, int | None], _Tri]


def _compile_condition(c: Condition) -> _Cond:
    if isinstance(c, Always):
        return lambda f, s, i, bh: True
    if isinstance(c, Not):
        inner = _compile_condition(c.not_)

        def _not(f: Features, s: str, i: int, bh: int | None) -> _Tri:
            v = inner(f, s, i, bh)
            return None if v is None else (not v)

        return _not
    if isinstance(c, (AllOf, AnyOf)):
        kids = [_compile_condition(k) for k in (c.all_ if isinstance(c, AllOf) else c.any_)]
        is_all = isinstance(c, AllOf)

        def _agg(f: Features, s: str, i: int, bh: int | None) -> _Tri:
            unknown = False
            for k in kids:
                v = k(f, s, i, bh)
                if v is None:
                    unknown = True
                elif is_all and not v:
                    return False
                elif not is_all and v:
                    return True
            if unknown:
                return None
            return is_all

        return _agg
    if isinstance(c, (CrossesAbove, CrossesBelow)):
        a, b = c.crosses_above if isinstance(c, CrossesAbove) else c.crosses_below
        ra, rb = _compile_operand(a), _compile_operand(b)
        above = isinstance(c, CrossesAbove)

        def _cross(f: Features, s: str, i: int, bh: int | None) -> _Tri:
            pbh = None if bh is None else bh - 1
            a0, b0 = ra(f, s, i - 1, pbh), rb(f, s, i - 1, pbh)
            a1, b1 = ra(f, s, i, bh), rb(f, s, i, bh)
            if i < 1 or math.isnan(a0 + b0 + a1 + b1):
                return None
            return (a0 <= b0 and a1 > b1) if above else (a0 >= b0 and a1 < b1)

        return _cross
    for cls, name in _CMP.items():
        if isinstance(c, cls):
            a, b = getattr(c, name)
            ra, rb = _compile_operand(a), _compile_operand(b)
            fn = _CMP_FN[name]

            def _cmp(f: Features, s: str, i: int, bh: int | None, ra=ra, rb=rb, fn=fn) -> _Tri:
                x, y = ra(f, s, i, bh), rb(f, s, i, bh)
                if math.isnan(x) or math.isnan(y):
                    return None
                return fn(x, y)

            return _cmp
    raise TypeError(f"unsupported condition {type(c).__name__}")


def _collect_operands(obj: Any) -> list[Any]:
    """All price/indicator operands inside a condition or operand (depth-first)."""
    if isinstance(
        obj,
        (PriceOperand, IndOperand, SessionOperand, CalendarOperand, EventOperand, UniverseOperand),
    ):
        return [obj]
    out: list[Any] = []
    if isinstance(obj, AllOf):
        for k in obj.all_:
            out += _collect_operands(k)
    elif isinstance(obj, AnyOf):
        for k in obj.any_:
            out += _collect_operands(k)
    elif isinstance(obj, Not):
        out += _collect_operands(obj.not_)
    else:
        for name in ("gt", "ge", "lt", "le", "eq", "crosses_above", "crosses_below"):
            pair = getattr(obj, name, None)
            if pair is not None:
                for o in pair:
                    out += _collect_operands(o)
    return out


def _true(v: _Tri) -> bool:
    return v is True


class CompiledStrategy:
    """A resolved strategy ready to ``prepare`` (vectorised) and ``evaluate`` (pure)."""

    def __init__(self, spec: StrategySpec, sleeve_id: str | None = None) -> None:
        self.spec: StrategySpec = spec.resolve()  # type: ignore[assignment]
        self.sleeve_id: str = sleeve_id or spec.id
        self.warmup_bars: int = spec.warmup_bars
        self.calendar: pd.DatetimeIndex = pd.DatetimeIndex([], tz="UTC")
        self._flags_cache: np.ndarray = np.zeros(0, dtype=bool)
        self.universe: list[str] = sorted(_canon(s) for s in self.spec.universe)
        self.required: list[str] = sorted({_canon(s) for s in self.spec.required_symbols()})
        self._stop: StopRequest | None = self.spec.stops[0] if self.spec.stops else None
        s = self.spec
        # (operands, context symbols) pairs whose features must be prepared
        self._operands: list[tuple[Any, str]] = []
        if isinstance(s, RotationSpec):
            self._elig = _compile_condition(s.eligibility)
            self._rank = _compile_operand(s.rank.by)
            self._fb_sym = _canon(s.fallback.symbol) if s.fallback else None
            self._fb_req = (
                _compile_condition(s.fallback.require)
                if s.fallback and s.fallback.require
                else None
            )
            for sym in self.universe:
                for o in _collect_operands(s.eligibility) + _collect_operands(s.rank.by):
                    self._operands.append((o, sym))
            if self._fb_req is not None and self._fb_sym:
                for o in _collect_operands(s.fallback.require):  # type: ignore[union-attr]
                    self._operands.append((o, self._fb_sym))
        else:
            assert isinstance(s, RulesSpec)
            self._entry = _compile_condition(s.entry)
            self._exit = _compile_condition(s.exit) if s.exit is not None else None
            self._max_pos: int | None = (
                int(s.position.max_positions) if s.position.max_positions is not None else None  # type: ignore[arg-type]
            )
            self._pos_rank = (
                _compile_operand(s.position.rank.by) if s.position.rank is not None else None
            )
            self._pos_sign = (
                -1.0 if s.position.rank is not None and s.position.rank.order == "desc" else 1.0
            )
            for sym in self.universe:
                for c in (s.entry, s.exit):
                    if c is not None:
                        for o in _collect_operands(c):
                            self._operands.append((o, sym))
                if s.position.rank is not None:
                    for o in _collect_operands(s.position.rank.by):
                        self._operands.append((o, sym))

    # ------------------------------------------------------------------ schedule
    def _flags(self, cal: pd.DatetimeIndex) -> np.ndarray:
        n = len(cal)
        freq = self.spec.schedule.freq
        flags = np.ones(n, dtype=bool)
        if freq in ("daily", "bar") or n == 0:
            return flags
        if freq == "monthly":
            key = cal.year.to_numpy() * 100 + cal.month.to_numpy()
        else:
            iso = cal.isocalendar()
            key = iso["year"].to_numpy().astype(np.int64) * 100 + iso["week"].to_numpy().astype(
                np.int64
            )
        flags[1:] = key[1:] != key[:-1]
        return flags

    def is_decision_bar(self, asof: pd.Timestamp, feats: Features | None = None) -> bool:
        """True iff ``asof`` is a scheduled decision bar.

        Uses ``feats`` when given (stateless); otherwise the last ``prepare`` call.
        """
        if feats is not None:
            cal, flags = feats.calendar, feats.decision_flags
        else:
            cal, flags = self.calendar, self._flags_cache
        i = self._pos(asof, strict=False, cal=cal)
        return i is not None and bool(flags[i])

    def _pos(
        self, asof: pd.Timestamp, strict: bool = True, cal: pd.DatetimeIndex | None = None
    ) -> int | None:
        i = (self.calendar if cal is None else cal).get_indexer([pd.Timestamp(asof)])[0]
        if i < 0:
            if strict:
                raise ValueError(f"{asof} is not a bar of the strategy calendar")
            return None
        return int(i)

    # ------------------------------------------------------------------ prepare
    def prepare(self, bars: Mapping[str, pd.DataFrame]) -> Features:
        """Compute causal feature arrays on the strategy calendar (union of universe bars)."""
        frames = {_canon(k): v for k, v in bars.items()}
        missing = sorted(set(self.required) - set(frames))
        if missing:
            raise ValueError(f"missing bars for required symbols: {missing}")
        cal: pd.DatetimeIndex | None = None
        for sym in self.universe:
            cal = frames[sym].index if cal is None else cal.union(frames[sym].index)
        assert cal is not None
        cal = pd.DatetimeIndex(cal).sort_values()
        self.calendar = cal

        raw: dict[tuple, pd.DataFrame | pd.Series] = {}  # de-duplicated indicator outputs
        arrays: dict[tuple, np.ndarray] = {}
        for op, ctx in self._operands:
            key = _op_key(op, ctx)
            assert key is not None
            if key in arrays:
                continue
            sym = key[0]
            df = frames[sym]
            if key[1] == "price":
                series = df[op.price]
            elif key[1] == "session":
                series = self._session(df, op)
            elif key[1] in ("calendar", "event"):
                series = self._clock(df, op, intraday=self.spec.timeframe == "15m")
            elif key[1] == "universe":
                series = self._member(sym, df, intraday=self.spec.timeframe == "15m")
            else:
                series = self._indicator(raw, sym, df, op)
            arrays[key] = series.reindex(cal).to_numpy(dtype="float64")
        flags = self._flags(cal)
        self._flags_cache = flags
        return Features(calendar=cal, arrays=arrays, decision_flags=flags)

    @staticmethod
    def _session(df: pd.DataFrame, op: SessionOperand) -> pd.Series:
        """Per-session value (causal) of a ``{session: ...}`` operand on one symbol's 15m bars."""
        idx = df.index
        n = len(idx)
        codes = pd.factorize(idx.tz_convert(_NY).normalize())[0]  # one code per New York day
        first = np.ones(n, dtype=bool)
        first[1:] = codes[1:] != codes[:-1]
        start = np.maximum.accumulate(np.where(first, np.arange(n), 0))
        bar_index = np.arange(n) - start
        if op.session == "bar_index":
            vals = bar_index.astype("float64")
        elif op.session == "open":
            vals = df["open"].to_numpy(dtype="float64")[start]
        elif op.session in ("prev_close", "gap"):
            close = df["close"].to_numpy(dtype="float64")
            last = np.zeros(codes.max() + 1 if n else 0, dtype=np.int64)
            last[codes] = np.arange(n)  # last row of each session (codes ascend with time)
            prev = np.full(n, np.nan)
            has_prev = codes > 0
            prev[has_prev] = close[last[codes[has_prev] - 1]]
            if op.session == "prev_close":
                vals = prev
            else:
                with np.errstate(divide="ignore", invalid="ignore"):
                    vals = df["open"].to_numpy(dtype="float64")[start] / prev - 1.0
        elif op.session == "vwap":
            h, lo, c, v = (
                df[k].to_numpy(dtype="float64") for k in ("high", "low", "close", "volume")
            )
            pv = pd.Series((h + lo + c) / 3.0 * v).groupby(codes).cumsum().to_numpy()
            cv = pd.Series(v).groupby(codes).cumsum().to_numpy()
            with np.errstate(divide="ignore", invalid="ignore"):
                vals = np.where(cv > 0, pv / cv, np.nan)
        else:
            nb = int(op.bars)  # type: ignore[arg-type]
            high = op.session == "range_high"
            src = df["high" if high else "low"].to_numpy(dtype="float64")
            part = pd.Series(np.where(bar_index < nb, src, np.nan))
            agg = part.groupby(codes).transform("max" if high else "min").to_numpy()
            vals = np.where(bar_index >= nb - 1, agg, np.nan)
        return pd.Series(vals, index=idx, dtype="float64")

    @staticmethod
    def _clock(df: pd.DataFrame, op: Any, intraday: bool) -> pd.Series:
        """``calendar`` / ``event`` operand per bar from the XNYS calendar (known in advance)."""
        idx = df.index
        if len(idx) == 0:
            return pd.Series([], index=idx, dtype="float64")
        days = (
            idx.tz_convert(_NY).tz_localize(None).normalize()
            if intraday
            else idx.tz_localize(None).normalize()
        )
        lo, hi = days.min(), days.max()
        if isinstance(op, CalendarOperand):
            span = xnys_sessions(
                "SPY", lo.replace(day=1).date(), (hi + pd.offsets.MonthEnd(0)).date()
            )
            sess = span.tz_localize(None)
            month = sess.year * 100 + sess.month
            ser = pd.Series(month, index=sess)
            pos = ser.groupby(ser.to_numpy()).cumcount().to_numpy()
            size = ser.groupby(ser.to_numpy()).transform("size").to_numpy()
            table = pos + 1 if op.calendar == "tday_of_month" else size - 1 - pos
            vals = pd.Series(table.astype("float64"), index=sess).reindex(days).to_numpy()
        else:
            fomc = pd.DatetimeIndex(pd.to_datetime(list(fomc_dates())))
            nxt_i = fomc.searchsorted(days, side="left")
            ok = nxt_i < len(fomc)
            end = fomc[-1].date()
            span = xnys_sessions("SPY", lo.date(), max(end, hi.date()))
            sess = span.tz_localize(None)
            d_pos = sess.searchsorted(days, side="left")
            f_pos = sess.searchsorted(fomc[np.minimum(nxt_i, len(fomc) - 1)], side="left")
            vals = np.where(ok, (f_pos - d_pos).astype("float64"), np.nan)
        return pd.Series(vals, index=idx, dtype="float64")

    @staticmethod
    def _member(sym: str, df: pd.DataFrame, intraday: bool) -> pd.Series:
        """``{universe: sp500_member}`` per bar: point-in-time membership (A32), 1.0 or 0.0.

        Uses the list in force at the previous session's close, so it needs no bar data of the
        current or any later bar (causal by construction).
        """
        idx = df.index
        if len(idx) == 0:
            return pd.Series([], index=idx, dtype="float64")
        days = idx.tz_convert(_NY).normalize() if intraday else idx
        uniq = sorted({ts.date() for ts in days})
        flag = dict(zip(uniq, sp500_member_flags(sym, uniq), strict=True))
        return pd.Series([flag[ts.date()] for ts in days], index=idx, dtype="float64")

    @staticmethod
    def _indicator(raw: dict, sym: str, df: pd.DataFrame, op: IndOperand) -> pd.Series:
        name = op.ind
        rkey = (sym, name, op.of, op.period, op.skip, op.fast, op.slow, op.signal, op.k)
        if rkey not in raw:
            fn = INDICATORS[name]
            x = df[op.of]
            if name == "atr":
                raw[rkey] = fn(df["high"], df["low"], df["close"], int(op.period))  # type: ignore[arg-type]
            elif name == "devatr":
                raw[rkey] = fn(df["high"], df["low"], df["close"], int(op.period))  # type: ignore[arg-type]
            elif name in ("noise_upper", "noise_lower"):
                raw[rkey] = fn(df["open"], df["close"], int(op.period))  # type: ignore[arg-type]
            elif name == "ret":
                raw[rkey] = fn(x, int(op.period), int(op.skip or 0))  # type: ignore[arg-type]
            elif name == "macd":
                raw[rkey] = fn(x, int(op.fast or 12), int(op.slow or 26), int(op.signal or 9))
            elif name == "bbands":
                raw[rkey] = fn(x, int(op.period or 20), float(op.k if op.k is not None else 2.0))
            else:
                raw[rkey] = fn(x, int(op.period))  # type: ignore[arg-type]
        out = raw[rkey]
        return out[op.field] if isinstance(out, pd.DataFrame) else out

    # ------------------------------------------------------------------ evaluate
    def evaluate(
        self,
        feats: Features,
        asof: pd.Timestamp,
        held: Mapping[str, HeldPosition],
        force_decision: bool = False,
    ) -> SignalSet:
        """Pure evaluation at calendar bar ``asof`` (reads feature row ``asof`` only)."""
        asof = pd.Timestamp(asof)
        i = int(feats.calendar.get_indexer([asof])[0])
        if i < 0:
            raise ValueError(f"{asof} is not a bar of the strategy calendar")
        decision = force_decision or bool(feats.decision_flags[i])
        if not decision:
            return SignalSet(sleeve_id=self.sleeve_id, asof=asof, decision=False, intents=[])
        held_n: dict[str, HeldPosition] = {}
        for k, v in held.items():
            held_n[_canon(k)] = v
        if isinstance(self.spec, RotationSpec):
            return self._rotation(feats, asof, i, held_n)
        return self._rules(feats, asof, i, held_n)

    def _intent(self, symbol: str, target: str, weight: float, reason: str) -> Intent:
        stop = self._stop if target == "long" else None
        return Intent(symbol=symbol, target=target, weight=weight, reason=reason, stop=stop)  # type: ignore[arg-type]

    def _rotation(
        self, f: Features, asof: pd.Timestamp, i: int, held: dict[str, HeldPosition]
    ) -> SignalSet:
        s = self.spec
        assert isinstance(s, RotationSpec)
        k = int(s.select.top)  # type: ignore[arg-type]
        elig = {sym: _true(self._elig(f, sym, i, None)) for sym in self.universe}
        scores: dict[str, float] = {}
        for sym in self.universe:
            if s.filter_mode == "pre_rank" and not elig[sym]:
                continue
            v = self._rank(f, sym, i, None)
            if not math.isnan(v):
                scores[sym] = v
        sign = -1.0 if s.rank.order == "desc" else 1.0
        ranked = sorted(scores, key=lambda sym: (sign * scores[sym], sym))
        if s.filter_mode == "post_rank":
            selected = [sym for sym in ranked[:k] if elig[sym]]
        else:
            selected = ranked[:k]
        intents = [
            self._intent(sym, "long", 1.0 / k, f"rank {n}/{k} score={scores[sym]:.6g}")
            for n, sym in enumerate(selected, 1)
        ]
        targets = set(selected)
        empty = k - len(selected)
        if empty > 0 and self._fb_sym is not None:
            ok = self._fb_req is None or _true(self._fb_req(f, self._fb_sym, i, None))
            if ok:
                intents.append(
                    self._intent(self._fb_sym, "long", empty / k, f"fallback {empty}/{k} slots")
                )
                targets.add(self._fb_sym)
        for sym in sorted(held):
            if sym not in targets:
                intents.append(self._intent(sym, "flat", 0.0, "not selected"))
        diag = {
            "scores": {sym: scores[sym] for sym in ranked},
            "eligible": [sym for sym in self.universe if elig[sym]],
        }
        return SignalSet(
            sleeve_id=self.sleeve_id, asof=asof, decision=True, intents=intents, diagnostics=diag
        )

    @staticmethod
    def _bars_held(cal: pd.DatetimeIndex, pos: HeldPosition, i: int) -> int:
        j = int(cal.searchsorted(pd.Timestamp(pos.entry_ts), side="left"))
        return max(i - j, 0)

    def _rules(
        self, f: Features, asof: pd.Timestamp, i: int, held: dict[str, HeldPosition]
    ) -> SignalSet:
        s = self.spec
        assert isinstance(s, RulesSpec)
        w = s.position.weight
        if w == "equal":
            weight = 1.0 / (self._max_pos if self._max_pos is not None else len(self.universe))
        else:
            weight = float(w)  # type: ignore[arg-type]
        intents: list[Intent] = []
        candidates: list[tuple[float, str]] = []  # (ascending sort key, symbol); capped rules
        kept = 0
        for sym in self.universe:
            if sym in held:
                bh = self._bars_held(f.calendar, held[sym], i)
                if self._exit is not None:
                    out = _true(self._exit(f, sym, i, bh))
                    why = "exit condition"
                else:
                    out = not _true(self._entry(f, sym, i, None))
                    why = "exit: entry no longer true"
                if out:
                    intents.append(self._intent(sym, "flat", 0.0, f"{why} (bars_held={bh})"))
                else:
                    kept += 1
                    intents.append(self._intent(sym, "long", weight, f"hold (bars_held={bh})"))
            elif _true(self._entry(f, sym, i, None)):
                if self._max_pos is None:
                    intents.append(self._intent(sym, "long", weight, "entry"))
                else:
                    score = self._pos_rank(f, sym, i, None)  # type: ignore[misc]
                    if not math.isnan(score):
                        candidates.append((self._pos_sign * score, sym))
        if self._max_pos is not None and candidates:
            candidates.sort()
            for _, sym in candidates[: max(self._max_pos - kept, 0)]:
                intents.append(self._intent(sym, "long", weight, "entry"))
        for sym in sorted(held):
            if sym not in self.universe:
                intents.append(self._intent(sym, "flat", 0.0, "not in universe"))
        return SignalSet(sleeve_id=self.sleeve_id, asof=asof, decision=True, intents=intents)


def compile_strategy(spec: StrategySpec, sleeve_id: str | None = None) -> CompiledStrategy:
    """Compile a strategy spec (parameters resolved) into a :class:`CompiledStrategy`."""
    return CompiledStrategy(spec, sleeve_id)
