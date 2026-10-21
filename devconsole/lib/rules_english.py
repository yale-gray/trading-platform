"""Plain-English descriptions of strategy rules (SPEC-006 T3).

Read-only presentation helpers: they walk the pydantic models in ``tradebot.strategy.schema``
and describe them. Nothing here evaluates a signal. No streamlit import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradebot.strategy.schema import (
    AllOf,
    Always,
    AnyOf,
    Condition,
    CrossesAbove,
    CrossesBelow,
    Eq,
    Ge,
    Gt,
    IndOperand,
    Le,
    Lt,
    Not,
    Operand,
    PosOperand,
    PriceOperand,
    RotationSpec,
    RulesSpec,
    StopRequest,
    StrategySpec,
)

GLOSSARY: dict[str, str] = {
    "sma": "Simple moving average: the average closing price over the last N days. "
    "It smooths out day-to-day noise to show the trend.",
    "ema": "Exponential moving average: like an average price, but recent days count more.",
    "rsi": "RSI: a 0-100 gauge of recent up moves versus down moves. "
    "Low numbers mean the price has fallen a lot lately, high numbers mean it has risen a lot.",
    "atr": "ATR (average true range): the typical size of a day's price swing over N days.",
    "ret": "Return: how much the price changed (in percent) over the last N days.",
    "vol": "Volatility: how much daily returns jump around. Higher means a bumpier ride.",
    "macd": "MACD: compares a fast and a slow average price to show whether momentum is "
    "building or fading.",
    "bbands": "Bollinger bands: a band above and below the average price that widens when "
    "prices are jumpy.",
    "trailing stop": "A safety exit that sells if the price drops a set distance below its "
    "recent high since you bought.",
    "stop": "A safety exit that sells if the price drops a set distance below its recent high "
    "since you bought.",
    "universe": "The list of symbols this strategy is allowed to trade.",
    "warmup": "How much price history an indicator needs before it can give a number.",
    "rotation": "A strategy that ranks a group of symbols and holds the best few.",
    "rules": "A strategy that looks at each symbol on its own and buys or sells by fixed rules.",
    "crosses above": "Was at or below on the previous day and is above today.",
    "crosses below": "Was at or above on the previous day and is below today.",
    "bars_held": "How many trading days you have held the position since buying.",
    "bp": "Basis point: 1 bp is 0.01%.",
    "slippage": "Getting a slightly worse price than the quote when a trade fills.",
}

# Plain help for the built-in parameter names (fallback is derived from how the name is used).
_PARAM_HELP: dict[str, str] = {
    "mom_lookback": "how many days back the return is measured to rank symbols",
    "trend_len": "days in the long-term average price used as a trend filter",
    "top_n": "how many of the best-ranked symbols to hold",
    "rsi_len": "days used to calculate the RSI",
    "rsi_entry": "buy only when the RSI is below this level",
    "rsi_exit": "sell when the RSI rises above this level",
    "max_hold": "sell after holding this many days, whatever the price does",
    "atr_len": "days used to measure the typical daily price swing, called ATR",
    "stop_mult": "how many typical daily swings below the high the safety stop sits",
    "fast": "days in the faster average price",
    "slow": "days in the slower average price",
}

_PRICE_NAMES = {
    "close": "price",
    "open": "opening price",
    "high": "daily high",
    "low": "daily low",
    "volume": "trading volume",
}
_OF_NAMES = {
    "close": "closing price",
    "open": "opening price",
    "high": "daily high",
    "low": "daily low",
    "volume": "trading volume",
}
_CMP_WORDS: dict[str, str] = {
    "gt": "is above",
    "ge": "is at or above",
    "lt": "is below",
    "le": "is at or below",
    "eq": "equals",
    "crosses_above": "crosses above",
    "crosses_below": "crosses below",
}
_CMP_CLASSES: tuple[tuple[type, str], ...] = (
    (Gt, "gt"),
    (Ge, "ge"),
    (Lt, "lt"),
    (Le, "le"),
    (Eq, "eq"),
    (CrossesAbove, "crosses_above"),
    (CrossesBelow, "crosses_below"),
)
_FREQ_WORDS = {"daily": "every day", "weekly": "every week", "monthly": "once a month"}
_FREQ_CHECK = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}


@dataclass(frozen=True)
class RuleText:
    """Plain-English view of one strategy."""

    summary: str
    entry: list[str]
    exit: list[str]
    stops: list[str]
    parameters: dict[str, str]
    warmup: str
    signals_note: str = "Signals appear after Wave 3."


# --------------------------------------------------------------------------- explanation tracking


def _first_mention(key: str, seen: set[str] | None) -> bool:
    """True if the parenthetical explanation for ``key`` should be shown now.

    ``seen`` is an explicit set owned by the caller (one per ``describe_strategy`` call).
    With ``seen=None`` the explanation is always shown, so standalone calls are unchanged.
    """
    if seen is None:
        return True
    if key in seen:
        return False
    seen.add(key)
    return True


def _ind_key(op: IndOperand) -> str:
    """Key for 'explanation already shown'. MACD and Bollinger fields each explain themselves."""
    if op.ind in ("macd", "bbands"):
        return f"{op.ind}:{op.field}"
    return op.ind


# --------------------------------------------------------------------------- operands


def _num(x: float | int | str) -> str:
    if isinstance(x, str):
        return x
    f = float(x)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _days(n: Any) -> str:
    """'200-day' for a number, or 'parameter-name-day' fallback for an unresolved ref."""
    if isinstance(n, str):
        return f"({n.lstrip('$')} parameter)-day"
    return f"{_num(n)}-day"


def _owner(symbol: str | None) -> str:
    return f" for {symbol}" if symbol else ""


def describe_operand(op: Operand, seen: set[str] | None = None) -> str:
    """Describe one operand, e.g. ``{ind: sma, period: 200}`` as "200-day average price"."""
    if isinstance(op, bool):  # pragma: no cover - not a schema type
        return str(op)
    if isinstance(op, (int, float)):
        return _num(op)
    if isinstance(op, str):  # unresolved "$name" reference
        return f"the value of parameter {op.lstrip('$')}"
    if isinstance(op, PriceOperand):
        base = _PRICE_NAMES[op.price]
        return f"{op.symbol}'s {base}" if op.symbol else base
    if isinstance(op, PosOperand):
        return "the number of days the position has been held"
    if isinstance(op, IndOperand):
        text = _describe_indicator(op, seen)
        return f"{text} of {op.symbol}" if op.symbol else text
    return str(op)  # pragma: no cover - defensive


def _describe_indicator(op: IndOperand, seen: set[str] | None = None) -> str:
    of = _OF_NAMES[op.of]
    explain = _first_mention(_ind_key(op), seen)
    if op.ind == "sma":
        what = "average price" if op.of == "close" else f"average {of}"
        return f"{_days(op.period)} {what}"
    if op.ind == "ema":
        what = "weighted average price" if op.of == "close" else f"weighted average {of}"
        note = " (recent days count more)" if explain else ""
        return f"{_days(op.period)} {what}{note}"
    if op.ind == "ret":
        base = f"{_days(op.period)} return"
        if op.skip:
            skip = f"skipping the most recent {_num(op.skip)} days"
            lead = "change in price, " if explain else ""
            return f"{base} ({lead}{skip})"
        return f"{base} (change in price)" if explain else base
    if op.ind == "rsi":
        if explain:
            return f"{_days(op.period)} RSI (a 0-100 gauge of recent up vs down moves)"
        return f"{_days(op.period)} RSI"
    if op.ind == "atr":
        if explain:
            return f"{_days(op.period)} average daily price range (ATR)"
        return f"{_days(op.period)} ATR"
    if op.ind == "vol":
        note = " (how much daily returns vary)" if explain else ""
        return f"{_days(op.period)} volatility{note}"
    if op.ind == "macd":
        spans = f"{_num(op.fast)}/{_num(op.slow)}-day"
        if op.field == "macd":
            if explain:
                return f"MACD line ({spans}: fast average minus slow average)"
            return f"{spans} MACD line"
        if op.field == "signal":
            if explain:
                return f"MACD signal line ({_days(op.signal)} average of the MACD line)"
            return f"{_days(op.signal)} MACD signal line"
        if explain:
            return f"MACD histogram ({spans}, gap between the MACD line and its signal line)"
        return f"{spans} MACD histogram"
    if op.ind == "bbands":
        n = _days(op.period)
        k = "" if op.k is None else f", {_num(op.k)} standard deviations wide"
        names = {
            "mid": f"middle Bollinger band ({n} average price)",
            "upper": f"upper Bollinger band ({n}{k})",
            "lower": f"lower Bollinger band ({n}{k})",
            "pctb": (
                f"Bollinger %B ({n}: where price sits between the bands, 0 = lower, 1 = upper)"
                if explain
                else f"{n} Bollinger %B"
            ),
            "bandwidth": (
                f"Bollinger bandwidth ({n}: how wide the bands are)"
                if explain
                else f"{n} Bollinger bandwidth"
            ),
        }
        return names.get(str(op.field), f"Bollinger bands {n}")
    return f"{op.ind} indicator"  # pragma: no cover - schema validates names


# --------------------------------------------------------------------------- conditions


def _cmp_text(kind: str, left: Operand, right: Operand, seen: set[str] | None = None) -> str:
    lhs = describe_operand(left, seen)
    if isinstance(left, PriceOperand) and left.price == "close" and not left.symbol:
        lhs = "price"
    word = _CMP_WORDS[kind]
    if isinstance(right, IndOperand) and not right.symbol and isinstance(left, PriceOperand):
        if not left.symbol:
            return f"{lhs} {word} its {describe_operand(right, seen)}"
    subject = f"the {lhs}" if isinstance(left, IndOperand) else lhs
    rhs = describe_operand(right, seen)
    if isinstance(right, IndOperand):
        rhs = f"the {rhs}"
    return f"{subject} {word} {rhs}"


def describe_condition(cond: Condition, seen: set[str] | None = None) -> str:
    """Describe a condition tree as one sentence fragment."""
    for cls, kind in _CMP_CLASSES:
        if isinstance(cond, cls):
            left, right = getattr(cond, kind)
            return _cmp_text(kind, left, right, seen)
    if isinstance(cond, AllOf):
        return " and ".join(_wrap(c, seen) for c in cond.all_)
    if isinstance(cond, AnyOf):
        return " or ".join(_wrap(c, seen) for c in cond.any_)
    if isinstance(cond, Not):
        return f"it is not true that {describe_condition(cond.not_, seen)}"
    if isinstance(cond, Always):
        return "always (no extra requirement)"
    return str(cond)  # pragma: no cover - defensive


def _wrap(cond: Condition, seen: set[str] | None = None) -> str:
    text = describe_condition(cond, seen)
    return f"({text})" if isinstance(cond, (AllOf, AnyOf, Not)) else text


def _bullets(cond: Condition, seen: set[str] | None = None) -> list[str]:
    """Top-level bullet lines for a condition; nested groups become indented sub-bullets."""
    if isinstance(cond, AllOf):
        out = ["All of these must be true:"]
        for c in cond.all_:
            out += _indent(_bullets(c, seen))
        return out
    if isinstance(cond, AnyOf):
        out = ["At least one of these must be true:"]
        for c in cond.any_:
            out += _indent(_bullets(c, seen))
        return out
    if isinstance(cond, Not):
        return [f"This must NOT be true: {describe_condition(cond.not_, seen)}"]
    return [_cap(describe_condition(cond, seen))]


def _indent(lines: list[str]) -> list[str]:
    return [f"  {line}" if i else f"- {line}" for i, line in enumerate(lines)]


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------------------- stops


def _describe_stop(stop: StopRequest, seen: set[str] | None = None) -> str:
    if stop.type == "atr_trailing":
        days = _days(stop.period)
        what = f"{days} average daily range (ATR)" if _first_mention("atr", seen) else f"{days} ATR"
        return (
            f"Sells if price falls {_num(stop.mult)} x its {what} below the highest close since "
            "buying."
        )
    return f"Stop of type {stop.type}."  # pragma: no cover - schema has one type


# --------------------------------------------------------------------------- parameters


def _param_uses(spec: StrategySpec) -> dict[str, str]:
    """Derive a help line for each parameter from where the unresolved spec uses it."""
    uses: dict[str, str] = {}
    body = spec.model_dump(mode="json", by_alias=True, exclude_none=True)

    def visit(obj: Any) -> None:
        if isinstance(obj, dict):
            if "ind" in obj:
                for key in ("period", "fast", "slow", "signal", "skip", "k"):
                    v = obj.get(key)
                    if isinstance(v, str) and v.startswith("$"):
                        uses.setdefault(v[1:], f"{key} setting of the {obj['ind'].upper()}")
            for v in obj.values():
                visit(v)
        elif isinstance(obj, list):
            for v in obj:
                visit(v)

    visit(body)
    return uses


def _describe_parameters(spec: StrategySpec) -> dict[str, str]:
    derived = _param_uses(spec)
    out: dict[str, str] = {}
    for name, value in spec.parameters.items():
        help_ = _PARAM_HELP.get(name) or derived.get(name) or "a number the rules use"
        out[name] = f"{_num(value)} ({help_})"
    return out


# --------------------------------------------------------------------------- strategies


def _join_symbols(symbols: list[str]) -> str:
    if len(symbols) <= 1:
        return "".join(symbols)
    if len(symbols) == 2:
        return f"{symbols[0]} or {symbols[1]}"
    if len(symbols) <= 6:
        return ", ".join(symbols[:-1]) + f", or {symbols[-1]}"
    return f"{len(symbols)} symbols ({', '.join(symbols[:4])}, ...)"


def _warmup(spec: StrategySpec) -> str:
    try:
        n = spec.warmup_bars
    except Exception:  # noqa: BLE001 - presentation must not crash
        return "needs some price history before it can act"
    return f"needs {n} days of history before it can act"


def _describe_rotation(spec: RotationSpec, seen: set[str]) -> RuleText:
    r = spec
    top = int(r.select.top) if not isinstance(r.select.top, str) else r.select.top
    direction = "highest" if r.rank.order == "desc" else "lowest"
    names = _join_symbols(r.universe)
    freq = _FREQ_WORDS[r.schedule.freq]
    # Output order is summary, entry, exit, stops; each indicator is described in that order.
    by_summary = describe_operand(r.rank.by, seen)
    summary = (
        f"{_cap(freq)}, ranks {names} by their {by_summary} and holds the "
        f"{direction}-ranked {top}, equally weighted"
    )
    entry: list[str] = []
    elig_is_always = isinstance(r.eligibility, Always)
    if not elig_is_always:
        summary += (
            f", skipping any that fail the trend check ({describe_condition(r.eligibility, seen)})"
        )
    summary += "."
    by_entry = describe_operand(r.rank.by, seen)
    entry.append(
        f"Every {_FREQ_CHECK[r.schedule.freq].replace('daily', 'day')} check: rank "
        f"the symbols by {by_entry}, {direction} first."
    )
    if not elig_is_always:
        if r.filter_mode == "pre_rank":
            entry.append("Only symbols that pass this test are ranked:")
        else:
            entry.append("After ranking, skip any pick that fails this test:")
        entry += _indent(_bullets(r.eligibility, seen))
    entry.append(f"Hold the top {top}, giving each an equal share of the money.")
    exits = [
        f"At the next {_FREQ_CHECK[r.schedule.freq]} check, sell any holding that is no longer "
        f"in the top {top}"
        + ("." if elig_is_always else " or that no longer passes the test above.")
    ]
    if r.fallback is not None:
        entry.append(
            f"If fewer than {top} symbols qualify, put the empty slots into {r.fallback.symbol}"
            + (
                f" (only when {describe_condition(r.fallback.require, seen)})."
                if r.fallback.require is not None
                else "."
            )
        )
    else:
        entry.append("If fewer than that qualify, the empty slots stay in cash.")
    return RuleText(
        summary=summary,
        entry=entry,
        exit=exits,
        stops=[_describe_stop(s, seen) for s in r.stops],
        parameters=_describe_parameters(spec),
        warmup=_warmup(spec),
    )


def _describe_rules(spec: RulesSpec, seen: set[str]) -> RuleText:
    r = spec
    names = _join_symbols(r.universe)
    # Output order is summary, entry, exit, stops; each indicator is described in that order.
    summary = f"Buys {names} when {describe_condition(r.entry, seen)}"
    if r.exit is None:
        summary += ", and sells when that stops being true"
    else:
        summary += f"; sells when {describe_condition(r.exit, seen)}"
    if r.stops:
        summary += ", with a safety stop"
    summary += f". Checked {_FREQ_CHECK[r.schedule.freq]}."
    if isinstance(r.entry, (AllOf, AnyOf)):
        entry_lines = _bullets(r.entry, seen)
    else:
        entry_lines = [f"Buy when {describe_condition(r.entry, seen)}."]
    weight = r.position.weight
    entry = list(entry_lines)
    if weight == "equal":
        n = len(r.universe)
        noun = "symbol" if n == 1 else "symbols"
        entry.append(
            f"Each symbol gets a fixed slice of this strategy's money: 1/{n} for each of "
            f"{n} {noun}, whether or not the others are held."
        )
    else:
        entry.append(f"Each symbol held gets a weight of {_num(weight)}.")
    if r.exit is None:
        exit_lines = ["No separate sell rule: sells when the buy conditions above stop being true."]
    else:
        exit_lines = (
            _bullets(r.exit, seen)
            if isinstance(r.exit, (AllOf, AnyOf))
            else [f"Sell when {describe_condition(r.exit, seen)}."]
        )
    return RuleText(
        summary=summary,
        entry=entry,
        exit=exit_lines,
        stops=[_describe_stop(s, seen) for s in r.stops],
        parameters=_describe_parameters(spec),
        warmup=_warmup(spec),
    )


def describe_strategy(spec: StrategySpec) -> RuleText:
    """Describe a rotation or rules strategy in plain English (parameters resolved)."""
    original = spec
    resolved = spec.resolve()
    # Per-call set of indicators already explained; the explanation shows only on first mention.
    seen: set[str] = set()
    text = (
        _describe_rotation(resolved, seen)
        if isinstance(resolved, RotationSpec)
        else _describe_rules(resolved, seen)  # type: ignore[arg-type]
    )
    # Parameter table comes from the unresolved spec so names are still known.
    return RuleText(
        summary=text.summary,
        entry=text.entry,
        exit=text.exit,
        stops=text.stops,
        parameters=_describe_parameters(original),
        warmup=text.warmup,
    )


# --------------------------------------------------------------------------- charting helper


def indicators_used(spec: StrategySpec) -> list[tuple[str, dict[str, int | float]]]:
    """Indicators the strategy uses, as ``(name, kwargs)`` pairs, deduped, in first-use order.

    ``name`` is the key in ``tradebot.strategy.indicators.INDICATORS``. ``kwargs`` are the
    keyword arguments that function takes, with the names it defines: the window is ``n``
    (the schema's ``period``), and ``skip``, ``fast``, ``slow``, ``signal`` and ``k`` keep
    their names. Window and count values are ints; ``k`` (band width) is a float. Values are
    resolved from parameters, so the result can be passed straight to ``compute_overlays``.
    """
    resolved = spec.resolve()
    body = resolved.model_dump(mode="json", by_alias=True, exclude_none=True)
    found: list[tuple[str, dict[str, int | float]]] = []

    def add(name: str, params: dict[str, int | float]) -> None:
        item = (name, params)
        if item not in found:
            found.append(item)

    def visit(obj: Any) -> None:
        if isinstance(obj, dict):
            if "ind" in obj:
                params: dict[str, int | float] = {}
                for schema_key, kwarg in (
                    ("period", "n"),
                    ("skip", "skip"),
                    ("fast", "fast"),
                    ("slow", "slow"),
                    ("signal", "signal"),
                    ("k", "k"),
                ):
                    value = obj.get(schema_key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        params[kwarg] = float(value) if kwarg == "k" else int(value)
                add(str(obj["ind"]), params)
            for key, v in obj.items():
                if key != "stops":
                    visit(v)
        elif isinstance(obj, list):
            for v in obj:
                visit(v)

    visit(body)
    for stop in body.get("stops", []):
        add("atr", {"n": int(stop["period"])})
    return found
