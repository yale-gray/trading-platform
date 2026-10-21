"""Unit tests for devconsole/lib/rules_english.py: plain-English strategy text (SPEC-006 AC 8)."""

from __future__ import annotations

import inspect
import re

import pytest

from devconsole.lib import rules_english as rules
from devconsole.lib.rules_english import (
    describe_condition,
    describe_operand,
    describe_strategy,
    indicators_used,
)
from devconsole.lib.sources import FULL_END, FULL_START, DataSource, load_bars
from tradebot.strategy.indicators import INDICATORS
from tradebot.strategy.io import load_strategy
from tradebot.strategy.schema import STRATEGIES, IndOperand, RulesSpec

pytestmark = pytest.mark.unit

_BUILTIN_IDS = ("core_momentum", "mean_reversion", "crypto_trend")
_YAML_KEYS = (
    r"\b(?:gt|ge|lt|le|eq|ind|period|price|pos|all|any|not|always|kind|symbol|field|anchor)"
    r"\s*:"
)


def _all_text(text: rules.RuleText) -> str:
    parts = [text.summary, *text.entry, *text.exit, *text.stops, *text.parameters.values()]
    return "\n".join(parts)


def _assert_plain_english(text: rules.RuleText) -> None:
    body = _all_text(text)
    assert "$" not in body
    assert not re.search(r"[{}\[\]]", body), body
    assert not re.search(_YAML_KEYS, body), body
    # Raw parameter names such as rsi_len or stop_mult must never leak into prose.
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", body), body


def _rules_yaml(entry: str, exit_: str | None = None, extra: str = "") -> str:
    """A small rules strategy around one hand-written entry (and optional exit) condition."""
    text = (
        "schema_version: 1\n"
        "id: probe\n"
        "name: Probe\n"
        "kind: rules\n"
        "asset_class: equity\n"
        "parameters: {n: 5, stop_mult: 2.5, atr_len: 14}\n"
        "universe: [SPY]\n"
        "schedule: {freq: daily}\n"
        f"entry: {entry}\n"
    )
    if exit_ is not None:
        text += f"exit: {exit_}\n"
    return text + extra


# --------------------------------------------------------------------------- built-ins


@pytest.mark.parametrize("strategy_id", _BUILTIN_IDS)
def test_builtin_renders_with_no_dollar_or_yaml_punctuation(strategy_id: str) -> None:
    text = describe_strategy(STRATEGIES[strategy_id])

    _assert_plain_english(text)
    assert text.summary
    assert text.entry
    assert text.exit
    assert text.parameters
    assert text.warmup.startswith("needs ")
    assert text.signals_note == "Signals appear after Wave 3."


def test_builtin_stops_render_for_strategies_that_declare_them() -> None:
    assert describe_strategy(STRATEGIES["core_momentum"]).stops == []
    for strategy_id in ("mean_reversion", "crypto_trend"):
        stops = describe_strategy(STRATEGIES[strategy_id]).stops
        assert len(stops) == 1
        assert "2.5 x its 14-day average daily range (ATR)" in stops[0]


def test_mean_reversion_has_expected_plain_phrases() -> None:
    text = describe_strategy(STRATEGIES["mean_reversion"])
    summary = text.summary

    assert "2-day RSI" in summary
    assert "below 10" in summary
    assert "200-day" in summary
    assert "SPY" in summary
    assert "QQQ" in summary
    assert "above 70" in summary  # rsi_exit


def test_rsi_gauge_explanation_appears_exactly_once_for_mean_reversion() -> None:
    """Explanation shows on first mention only: RSI is used in entry and exit."""
    text = describe_strategy(STRATEGIES["mean_reversion"])
    assert _all_text(text).count("0-100 gauge") == 1


def test_standalone_operand_always_shows_explanation() -> None:
    """With no shared 'seen' set (standalone call) the explanation is always shown."""
    op = IndOperand(ind="rsi", period=2)
    assert "0-100 gauge" in describe_operand(op)
    assert "0-100 gauge" in describe_operand(op)  # no seen set: shown every time


def test_mean_reversion_parameters_use_plain_help_lines() -> None:
    params = describe_strategy(STRATEGIES["mean_reversion"]).parameters

    assert params["rsi_len"].startswith("2 (")
    assert params["rsi_entry"].startswith("10 (")
    assert params["max_hold"].startswith("5 (")
    assert set(params) == {
        "rsi_len",
        "rsi_entry",
        "rsi_exit",
        "trend_len",
        "max_hold",
        "atr_len",
        "stop_mult",
    }


