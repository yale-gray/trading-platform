"""ADR A16: a single-strategy backtest gets single-strategy caps and a kill-switch toggle."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from tradebot.api.jobs import single_sleeve_config, strategy_max_weight
from tradebot.strategy.io import load_strategy

STRATEGIES = Path(__file__).resolve().parents[2] / "strategies"
D0, D1 = date(2019, 1, 2), date(2020, 1, 2)


def _risk(name: str, over: dict | None = None):  # type: ignore[no-untyped-def]
    spec = load_strategy(STRATEGIES / f"{name}.yaml")
    return single_sleeve_config(spec, D0, D1, over or {}).engine.risk


def test_rotation_with_fallback_may_hold_everything_in_one_symbol() -> None:
    assert _risk("core_momentum").max_asset_fraction == Decimal(1)


def test_rules_equal_weight_cap_is_one_over_universe() -> None:
    assert _risk("mean_reversion").max_asset_fraction == Decimal("0.5")


def test_no_crypto_cap_and_kill_switch_on_by_default() -> None:
    r = _risk("crypto_trend")
    assert r.max_crypto_fraction == Decimal(1)
    assert r.kill_switch_drawdown == Decimal("0.20")


def test_overrides_toggle_kill_switch_and_caps() -> None:
    r = _risk("core_momentum", {"kill_switch": False, "max_asset_fraction": 0.4})
    assert r.kill_switch_drawdown == Decimal(1)
    assert r.max_asset_fraction == Decimal("0.4")


def test_vol_scaled_override_and_max_weight_without_fallback() -> None:
    spec = load_strategy(STRATEGIES / "core_momentum.yaml")
    cfg = single_sleeve_config(spec, D0, D1, {"vol_scaled": False})
    assert cfg.engine.sleeves[0].vol_scaled is False
    no_fb = spec.model_copy(update={"fallback": None})
    assert strategy_max_weight(no_fb) == Decimal(1) / Decimal(3)


def test_equal_weight_with_max_positions_caps_at_one_over_max_positions() -> None:
    spec = load_strategy(STRATEGIES / "tA_st_reversal.yaml")
    mp = spec.resolve().position.max_positions
    assert mp is not None
    assert strategy_max_weight(spec) == Decimal(1) / Decimal(int(mp))
