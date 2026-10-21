"""Tests for tradebot.strategy.schema and io (SPEC-002 section 2-3, 5, T7b, AC 1).

Expected values are hand-derived (in comments) from the spec, not copied from the
implementation's output. Offline and deterministic: the three committed sleeve YAMLs plus
small in-test mutations of their parsed dicts.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tradebot.strategy.io import STRATEGIES_DIR, dump_strategy, load_strategy
from tradebot.strategy.schema import RotationSpec, RulesSpec, StrategySpec

pytestmark = pytest.mark.unit

SLEEVE_IDS = ("core_momentum", "mean_reversion", "crypto_trend")


def _doc(sid: str) -> dict[str, Any]:
    """Fresh parsed dict of a committed sleeve YAML (mutate freely)."""
    return yaml.safe_load((STRATEGIES_DIR / f"{sid}.yaml").read_text(encoding="utf-8"))


def _from_doc(doc: dict[str, Any]) -> StrategySpec:
    return load_strategy(yaml.safe_dump(doc, sort_keys=False))


def _sleeve(sid: str) -> StrategySpec:
    return load_strategy(STRATEGIES_DIR / f"{sid}.yaml")


# --------------------------------------------------------------------------- AC 1: sleeves load


@pytest.mark.parametrize(
    ("sid", "expected_type"),
    [("core_momentum", RotationSpec), ("mean_reversion", RulesSpec), ("crypto_trend", RulesSpec)],
)
def test_three_sleeve_yamls_load_as_the_right_kind(sid: str, expected_type: type) -> None:
    assert isinstance(_sleeve(sid), expected_type)


def test_core_momentum_fields_match_spec_section_3() -> None:
    spec = _sleeve("core_momentum")
    assert isinstance(spec, RotationSpec)
    assert spec.asset_class == "equity"
    assert spec.universe == [
        "SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "XLK",
        "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
    ]  # fmt: skip
    assert spec.fallback is not None and spec.fallback.symbol == "IEF"
    assert spec.schedule.freq == "monthly"
    # resolved: top_n = 3, mom_lookback = 126
    resolved = spec.resolve().model_dump(mode="json", by_alias=True)
    assert resolved["select"]["top"] == 3
    assert resolved["rank"]["by"]["period"] == 126


def test_crypto_trend_stop_and_constraint_match_spec_section_3() -> None:
    spec = _sleeve("crypto_trend")
    assert spec.asset_class == "crypto"
    assert spec.constraints == ["fast < slow"]
    assert [s.period for s in spec.stops] == ["$atr_len"]
    assert spec.stops[0].mult == "$stop_mult"


# --------------------------------------------------------------------------- AC 1: round trips


@pytest.mark.parametrize("sid", SLEEVE_IDS)
def test_sleeve_round_trips_through_json_and_yaml_unchanged(sid: str) -> None:
    original = _sleeve(sid)

    as_json = dump_strategy(original, "json")
    assert as_json.lstrip().startswith("{")  # JSON path taken by load_strategy
    from_json = load_strategy(as_json)
    assert from_json == original

    as_yaml = dump_strategy(from_json, "yaml")
    from_yaml = load_strategy(as_yaml)
    assert from_yaml == original

    # re-dumping is stable: the second JSON dump equals the first
    assert dump_strategy(from_yaml, "json") == as_json


@pytest.mark.parametrize("sid", SLEEVE_IDS)
def test_load_from_path_string_and_from_path_object_agree(sid: str) -> None:
    path = STRATEGIES_DIR / f"{sid}.yaml"
    assert load_strategy(str(path)) == load_strategy(path)


def test_dump_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="unknown format"):
        dump_strategy(_sleeve("mean_reversion"), "toml")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- resolve()


def test_resolve_substitutes_params_and_leaves_original_untouched() -> None:
    spec = _sleeve("crypto_trend")
    resolved = spec.resolve()

    # Resolved document has no $refs left.
    assert "$" not in dump_strategy(resolved, "json")
    # Hand: entry.all[1] is sma(fast) > sma(slow) with fast=20, slow=50.
    rd = resolved.model_dump(mode="json", by_alias=True)
    assert rd["entry"]["all"][1]["gt"][0]["period"] == 20
    assert rd["entry"]["all"][1]["gt"][1]["period"] == 50
    assert rd["stops"][0]["period"] == 14
    assert rd["stops"][0]["mult"] == 2.5

    # Original is unchanged: still symbolic, parameters unchanged.
    assert spec.parameters == {"fast": 20.0, "slow": 50.0, "atr_len": 14.0, "stop_mult": 2.5}
    assert "$fast" in dump_strategy(spec, "json")
    assert spec.stops[0].period == "$atr_len"


def test_resolve_is_idempotent_and_empty_overrides_match_no_overrides() -> None:
    spec = _sleeve("mean_reversion")
    once = spec.resolve()
    assert once != spec  # $refs were substituted, so the symbolic spec differs
    assert once.resolve() == once
    assert spec.resolve({}) == once


def test_resolve_override_replaces_values_and_original_is_untouched() -> None:
    spec = _sleeve("crypto_trend")
    resolved = spec.resolve({"fast": 30, "stop_mult": 3.0})

    assert resolved.parameters["fast"] == 30.0
    assert resolved.parameters["stop_mult"] == 3.0
    rd = resolved.model_dump(mode="json", by_alias=True)
    assert rd["entry"]["all"][1]["gt"][0]["period"] == 30  # fast ind now 30
    assert rd["stops"][0]["mult"] == 3.0

    # Original keeps its own values.
    assert spec.parameters["fast"] == 20.0
    assert spec.parameters["stop_mult"] == 2.5


def test_resolve_override_applies_to_mean_reversion_literal_slot() -> None:
    resolved = _sleeve("mean_reversion").resolve({"rsi_entry": 15})
    rd = resolved.model_dump(mode="json", by_alias=True)
    # entry.all[0] is rsi(2) < rsi_entry
    assert rd["entry"]["all"][0]["lt"][1] == 15


def test_resolve_unknown_override_name_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown parameter 'nope'"):
        _sleeve("crypto_trend").resolve({"nope": 1})


def test_resolve_override_violating_constraint_raises_validation_error() -> None:
    # fast=60 > slow=50 violates "fast < slow"
    with pytest.raises(ValidationError, match="constraint violated"):
        _sleeve("crypto_trend").resolve({"fast": 60})


def test_resolve_override_making_atr_period_not_14_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="atr_trailing period must be 14"):
        _sleeve("crypto_trend").resolve({"atr_len": 10})


# --------------------------------------------------------------------------- rejections


def test_mixed_asset_class_in_universe_is_rejected() -> None:
    doc = _doc("core_momentum")  # equity
    doc["universe"] = [*doc["universe"], "BTC/USD"]
    with pytest.raises(ValidationError, match="not of asset_class"):
        _from_doc(doc)


def test_crypto_strategy_with_equity_symbol_is_rejected() -> None:
    doc = _doc("crypto_trend")
    doc["universe"] = [*doc["universe"], "SPY"]
    with pytest.raises(ValidationError, match="not of asset_class"):
        _from_doc(doc)


def test_fallback_of_wrong_asset_class_is_rejected() -> None:
    doc = _doc("core_momentum")
    doc["fallback"] = {"symbol": "BTC/USD"}
    with pytest.raises(ValidationError, match="not of asset_class"):
        _from_doc(doc)


def test_cross_asset_operand_of_wrong_class_is_rejected() -> None:
    doc = _doc("mean_reversion")  # equity
    doc["entry"]["all"][0]["lt"][0]["symbol"] = "BTC/USD"
    with pytest.raises(ValidationError, match="not of asset_class"):
        _from_doc(doc)


def test_unknown_indicator_is_rejected() -> None:
    doc = _doc("mean_reversion")
    doc["entry"]["all"][0]["lt"][0]["ind"] = "wizardry"
    with pytest.raises(ValidationError, match="unknown indicator"):
        _from_doc(doc)


def test_undefined_param_reference_is_rejected() -> None:
    doc = _doc("mean_reversion")
    doc["entry"]["all"][0]["lt"][0]["period"] = "$rsi_period"  # not in parameters
    with pytest.raises(ValidationError, match="undefined parameter"):
        _from_doc(doc)


def test_pos_operand_in_rotation_spec_is_rejected() -> None:
    doc = _doc("core_momentum")
    doc["eligibility"] = {"gt": [{"pos": "bars_held"}, 0]}
    with pytest.raises(ValidationError, match="only valid in exit conditions of kind: rules"):
        _from_doc(doc)


def test_pos_operand_is_accepted_in_rules_spec() -> None:
    # Positive control for the rejection above: mean_reversion uses {pos: bars_held}.
    spec = _sleeve("mean_reversion")
    assert isinstance(spec, RulesSpec)


def test_atr_stop_period_literal_not_14_is_rejected() -> None:
    doc = _doc("crypto_trend")
    doc["stops"][0]["period"] = 10
    with pytest.raises(ValidationError, match="atr_trailing period must be 14"):
        _from_doc(doc)


def test_atr_stop_period_via_param_not_14_is_rejected() -> None:
    doc = _doc("crypto_trend")
    doc["parameters"]["atr_len"] = 10  # stops[0].period is "$atr_len"
    with pytest.raises(ValidationError, match="atr_trailing period must be 14"):
        _from_doc(doc)


def test_two_stops_are_rejected() -> None:
    doc = _doc("crypto_trend")
    doc["stops"] = [copy.deepcopy(doc["stops"][0]), copy.deepcopy(doc["stops"][0])]
    with pytest.raises(ValidationError, match="at most one stop"):
        _from_doc(doc)


def test_non_atr_trailing_stop_type_is_rejected() -> None:
    doc = _doc("crypto_trend")
    doc["stops"][0]["type"] = "trailing_pct"
    with pytest.raises(ValidationError):
        _from_doc(doc)


def test_constraint_violated_at_load_is_rejected() -> None:
    doc = _doc("crypto_trend")
    doc["parameters"]["fast"] = 60  # fast < slow (50) now false
    with pytest.raises(ValidationError, match="constraint violated"):
        _from_doc(doc)


def test_extra_top_level_key_is_rejected() -> None:
    doc = _doc("mean_reversion")
    doc["bogus_key"] = 1
    with pytest.raises(ValidationError):
        _from_doc(doc)


def test_extra_nested_key_is_rejected() -> None:
    doc = _doc("core_momentum")
    doc["select"]["bogus_key"] = 1
    with pytest.raises(ValidationError):
        _from_doc(doc)


# --------------------------------------------------------------------------- registry


def test_strategies_registry_has_exactly_the_three_sleeve_ids() -> None:
    from tradebot.strategy import schema

    registry = schema.STRATEGIES
    assert set(registry) == set(SLEEVE_IDS)
    assert len(registry) == 3
    for key, spec in registry.items():
        assert spec.id == key


# --------------------------------------------------------------------------- warmup_bars


@pytest.mark.parametrize(
    ("sid", "expected"),
    [
        # core_momentum: eligibility sma(200) -> lookback 200; rank ret(126, skip 0) -> 126.
        #   max(200, 126) + 1 = 201
        ("core_momentum", 201),
        # mean_reversion: entry rsi(2) -> 2, sma(200) -> 200; exit rsi(2) -> 2.
        #   max(2, 200, 2) + 1 = 201
        ("mean_reversion", 201),
        # crypto_trend: entry sma(50) -> 50, sma(20) -> 20.
        #   max(50, 20) + 1 = 51
        ("crypto_trend", 51),
    ],
)
def test_warmup_bars_match_hand_computed_lookbacks(sid: str, expected: int) -> None:
    assert _sleeve(sid).warmup_bars == expected


# --------------------------------------------------------------------------- wave 2 fixes


def test_import_schema_does_not_import_yfinance_or_ccxt() -> None:
    import subprocess
    import sys

    code = (
        "import sys, tradebot.strategy.schema;"
        "bad=[m for m in ('yfinance','ccxt') if m in sys.modules];"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def test_asset_class_detection_uses_data_layer_normalization() -> None:
    from tradebot.strategy.schema import asset_class_of

    assert asset_class_of("btc-usd") == "crypto"
    assert asset_class_of("spy") == "equity"
    with pytest.raises(ValueError, match="symbol"):
        asset_class_of("not a symbol!")


def test_unrecognised_universe_symbol_is_rejected() -> None:
    doc = _doc("core_momentum")
    doc["universe"] = [*doc["universe"], "DOGE/USD"]
    with pytest.raises(ValidationError, match="Unsupported crypto base"):
        _from_doc(doc)


def test_resolve_never_touches_id_name_description_or_symbols() -> None:
    doc = _doc("crypto_trend")
    doc["name"] = "uses $fast here"
    doc["description"] = "$slow and $atr_len"
    spec = _from_doc(doc)
    resolved = spec.resolve()
    assert resolved.id == "crypto_trend"
    assert resolved.name == "uses $fast here"
    assert resolved.description == "$slow and $atr_len"
    assert resolved.universe == spec.universe


def test_resolve_substitutes_top_and_rank_period() -> None:
    rd = _sleeve("core_momentum").resolve({"top_n": 5}).model_dump(mode="json", by_alias=True)
    assert rd["select"]["top"] == 5
    assert isinstance(rd["select"]["top"], int)


@pytest.mark.parametrize("bad", [0, -3])
def test_integer_period_below_one_is_rejected(bad: int) -> None:
    doc = _doc("crypto_trend")
    doc["entry"]["all"][0]["gt"][1]["period"] = bad
    with pytest.raises(ValidationError):
        _from_doc(doc)


def test_param_period_below_one_is_rejected() -> None:
    doc = _doc("crypto_trend")
    doc["parameters"]["fast"] = 0
    with pytest.raises(ValidationError, match=">= 1"):
        _from_doc(doc)


def test_select_top_below_one_is_rejected() -> None:
    doc = _doc("core_momentum")
    doc["select"]["top"] = 0
    with pytest.raises(ValidationError):
        _from_doc(doc)
    doc["select"]["top"] = "$top_n"
    doc["parameters"]["top_n"] = 0
    with pytest.raises(ValidationError, match=">= 1"):
        _from_doc(doc)


def test_rank_period_below_one_is_rejected() -> None:
    doc = _doc("core_momentum")
    doc["rank"]["by"]["period"] = 0
    with pytest.raises(ValidationError):
        _from_doc(doc)


@pytest.mark.parametrize("bad", [0, -1.5])
def test_stop_mult_must_be_positive(bad: float) -> None:
    doc = _doc("crypto_trend")
    doc["stops"][0]["mult"] = bad
    with pytest.raises(ValidationError):
        _from_doc(doc)
    doc["stops"][0]["mult"] = "$stop_mult"
    doc["parameters"]["stop_mult"] = bad
    with pytest.raises(ValidationError, match="> 0"):
        _from_doc(doc)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_parameter_is_rejected(bad: float) -> None:
    doc = _doc("crypto_trend")
    doc["parameters"]["stop_mult"] = bad
    with pytest.raises(ValidationError, match="finite"):
        _from_doc(doc)


def test_duplicate_universe_entries_are_rejected() -> None:
    doc = _doc("core_momentum")
    doc["universe"] = [*doc["universe"], "spy"]  # duplicate after normalization
    with pytest.raises(ValidationError, match="duplicate universe"):
        _from_doc(doc)
    doc = _doc("crypto_trend")
    doc["universe"] = ["BTC/USD", "btc-usd"]
    with pytest.raises(ValidationError, match="duplicate universe"):
        _from_doc(doc)


def test_non_integer_param_in_int_slot_is_rejected_at_load() -> None:
    doc = _doc("crypto_trend")
    doc["parameters"]["slow"] = 50.5
    with pytest.raises(ValidationError, match="must be an integer"):
        _from_doc(doc)


def test_non_integer_override_in_int_slot_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        _sleeve("crypto_trend").resolve({"slow": 50.5})


def test_non_integer_param_in_num_slot_is_accepted() -> None:
    assert _sleeve("crypto_trend").resolve({"stop_mult": 2.25}).parameters["stop_mult"] == 2.25


def test_pos_operand_rejected_in_rules_entry() -> None:
    doc = _doc("mean_reversion")
    doc["entry"] = {"ge": [{"pos": "bars_held"}, 1]}
    with pytest.raises(ValidationError, match="only valid in exit"):
        _from_doc(doc)


def test_pos_operand_rejected_in_rotation_rank_and_fallback_require() -> None:
    doc = _doc("core_momentum")
    doc["rank"]["by"] = {"pos": "bars_held"}
    with pytest.raises(ValidationError, match="only valid in exit"):
        _from_doc(doc)
    doc = _doc("core_momentum")
    doc["fallback"]["require"] = {"gt": [{"pos": "bars_held"}, 0]}
    with pytest.raises(ValidationError, match="only valid in exit"):
        _from_doc(doc)


def test_cross_symbol_operand_must_share_asset_class_v1() -> None:
    # PM decision (v1): cross-symbol operands share the strategy's asset class.
    doc = _doc("mean_reversion")  # equity
    doc["entry"]["all"][1]["gt"][0] = {"price": "close", "symbol": "ETH/USD"}
    with pytest.raises(ValidationError, match="not of asset_class"):
        _from_doc(doc)
    doc["entry"]["all"][1]["gt"][0] = {"price": "close", "symbol": "IWM"}
    assert "IWM" in _from_doc(doc).required_symbols()


# ----- io


def test_duplicate_yaml_keys_are_rejected() -> None:
    text = (STRATEGIES_DIR / "mean_reversion.yaml").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key 'name'"):
        load_strategy(text + "name: again\n")


def test_duplicate_nested_yaml_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate key"):
        load_strategy("a: {b: 1, b: 2}\n")


def test_duplicate_json_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate key"):
        load_strategy('{"id": "a", "id": "b"}')


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "42\n", ""])
def test_non_mapping_document_raises_value_error(text: str) -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        load_strategy(text)


# --------------------------------------------------------------------------- coverage gaps


@pytest.mark.parametrize(
    "expr", ["__import__('os').system('x') < 1", "fast < slow; 1", "1 + 1 < 3"]
)
def test_unparseable_constraint_raises_validation_error(expr: str) -> None:
    doc = _doc("crypto_trend")
    doc["constraints"] = [expr]
    with pytest.raises(ValidationError, match="unparseable constraint"):
        _from_doc(doc)


def test_constraint_with_undefined_name_raises_validation_error() -> None:
    doc = _doc("crypto_trend")
    doc["constraints"] = ["fast < nope"]
    with pytest.raises(ValidationError, match="undefined parameter 'nope'"):
        _from_doc(doc)


def test_required_symbols_includes_universe_fallback_and_cross_symbols() -> None:
    # core_momentum: 16 universe symbols + fallback IEF = 17, sorted.
    syms = _sleeve("core_momentum").required_symbols()
    assert len(syms) == 17 and "IEF" in syms and syms == sorted(syms)

    doc = _doc("mean_reversion")  # universe SPY, QQQ
    doc["entry"]["all"][1]["gt"][0] = {"price": "close", "symbol": "IWM"}
    assert _from_doc(doc).required_symbols() == ["IWM", "QQQ", "SPY"]

    assert _sleeve("crypto_trend").required_symbols() == ["BTC/USD", "ETH/USD", "SOL/USD"]


def test_invalid_filter_mode_is_rejected() -> None:
    doc = _doc("core_momentum")
    doc["filter_mode"] = "mid_rank"
    with pytest.raises(ValidationError):
        _from_doc(doc)


def test_invalid_schedule_is_rejected() -> None:
    doc = _doc("core_momentum")
    doc["schedule"] = {"freq": "hourly"}
    with pytest.raises(ValidationError):
        _from_doc(doc)


def test_rules_exit_with_not_and_always_parses() -> None:
    doc = _doc("mean_reversion")
    doc["exit"] = {"any": [{"not": {"gt": [{"price": "close"}, 1]}}, {"always": True}]}
    spec = _from_doc(doc)
    assert isinstance(spec, RulesSpec)
    dumped = spec.model_dump(mode="json", by_alias=True)
    assert dumped["exit"]["any"][0]["not"]["gt"][1] == 1
    assert dumped["exit"]["any"][1] == {"always": True}


def test_crosses_above_below_and_fallback_require_parse() -> None:
    doc = _doc("crypto_trend")
    doc["entry"] = {
        "all": [
            {
                "crosses_above": [
                    {"ind": "sma", "period": "$fast"},
                    {"ind": "sma", "period": "$slow"},
                ]
            },
            {"crosses_below": [{"price": "close"}, 100]},
        ]
    }
    spec = _from_doc(doc)
    assert [next(iter(c.model_dump(by_alias=True))) for c in spec.entry.all_] == [
        "crosses_above",
        "crosses_below",
    ]

    doc = _doc("core_momentum")
    doc["fallback"]["require"] = {"gt": [{"price": "close", "symbol": "IEF"}, 0]}
    rot = _from_doc(doc)
    assert isinstance(rot, RotationSpec)
    assert rot.fallback is not None and rot.fallback.require is not None
