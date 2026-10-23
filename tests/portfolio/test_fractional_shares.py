"""Fractional equity shares (owner decision 2026-10-22; SPEC-003 section 2).

``EngineConfig.equity_qty_quantum`` defaults to 1e-6; ``1`` is the old whole-share mode.
Buy sizing rounds DOWN to the quantum, sells are exact and on the grid, crypto stays 1e-8.
Every expected value is worked out by hand in a comment.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import EngineConfig, SleeveConfig, default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.state import EngineState
from tradebot.runner.bootstrap import smart_definition
from tradebot.runner.state import PortfolioDef
from tradebot.sim.broker import Bar
from tradebot.sim.orders import Side

pytestmark = pytest.mark.unit

D = Decimal
D1, D2, D3, D4 = date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)
CORE = "core"


def _cfg(capital: str, quantum: str | None = None) -> EngineConfig:
    """One equity sleeve at weight 1, default costs (equity fee 0, slip 5 bps), default risk."""
    base = default_config()
    kw = {} if quantum is None else {"equity_qty_quantum": D(quantum)}
    return EngineConfig(
        initial_capital=D(capital),
        sleeves=(
            SleeveConfig(
                sleeve_id=CORE,
                name="core",
                target_weight=D(1),
                vol_scaled=False,
                allowed_asset_classes=frozenset({AssetClass.EQUITY}),
            ),
        ),
        risk=base.risk,
        costs=base.costs,
        **kw,
    )


def _bar(sym: str, px: float) -> Bar:
    return Bar(symbol=sym, open=px, high=px, low=px, close=px)


def test_default_quantum_is_1e6_and_validated() -> None:
    assert default_config().equity_qty_quantum == D("0.000001")
    assert _cfg("1000.00", "1").equity_qty_quantum == D(1)
    for bad in ("0", "-1", "2", "0.5", "0.0000015", "NaN"):
        with pytest.raises(ValidationError):
            _cfg("1000.00", bad)
    with pytest.raises((TypeError, ValidationError)):  # floats are never accepted
        EngineConfig.model_validate({**default_config().model_dump(), "equity_qty_quantum": 0.1})


def test_spy_764_with_2500_target_buys_3_272251_rounded_down() -> None:
    # 2500 / 764.00 = 3.27225130890... -> DOWN at 1e-6 = 3.272251 (not ...252, not 3).
    eng = PortfolioEngine(_cfg("10000.00"))
    eng.process_bar(D1, {"SPY": _bar("SPY", 764.00)})
    res = eng.submit_target_weights(CORE, {"SPY": 0.25}, D1)
    assert res.orders[0].qty_requested == D("3.272251")
    r2 = eng.process_bar(D2, {"SPY": _bar("SPY", 764.00)})
    # Fill price ceil8(764 x 1.0005) = 764.38200000;
    # notional q2(3.272251 x 764.382) = 2501.2477... -> 2501.25
    assert r2.fills[0].qty_filled == D("3.272251")
    assert r2.fills[0].fill_price == D("764.38200000")
    assert r2.snapshot.cash == D("10000.00") - D("2501.25")
    eng.verify()


def test_whole_share_mode_behaves_as_before() -> None:
    # Same 2500 / 764 = 3.27 -> 3 whole shares when the quantum is 1.
    eng = PortfolioEngine(_cfg("10000.00", "1"))
    eng.process_bar(D1, {"SPY": _bar("SPY", 764.00)})
    res = eng.submit_target_weights(CORE, {"SPY": 0.25}, D1)
    assert res.orders[0].qty_requested == D("3")


def test_sell_of_exact_fractional_position_closes_it_to_zero() -> None:
    eng = PortfolioEngine(_cfg("10000.00"))
    eng.process_bar(D1, {"SPY": _bar("SPY", 764.00)})
    eng.submit_target_weights(CORE, {"SPY": 0.25}, D1)
    eng.process_bar(D2, {"SPY": _bar("SPY", 764.00)})
    out = eng.submit_target_weights(CORE, {}, D2)
    (order,) = out.orders
    assert order.side is Side.SELL and order.qty_requested == D("3.272251")
    r3 = eng.process_bar(D3, {"SPY": _bar("SPY", 764.00)})
    assert r3.fills[0].qty_filled == D("3.272251")
    assert not any(p.symbol == "SPY" for s in r3.snapshot.sleeves for p in s.positions)
    # Sell fill floor8(764 x 0.9995) = 763.61800000;
    # q2(3.272251 x 763.618) = 2498.7489... -> 2498.75
    assert r3.snapshot.cash == D("10000.00") - D("2501.25") + D("2498.75")
    eng.verify()


def test_4000_split_over_15_picks_buys_every_pick_with_cents_left() -> None:
    prices = [150, 175, 200, 225, 250, 275, 300, 350, 400, 450, 500, 600, 700, 800, 900]
    syms = sorted(
        ["AAPL", "ABBV", "ADBE", "AMZN", "COST", "CRM", "GOOG", "HD", "JPM", "LLY", "META", "NVDA"]
        + ["TSLA", "UNH", "V"]
    )  # alphabetical = price order
    eng = PortfolioEngine(_cfg("4000.00"))
    eng.process_bar(D1, {s: _bar(s, p) for s, p in zip(syms, prices, strict=True)})
    res = eng.submit_target_weights(CORE, {s: 1 / 15 for s in syms}, D1)
    by_sym = {o.symbol: o for o in res.orders}
    assert set(by_sym) == set(syms)  # every pick is bought: none dust-rejected
    # AAPL (first, $150): weight 1/15 is quantized to 0.066667 (1e-6, HALF_EVEN);
    # 0.066667 x 4000 = 266.668; / 150 = 1.7777866... -> 1.777786 (DOWN at 1e-6).
    assert by_sym["AAPL"].qty_requested == D("1.777786")
    r2 = eng.process_bar(D2, {s: _bar(s, p) for s, p in zip(syms, prices, strict=True)})
    assert len(r2.fills) == 15
    assert all(f.qty_filled > 0 for f in r2.fills)
    fees = sum((f.fee for f in r2.fills), D(0))
    assert fees == D(0)  # equity fee is 0 bps
    # Slippage (5 bps) makes the 15 reservations sum to 4002 > 4000, so the last pick is
    # shrunk to what cash allows; at most 1 cent of leftover per pick remains.
    assert r2.snapshot.cash < D("0.01") * 15 + fees
    assert r2.snapshot.cash >= 0
    eng.verify()


def test_old_engine_state_without_the_field_restores_and_continues() -> None:
    # Build a whole-share account the old way (quantum 1 == the old behaviour), checkpoint it,
    # then strip the new field from the JSON exactly as a pre-change state looks.
    old = PortfolioEngine(_cfg("10000.00", "1"))
    old.process_bar(D1, {"SPY": _bar("SPY", 764.00)})
    old.submit_target_weights(CORE, {"SPY": 0.25}, D1)
    old.process_bar(D2, {"SPY": _bar("SPY", 764.00)})
    doc = json.loads(old.export_state().to_json())
    del doc["config"]["equity_qty_quantum"]
    state = EngineState.from_json(json.dumps(doc))
    assert state.config.equity_qty_quantum == D("0.000001")  # loads with the default
    held = {p.symbol: p.qty for s in state.positions.values() for p in s}
    assert held == {"SPY": D(3)}  # 2500 / 764 = 3.27 -> 3 whole shares, still valid
    eng = PortfolioEngine(_cfg("10000.00"), state=state)  # default-quantum config matches
    eng.verify()
    # ...and it continues in fractional mode: 0.25 x equity / 764 is a fractional target.
    eng.process_bar(D3, {"SPY": _bar("SPY", 764.00)})
    res = eng.submit_target_weights(CORE, {"SPY": 0.25}, D3)
    (order,) = res.orders
    assert order.side is Side.BUY
    # sizing equity ~ 10000 - 2.30 slippage... compare structurally: buy qty is fractional.
    assert order.qty_requested != order.qty_requested.to_integral_value()
    eng.process_bar(D4, {"SPY": _bar("SPY", 764.00)})
    eng.verify()


def test_portfolio_def_json_without_the_field_loads_with_default() -> None:
    defn = smart_definition(D1)
    doc = json.loads(defn.to_json())
    assert doc["engine"]["equity_qty_quantum"] == "0.000001"
    del doc["engine"]["equity_qty_quantum"]  # a pre-change portfolio.config_json
    back = PortfolioDef.from_json(json.dumps(doc))
    assert back.engine.equity_qty_quantum == D("0.000001")
    assert back.engine_config().equity_qty_quantum == D("0.000001")
