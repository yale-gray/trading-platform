"""Declarative strategy schema (SPEC-002 section 2).

A strategy is one sleeve's rules, either ``kind: rotation`` (rank a universe and hold the top k)
or ``kind: rules`` (per-symbol entry/exit conditions). Any numeric field may be a literal or a
``$param`` reference; :meth:`resolve` substitutes parameters without mutating the original.

Day trading (ADR-0001 A21): ``timeframe: 15m`` strategies work on 15-minute bars, indicator
periods are then *bars*, ``schedule: {freq: bar}`` decides on every bar, and the
``{session: ...}`` operands read per-session values (bar index, opening range). A daily
strategy omits ``timeframe`` and serialises exactly as before.

``STRATEGIES`` (the three built-in sleeves, keyed by id) and ``load_strategy`` /
``dump_strategy`` are served lazily from :mod:`tradebot.strategy.io` to avoid an import cycle.
"""

from __future__ import annotations

import math
import operator
import re
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from tradebot.strategy.indicators import INDICATORS

_PARAM_RE = r"^\$[A-Za-z_][A-Za-z0-9_]*$"
Param = Annotated[str, StringConstraints(pattern=_PARAM_RE)]
Num = float | Param  # numeric literal or "$name"
Period = Annotated[int, Field(ge=1)] | Param  # integer >= 1 (periods, top) or "$name"
Skip = Annotated[int, Field(ge=0)] | Param  # integer >= 0 or "$name"
PosNum = Annotated[float, Field(gt=0)] | Param  # number > 0 (stop mult) or "$name"

_STRICT = ConfigDict(extra="forbid", populate_by_name=True)

# Indicators with a single output vs. multiple named output columns.
_SINGLE = {"sma", "ema", "ret", "rsi", "atr", "vol", "devatr"}
_FIELDS = {
    "macd": {"macd", "signal", "hist"},
    "bbands": {"mid", "upper", "lower", "pctb", "bandwidth"},
}


def asset_class_of(symbol: str) -> Literal["equity", "crypto"]:
    """Asset class of ``symbol`` via the data layer (normalizes first).

    Imported lazily so ``import tradebot.strategy.schema`` stays light. Raises ``ValueError``
    for an unrecognised symbol.
    """
    from tradebot.data.errors import UnknownSymbolError
    from tradebot.data.symbols import asset_class, normalize_symbol

    try:
        return "crypto" if asset_class(normalize_symbol(symbol)).value == "CRYPTO" else "equity"
    except UnknownSymbolError as e:
        raise ValueError(str(e)) from e


def _normalized(symbol: str) -> str:
    from tradebot.data.errors import UnknownSymbolError
    from tradebot.data.symbols import normalize_symbol

    try:
        return str(normalize_symbol(symbol))
    except UnknownSymbolError as e:
        raise ValueError(str(e)) from e


# --------------------------------------------------------------------------- operands


class PriceOperand(BaseModel):
    """``{price: close|open|high|low|volume}``, optionally for another ``symbol``."""

    model_config = _STRICT
    price: Literal["close", "open", "high", "low", "volume"]
    symbol: str | None = None


class PosOperand(BaseModel):
    """``{pos: bars_held}``: bars since the entry fill (rules kind only)."""

    model_config = _STRICT
    pos: Literal["bars_held"]


class SessionOperand(BaseModel):
    """``{session: ...}``: a value tied to the trading session of an intraday strategy.

    * ``bar_index``: 0-based index of the bar within its session (0 = the 09:30 bar);
    * ``open``: the session's first open;
    * ``range_high`` / ``range_low``: highest high / lowest low of the session's first ``bars``
      bars (the opening range). NaN until the bar that completes the range
      (``bar_index >= bars - 1``), then fixed for the rest of the session. Causal.

    Only meaningful for ``timeframe: 15m`` (sessions are New York trading days).
    """

    model_config = _STRICT
    session: Literal["bar_index", "open", "range_high", "range_low"]
    bars: Period | None = None
    symbol: str | None = None

    @model_validator(mode="after")
    def _check(self) -> SessionOperand:
        ranged = self.session.startswith("range_")
        if ranged and self.bars is None:
            raise ValueError(f"session {self.session!r} requires 'bars'")
        if not ranged and self.bars is not None:
            raise ValueError(f"session {self.session!r} does not take 'bars'")
        return self


