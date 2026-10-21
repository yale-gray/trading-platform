"""Golden backtests G1-G4 on the synthetic fixtures (SPEC-005 section 5).

Exact: ledger/fill hashes, Decimal money and quantities, fill counts. Floats: 1e-9. The
sensitivity meta-tests prove a golden cannot pass by accident (costs and timing matter).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from golden import harness as h

from tradebot.backtest.driver import BarDriver, build_engine_config
from tradebot.data.types import AssetClass
from tradebot.portfolio.config import CostConfig
from tradebot.portfolio.engine import PortfolioEngine

pytestmark = [pytest.mark.golden, pytest.mark.slow, pytest.mark.timeout(120)]


@pytest.mark.parametrize("gid", h.IDS)
def test_golden_matches(gid: str, update_golden: Path | None) -> None:
    actual = h.golden_dict(gid, h.run_golden(gid))
    if update_golden is not None:
        h.write_golden(gid, actual, update_golden)
        return
    h.assert_matches_golden(gid, actual)


def test_harness_refuses_to_write_without_a_note(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        h.write_golden("G1", {}, None)
    with pytest.raises(PermissionError):
        h.write_golden("G1", {}, tmp_path / "missing.md")


def test_diff_reports_field_by_field() -> None:
    base = {"a": "1", "m": {"sharpe": 1.0}, "first_fills": [{"x": "1"}]}
    assert h.diff_golden(base, base) == []
    d = h.diff_golden(base, {"a": "2", "m": {"sharpe": 1.0 + 1e-8}, "first_fills": [{"x": "2"}]})
    assert any(line.startswith("a:") for line in d)
    assert any("m.sharpe" in line for line in d)
    assert any("first diverging fill" in line for line in d)
    assert h.diff_golden({"m": {"s": 1.0}}, {"m": {"s": 1.0 + 1e-10}}) == []


# ----------------------------------------------------------------- sensitivity meta-tests


def _final_equity(
    engine_cfg_update: dict | None = None, lookahead: bool = False
) -> tuple[Decimal, str]:
    """Final equity and fill-list hash of G4, optionally with other costs or a leaky driver."""
    bars = h.load_bars()
    eng = h.engine_for(None)
    if engine_cfg_update is not None:
        eng = eng.model_copy(update=engine_cfg_update)
    cfg = h.golden_config("G4", eng)
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
    driver = BarDriver(engine, cfg.sleeves, bars, start=cfg.start)
    if lookahead:
        # test-only: features see bar t+1 at row t, i.e. every signal is made one bar early
        future = {}
        for sym, df in bars.items():
            f = df.copy()
            f.iloc[:-1] = df.iloc[1:].to_numpy()
            future[sym] = f
        for slot in driver._slots:  # noqa: SLF001
            slot.feats = slot.compiled.prepare(future)
    for _ in driver.run(end=cfg.end):
        pass
    return engine.snapshot().equity, h.fills_sha(h.fills_of(engine))


def test_g4_slippage_plus_one_bp_changes_final_equity() -> None:
    base = h.run_golden("G4").final_equity
    costs = dict(h.engine_for(None).costs)
    costs[AssetClass.EQUITY] = CostConfig(
        asset_class=AssetClass.EQUITY, fee_bps=Decimal("0"), slip_bps=Decimal("6")
    )
    costs[AssetClass.CRYPTO] = CostConfig(
        asset_class=AssetClass.CRYPTO, fee_bps=Decimal("10"), slip_bps=Decimal("11")
    )
    eq, _ = _final_equity({"costs": costs})
    assert eq != base
    assert eq < base


def test_g4_zero_costs_increase_final_equity() -> None:
    base = h.run_golden("G4").final_equity
    zero = {
        ac: CostConfig(asset_class=ac, fee_bps=Decimal(0), slip_bps=Decimal(0)) for ac in AssetClass
    }
    eq, _ = _final_equity({"costs": zero})
    assert eq > base


def test_g4_signals_one_bar_early_change_the_ledger() -> None:
    base_sha = h.fills_sha(h.run_golden("G4").fills)
    _, leaky_sha = _final_equity(lookahead=True)
    assert leaky_sha != base_sha