def test_warmup_is_longest_lookback_plus_one() -> None:
    # core_momentum: sma 200 is the longest lookback -> 201 days.
    assert describe_strategy(STRATEGIES["core_momentum"]).warmup == (
        "needs 201 days of history before it can act"
    )
    # crypto_trend: sma 50 is the longest lookback -> 51 days.
    assert describe_strategy(STRATEGIES["crypto_trend"]).warmup == (
        "needs 51 days of history before it can act"
    )


# --------------------------------------------------------------------------- node types


_CONDITIONS = {
    "gt": ("gt: [{price: close}, {ind: sma, period: 200}]", "price is above its 200-day"),
    "ge": ("ge: [{price: close}, 100]", "price is at or above 100"),
    "lt": ("lt: [{ind: rsi, period: 2}, 30]", "is below 30"),
    "le": ("le: [{price: volume}, 1000]", "trading volume is at or below 1000"),
    "eq": ("eq: [{ind: rsi, period: 2}, 50]", "equals 50"),
    "crosses_above": (
        "crosses_above: [{ind: sma, period: 20}, {ind: sma, period: 50}]",
        "crosses above",
    ),
    "crosses_below": (
        "crosses_below: [{ind: sma, period: 20}, {ind: sma, period: 50}]",
        "crosses below",
    ),
    "all": (
        "all: [{gt: [{price: close}, 10]}, {lt: [{price: close}, 20]}]",
        " and ",
    ),
    "any": (
        "any: [{gt: [{price: close}, 10]}, {lt: [{price: close}, 20]}]",
        " or ",
    ),
    "not": ("not: {lt: [{price: close}, 100]}", "it is not true that"),
    "always": ("always: true", "always (no extra requirement)"),
}


@pytest.mark.parametrize(("node", "phrase"), list(_CONDITIONS.values()), ids=list(_CONDITIONS))
def test_every_condition_node_type_renders(node: str, phrase: str) -> None:
    """Build a real spec from YAML, then check the entry condition's sentence."""
    spec = load_strategy(_rules_yaml(f"{{{node}}}"))
    assert isinstance(spec, RulesSpec)

    sentence = describe_condition(spec.entry)
    text = describe_strategy(spec)

    assert sentence
    assert phrase in sentence
    assert text.summary.startswith("Buys SPY when ")
    assert text.entry
    _assert_plain_english(text)


def test_pos_operand_renders_in_exit() -> None:
    spec = load_strategy(
        _rules_yaml(
            "{lt: [{price: close}, 100]}",
            exit_="{ge: [{pos: bars_held}, 5]}",
        )
    )
    text = describe_strategy(spec)
    assert "the number of days the position has been held is at or above 5" in " ".join(text.exit)
    _assert_plain_english(text)


def test_rules_exit_omitted_says_so() -> None:
    spec = load_strategy(_rules_yaml("{lt: [{price: close}, 100]}"))
    text = describe_strategy(spec)
    assert text.exit == [
        "No separate sell rule: sells when the buy conditions above stop being true."
    ]
    assert "sells when that stops being true" in text.summary


def test_float_position_weight_is_described() -> None:
    spec = load_strategy(
        _rules_yaml(
            "{lt: [{price: close}, 100]}",
            extra="position: {weight: 0.5}\n",
        )
    )
    assert "Each symbol held gets a weight of 0.5." in describe_strategy(spec).entry


def test_ret_with_skip_and_macd_bbands_vol_ema_render() -> None:
    spec = load_strategy(
        _rules_yaml(
            "{all: ["
            "{gt: [{ind: ret, period: 126, skip: 21}, 0]}, "
            "{gt: [{ind: macd, fast: 12, slow: 26, signal: 9, field: macd}, 0]}, "
            "{lt: [{ind: bbands, period: 20, k: 2, field: pctb}, 0.2]}, "
            "{gt: [{ind: vol, period: 20}, 0.01]}, "
            "{gt: [{ind: ema, period: 10}, {ind: ema, period: 30}]}"
            "]}"
        )
    )
    text = describe_strategy(spec)
    body = _all_text(text)

    assert "skipping the most recent 21 days" in body
    assert "MACD line" in body
    assert "Bollinger %B" in body
    assert "volatility" in body
    assert "weighted average price" in body
    _assert_plain_english(text)


def test_atr_stop_and_pct_b_bandwidth_fields_render() -> None:
    spec = load_strategy(
        _rules_yaml(
            "{gt: [{ind: bbands, period: 20, k: 2, field: bandwidth}, 0.1]}",
            extra=(
                "stops:\n"
                "  - {type: atr_trailing, period: 14, mult: 3, "
                "anchor: highest_close_since_entry}\n"
            ),
        )
    )
    text = describe_strategy(spec)
    assert "Bollinger bandwidth" in _all_text(text)
    assert text.stops == [
        "Sells if price falls 3 x its 14-day average daily range (ATR) below the highest close "
        "since buying."
    ]