class IndOperand(BaseModel):
    """``{ind: <name>, period: n, of: close, ...}``; name must be a key of ``INDICATORS``."""

    model_config = _STRICT
    ind: str
    period: Period | None = None
    of: Literal["close", "open", "high", "low", "volume"] = "close"
    skip: Skip | None = None
    fast: Period | None = None
    slow: Period | None = None
    signal: Period | None = None
    k: Num | None = None
    field: str | None = None
    symbol: str | None = None

    @model_validator(mode="after")
    def _check(self) -> IndOperand:
        if self.ind not in INDICATORS:
            raise ValueError(f"unknown indicator {self.ind!r}; known: {sorted(INDICATORS)}")
        given = {
            n
            for n in ("period", "skip", "fast", "slow", "signal", "k", "field")
            if getattr(self, n) is not None
        }
        allowed = {
            "sma": {"period"},
            "ema": {"period"},
            "rsi": {"period"},
            "atr": {"period"},
            "devatr": {"period"},
            "vol": {"period"},
            "ret": {"period", "skip"},
            "macd": {"fast", "slow", "signal", "field"},
            "bbands": {"period", "k", "field"},
        }.get(self.ind, set())
        if given - allowed:
            raise ValueError(f"indicator {self.ind!r} does not take {sorted(given - allowed)}")
        if self.ind in _SINGLE and self.period is None:
            raise ValueError(f"indicator {self.ind!r} requires 'period'")
        if self.ind in _FIELDS:
            if self.field not in _FIELDS[self.ind]:
                raise ValueError(f"{self.ind!r} requires field in {sorted(_FIELDS[self.ind])}")
        return self


Operand = float | Param | PriceOperand | PosOperand | IndOperand | SessionOperand


# --------------------------------------------------------------------------- conditions


class _Cmp(BaseModel):
    model_config = _STRICT


class Gt(_Cmp):
    gt: tuple[Operand, Operand]


class Ge(_Cmp):
    ge: tuple[Operand, Operand]


class Lt(_Cmp):
    lt: tuple[Operand, Operand]


class Le(_Cmp):
    le: tuple[Operand, Operand]


class Eq(_Cmp):
    eq: tuple[Operand, Operand]


class CrossesAbove(_Cmp):
    crosses_above: tuple[Operand, Operand]


class CrossesBelow(_Cmp):
    crosses_below: tuple[Operand, Operand]


class AllOf(_Cmp):
    all_: list[Condition] = Field(alias="all", min_length=1)


class AnyOf(_Cmp):
    any_: list[Condition] = Field(alias="any", min_length=1)


class Not(_Cmp):
    not_: Condition = Field(alias="not")


class Always(_Cmp):
    always: Literal[True]


Condition = Gt | Ge | Lt | Le | Eq | CrossesAbove | CrossesBelow | AllOf | AnyOf | Not | Always
for _m in (AllOf, AnyOf, Not):
    _m.model_rebuild()


# --------------------------------------------------------------------------- pieces


class Schedule(BaseModel):
    """When decisions are made (derived from the bar index only).

    ``bar`` = every bar (``timeframe: 15m`` only); ``daily`` = every daily bar.
    """

    model_config = _STRICT
    freq: Literal["daily", "weekly", "monthly", "bar"]


class StopRequest(BaseModel):
    """Stop declared by a strategy; execution owns the level, tracking and exit (SPEC-002 s5)."""

    model_config = _STRICT
    type: Literal["atr_trailing"]
    period: Period
    mult: PosNum
    anchor: Literal["highest_close_since_entry"]


class Rank(BaseModel):
    model_config = _STRICT
    by: Operand
    order: Literal["asc", "desc"] = "desc"


class Select(BaseModel):
    model_config = _STRICT
    top: Period
    weighting: Literal["equal"] = "equal"


class Fallback(BaseModel):
    model_config = _STRICT
    symbol: str
    require: Condition | None = None


class Position(BaseModel):
    """Per-symbol weight (fraction of sleeve capital) and an optional cap on open positions.

    ``max_positions`` (needs ``rank``): at most that many symbols are held at once; held
    symbols keep their slot until their exit fires, free slots go to the best-ranked entry
    candidates. ``weight: equal`` then means ``1 / max_positions`` instead of ``1 / universe``.
    """

    model_config = _STRICT
    weight: Literal["equal"] | float | Param = "equal"
    max_positions: Period | None = None
    rank: Rank | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_cap(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Uncapped positions serialise as before the cap existed (config hashes stay stable)."""
        data: dict[str, Any] = handler(self)
        for key in ("max_positions", "rank"):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def _cap_needs_rank(self) -> Position:
        if (self.max_positions is None) != (self.rank is None):
            raise ValueError("position 'max_positions' and 'rank' must be given together")
        return self


# --------------------------------------------------------------------------- helpers

_CONSTRAINT_RE = re.compile(
    r"^\s*(\$?[A-Za-z_]\w*|-?\d+(?:\.\d+)?)\s*(<=|>=|==|!=|<|>)\s*(\$?[A-Za-z_]\w*|-?\d+(?:\.\d+)?)\s*$"
)
_OPS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


def _walk(obj: Any):
    """Yield every dict and scalar inside nested dict/list structures."""
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)


_CMP_KEYS = ("gt", "ge", "lt", "le", "eq", "crosses_above", "crosses_below")
# Slot kinds: "period" = integer >= 1, "skip" = integer >= 0, "mult" = number > 0, "num" = any.
_INT_KINDS = ("period", "skip")
SlotFn = Callable[[str, str], Any]


def _slot(v: Any, kind: str, fn: SlotFn) -> Any:
    if isinstance(v, str) and re.fullmatch(_PARAM_RE, v):
        return fn(v, kind)
    return v


def _map_operand(o: Any, fn: SlotFn) -> Any:
    if isinstance(o, dict) and "ind" in o:
        out = dict(o)
        for key, kind in (
            ("period", "period"),
            ("fast", "period"),
            ("slow", "period"),
            ("signal", "period"),
            ("skip", "skip"),
            ("k", "num"),
        ):
            if key in out:
                out[key] = _slot(out[key], kind, fn)
        return out
    if isinstance(o, dict) and "session" in o:
        out = dict(o)
        if "bars" in out:
            out["bars"] = _slot(out["bars"], "period", fn)
        return out
    return _slot(o, "num", fn)


def _map_condition(c: Any, fn: SlotFn) -> Any:
    if not isinstance(c, dict):
        return c
    out: dict[str, Any] = {}
    for k, v in c.items():
        if k in _CMP_KEYS:
            out[k] = [_map_operand(x, fn) for x in v]
        elif k in ("all", "any"):
            out[k] = [_map_condition(x, fn) for x in v]
        elif k == "not":
            out[k] = _map_condition(v, fn)
        else:
            out[k] = v
    return out


def _map_params(data: dict[str, Any], fn: SlotFn) -> dict[str, Any]:
    """Apply ``fn(name_ref, kind)`` to every ``$param`` in a parameter-bearing slot only.

    Ids, names, descriptions, symbols and every other string are never visited.
    """
    out = dict(data)
    for key in ("eligibility", "entry", "exit"):
        if key in out:
            out[key] = _map_condition(out[key], fn)
    if "fallback" in out and "require" in out["fallback"]:
        out["fallback"] = {
            **out["fallback"],
            "require": _map_condition(out["fallback"]["require"], fn),
        }
    if "rank" in out:
        out["rank"] = {**out["rank"], "by": _map_operand(out["rank"]["by"], fn)}
    if "select" in out and "top" in out["select"]:
        out["select"] = {**out["select"], "top": _slot(out["select"]["top"], "period", fn)}
    if "stops" in out:
        out["stops"] = [
            {
                **st,
                "period": _slot(st.get("period"), "period", fn),
                "mult": _slot(st.get("mult"), "mult", fn),
            }
            for st in out["stops"]
        ]
    if "position" in out:
        pos = dict(out["position"])
        if "weight" in pos:
            pos["weight"] = _slot(pos["weight"], "num", fn)
        if "max_positions" in pos:
            pos["max_positions"] = _slot(pos["max_positions"], "period", fn)
        if "rank" in pos:
            pos["rank"] = {**pos["rank"], "by": _map_operand(pos["rank"]["by"], fn)}
        out["position"] = pos
    return out


def _check_slots(body: dict[str, Any], params: Mapping[str, float]) -> None:
    """Every ``$ref`` is defined and its value fits the slot it is used in (int / bounds)."""
    used: list[tuple[str, str]] = []

    def collect(ref: str, kind: str) -> str:
        used.append((ref[1:], kind))
        return ref

    _map_params(body, collect)
    missing = sorted({n for n, _ in used} - set(params))
    if missing:
        raise ValueError(f"undefined parameter(s): {missing}")
    for name, kind in used:
        v = params[name]
        if kind in _INT_KINDS and not float(v).is_integer():
            raise ValueError(f"parameter {name!r} must be an integer, got {v}")
        if kind == "period" and v < 1:
            raise ValueError(f"parameter {name!r} must be >= 1, got {v}")
        if kind == "skip" and v < 0:
            raise ValueError(f"parameter {name!r} must be >= 0, got {v}")
        if kind == "mult" and v <= 0:
            raise ValueError(f"parameter {name!r} must be > 0, got {v}")


def _constraint_value(tok: str, params: Mapping[str, float]) -> float:
    name = tok.lstrip("$")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", tok):
        return float(tok)
    if name not in params:
        raise ValueError(f"constraint references undefined parameter {name!r}")
    return params[name]


def _check_constraint(expr: str, params: Mapping[str, float]) -> bool:
    m = _CONSTRAINT_RE.match(expr)
    if not m:
        raise ValueError(f"unparseable constraint {expr!r}; use 'a < b' with names or numbers")
    a, op, b = m.groups()
    return _OPS[op](_constraint_value(a, params), _constraint_value(b, params))


def _has_pos(obj: Any) -> bool:
    return any(isinstance(d, dict) and "pos" in d for d in _walk(obj))


def _lookback(op: dict[str, Any]) -> int:
    """Bars of history needed before an indicator operand's first valid value (period-ish)."""
    name = op["ind"]
    if name == "macd":
        return int(op.get("slow", 26)) + int(op.get("signal", 9))
    if name == "bbands":
        return int(op.get("period", 20))
    if name == "devatr":
        return max(int(op["period"]), 14)
    return int(op["period"]) + (int(op.get("skip") or 0) if name == "ret" else 0)


# --------------------------------------------------------------------------- strategy


class _StrategyBase(BaseModel):
    """Common header shared by both kinds."""

    model_config = _STRICT
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1)
    name: str
    description: str = ""
    asset_class: Literal["equity", "crypto"]
    timeframe: Literal["1d", "15m"] = "1d"
    parameters: dict[str, float] = {}
    constraints: list[str] = []
    universe: list[str] = Field(min_length=1)
    schedule: Schedule
    stops: list[StopRequest] = []

    @model_serializer(mode="wrap")
    def _omit_daily_timeframe(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """A daily strategy serialises without ``timeframe`` (hashes/files predate the field)."""
        data: dict[str, Any] = handler(self)
        if self.timeframe == "1d":
            data.pop("timeframe", None)
        return data

    # -- subclass hooks -----------------------------------------------------
    def _body_dump(self) -> dict[str, Any]:
        """Dump of everything except header/parameters/constraints (where $refs live)."""
        d = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        for k in (
            "parameters",
            "constraints",
            "schema_version",
            "id",
            "name",
            "description",
            "timeframe",
        ):
            d.pop(k, None)
        return d

    @field_validator("parameters")
    @classmethod
    def _finite(cls, v: dict[str, float]) -> dict[str, float]:
        bad = sorted(k for k, x in v.items() if not math.isfinite(x))
        if bad:
            raise ValueError(f"parameter value(s) must be finite: {bad}")
        return v

    @model_validator(mode="after")
    def _validate_spec(self) -> _StrategyBase:
        params = self.parameters
        body = self._body_dump()

        _check_slots(body, params)

        # universe unique after normalization
        normalized = [_normalized(s) for s in self.universe]
        dupes = sorted({s for s in normalized if normalized.count(s) > 1})
        if dupes:
            raise ValueError(f"duplicate universe symbol(s): {dupes}")

        # One asset class across universe, fallback and cross-symbol operands.
        # PM decision (v1): cross-symbol operands must share the strategy's asset class.
        symbols = set(self.universe)
        symbols |= {d["symbol"] for d in _walk(body) if isinstance(d, dict) and "symbol" in d}
        wrong = sorted(s for s in symbols if asset_class_of(s) != self.asset_class)
        if wrong:
            raise ValueError(f"symbols {wrong} are not of asset_class {self.asset_class!r}")

        # timeframe / schedule (ADR-0001 A21)
        if self.timeframe == "15m":
            if self.asset_class != "equity":
                raise ValueError("timeframe '15m' is for equities only")
            if self.schedule.freq == "daily":
                raise ValueError("a 15m strategy schedules by 'bar', 'weekly' or 'monthly'")
        elif self.schedule.freq == "bar":
            raise ValueError("schedule 'bar' needs timeframe '15m'")
        if self.timeframe != "15m" and any(
            isinstance(d, dict) and "session" in d for d in _walk(body)
        ):
            raise ValueError("'session' operands need timeframe '15m'")

        # constraints
        for expr in self.constraints:
            if not _check_constraint(expr, params):
                raise ValueError(f"constraint violated: {expr!r} with parameters {dict(params)}")

        # stops (ADR-0001 D5): at most one, atr_trailing with period 14
        if len(self.stops) > 1:
            raise ValueError("at most one stop per strategy (ADR-0001 D5)")
        for stop in self.stops:
            period = stop.period
            if isinstance(period, str):
                period = params[period[1:]]
            if period != 14:
                raise ValueError(f"atr_trailing period must be 14 (ADR-0001 D5), got {period}")
        return self

    # -- public API ---------------------------------------------------------
    def resolve(self, overrides: Mapping[str, float] | None = None) -> _StrategyBase:
        """Return a new spec with ``$param`` references substituted (pure; self is unchanged).

        ``overrides`` replace parameter values (unknown names raise ``ValueError``); constraints
        and the stop-period rule are re-validated, so a violating override raises
        ``pydantic.ValidationError``.
        """
        params = dict(self.parameters)
        for k, v in (overrides or {}).items():
            if k not in params:
                raise ValueError(f"unknown parameter {k!r}; defined: {sorted(params)}")
            params[k] = float(v)

        _check_slots(self._body_dump(), params)

        def sub(ref: str, kind: str) -> Any:
            v = params[ref[1:]]
            return int(v) if kind in _INT_KINDS and float(v).is_integer() else v

        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        out = _map_params(data, sub)
        out["parameters"] = params
        return type(self).model_validate(out)

    def required_symbols(self) -> list[str]:
        """Sorted symbols needed: universe, fallback and cross-asset operand references."""
        body = self._body_dump()
        syms = set(self.universe)
        syms |= {d["symbol"] for d in _walk(body) if isinstance(d, dict) and "symbol" in d}
        return sorted(syms)

    @property
    def warmup_bars(self) -> int:
        """Max indicator lookback over all operands (parameters resolved) plus 1."""
        body = self.resolve()._body_dump()
        looks = [_lookback(d) for d in _walk(body) if isinstance(d, dict) and "ind" in d]
        return max(looks, default=0) + 1


class RotationSpec(_StrategyBase):
    """Cross-sectional ranking body: hold the top k eligible symbols."""

    kind: Literal["rotation"] = "rotation"
    eligibility: Condition = Always(always=True)
    filter_mode: Literal["pre_rank", "post_rank"] = "pre_rank"
    rank: Rank
    select: Select
    fallback: Fallback | None = None

    @model_validator(mode="after")
    def _no_pos(self) -> RotationSpec:
        if _has_pos(self._body_dump()):
            raise ValueError("'pos' operands are only valid in exit conditions of kind: rules")
        return self


class RulesSpec(_StrategyBase):
    """Per-symbol time-series body. ``exit`` omitted means hold-while (exit = not entry)."""

    kind: Literal["rules"] = "rules"
    entry: Condition
    exit: Condition | None = None
    position: Position = Position()

    @model_validator(mode="after")
    def _pos_only_in_exit(self) -> RulesSpec:
        if _has_pos(self.entry.model_dump(mode="json", by_alias=True)):
            raise ValueError("'pos' operands are only valid in exit conditions, not entry")
        return self


StrategySpec = RotationSpec | RulesSpec
_StrategyAdapter = TypeAdapter(Annotated[StrategySpec, Field(discriminator="kind")])


def _strategy_adapter() -> TypeAdapter[RotationSpec | RulesSpec]:
    return _StrategyAdapter


_BUILTIN_IDS = ("core_momentum", "mean_reversion", "crypto_trend")
_cache: dict[str, StrategySpec] | None = None


def __getattr__(name: str) -> Any:
    """Lazily provide STRATEGIES, load_strategy and dump_strategy (avoids a cycle with io)."""
    global _cache
    if name in ("load_strategy", "dump_strategy"):
        from tradebot.strategy import io

        return getattr(io, name)
    if name == "STRATEGIES":
        if _cache is None:
            from tradebot.strategy import io

            specs = [io.load_strategy(io.STRATEGIES_DIR / f"{i}.yaml") for i in _BUILTIN_IDS]
            _cache = {s.id: s for s in specs}
        return _cache
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