def test_rotation_renders_summary_entry_exit_and_fallback() -> None:
    spec = load_strategy(
        "schema_version: 1\n"
        "id: rot\n"
        "name: Rotation probe\n"
        "kind: rotation\n"
        "asset_class: equity\n"
        "parameters: {lb: 60, top: 2}\n"
        "universe: [SPY, QQQ, IWM]\n"
        "schedule: {freq: weekly}\n"
        "eligibility: {gt: [{price: close}, {ind: sma, period: 100}]}\n"
        "filter_mode: post_rank\n"
        "rank: {by: {ind: ret, period: $lb, skip: 5}, order: asc}\n"
        "select: {top: $top, weighting: equal}\n"
        "fallback: {symbol: IEF, require: {ge: [{ind: vol, period: 20}, 0.01]}}\n"
    )
    text = describe_strategy(spec)

    assert text.summary.startswith("Every week, ranks SPY, QQQ, or IWM by their")
    assert "lowest-ranked 2" in text.summary
    assert "skipping the most recent 5 days" in text.summary
    assert any("into IEF" in line for line in text.entry)
    assert any("After ranking, skip any pick that fails this test" in line for line in text.entry)
    assert text.exit and "next weekly check" in text.exit[0]
    assert text.parameters["lb"].startswith("60 (")
    _assert_plain_english(text)


# --------------------------------------------------------------------------- indicators_used


def test_indicators_used_for_mean_reversion_is_exact_and_deduped() -> None:
    # Hand order: entry rsi(rsi_len=2), entry sma(trend_len=200), then the stop atr(atr_len=14).
    # rsi appears again in exit but is listed once.
    assert indicators_used(STRATEGIES["mean_reversion"]) == [
        ("rsi", {"n": 2}),
        ("sma", {"n": 200}),
        ("atr", {"n": 14}),
    ]


def test_indicators_used_for_crypto_and_rotation() -> None:
    assert indicators_used(STRATEGIES["crypto_trend"]) == [
        ("sma", {"n": 50}),
        ("sma", {"n": 20}),
        ("atr", {"n": 14}),
    ]
    assert indicators_used(STRATEGIES["core_momentum"]) == [
        ("sma", {"n": 200}),
        ("ret", {"n": 126}),
    ]


def test_indicators_used_kwargs_are_the_indicator_function_parameter_names() -> None:
    for strategy_id in _BUILTIN_IDS:
        for name, kwargs in indicators_used(STRATEGIES[strategy_id]):
            params = set(inspect.signature(INDICATORS[name]).parameters)
            assert set(kwargs) <= params, (strategy_id, name, kwargs)


def test_indicators_used_maps_schema_keys_to_function_kwargs() -> None:
    spec = load_strategy(
        _rules_yaml(
            "{all: ["
            "{gt: [{ind: ret, period: 126, skip: 21}, 0]}, "
            "{gt: [{ind: macd, fast: 12, slow: 26, signal: 9, field: macd}, 0]}, "
            "{lt: [{ind: bbands, period: 20, k: 2, field: pctb}, 0.2]}"
            "]}"
        )
    )
    found = dict(indicators_used(spec))
    assert found["ret"] == {"n": 126, "skip": 21}
    assert found["macd"] == {"fast": 12, "slow": 26, "signal": 9}
    assert found["bbands"] == {"n": 20, "k": 2.0}
    assert isinstance(found["bbands"]["k"], float)
    assert isinstance(found["bbands"]["n"], int)


def test_indicators_used_resolves_parameter_references() -> None:
    spec = load_strategy(_rules_yaml("{lt: [{ind: rsi, period: $n}, 30]}"))
    assert indicators_used(spec) == [("rsi", {"n": 5})]


@pytest.mark.parametrize("strategy_id", _BUILTIN_IDS)
def test_indicators_used_feeds_compute_overlays_on_fixture_bars(strategy_id: str) -> None:
    from devconsole.lib.charts import compute_overlays

    symbol = STRATEGIES[strategy_id].universe[0]
    bars = load_bars(DataSource.SYNTHETIC, symbol, FULL_START, FULL_END).bars
    picks = indicators_used(STRATEGIES[strategy_id])

    overlays = compute_overlays(bars, picks)

    assert overlays
    for name, _kwargs in picks:
        assert any(label.startswith(name.upper()) for label in overlays), name
