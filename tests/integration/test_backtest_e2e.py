"""Backtest integration on the synthetic fixtures (SPEC-002 AC 6, 8, 9; SPEC-005 T17).

Everything runs through the real data path, strategy YAML, ``BarDriver``, ``PortfolioEngine``
and benchmark. Some checks recompute numbers from the raw bars without the portfolio engine.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from golden import harness as h

from tradebot.backtest.engine import config_hash, run_backtest, run_with_benchmark

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.timeout(120)]


def test_report_json_is_byte_identical_across_runs() -> None:
    cfg = h.golden_config("G1")
    bars = h.load_bars()
    a = run_with_benchmark(cfg, bars).to_json()
    b = run_with_benchmark(cfg, bars).to_json()
    assert a == b
    assert a == h.run_golden("G1").report.to_json()


def test_config_hash_changes_with_any_resolved_parameter() -> None:
    cfg = h.golden_config("G1")
    spec = cfg.sleeves[0].strategy
    assert spec is not None
    tweaked = cfg.model_copy(
        update={
            "sleeves": [
                cfg.sleeves[0].model_copy(update={"strategy": spec.resolve({"rsi_entry": 11})})
            ]
        }
    )
    assert config_hash(tweaked) != config_hash(cfg)
    assert config_hash(cfg) == h.run_golden("G1").report.result.config_hash


@pytest.mark.parametrize("gid", h.IDS)
def test_no_fill_before_its_signal_bar(gid: str) -> None:
    run = h.run_golden(gid)
    assert run.fills
    for f in run.fills:
        assert f["signal_ts"] < f["fill_ts"], f
    first_fill = min(f["fill_ts"] for f in run.fills)
    assert first_fill > h.START.isoformat()  # warm-up is covered, signal on bar one fills on two


def test_g1_fills_match_raw_bars_independently() -> None:
    """Fill price = open of the fill bar x (1 +/- 5 bps); amount = qty x price; no engine code."""
    run = h.run_golden("G1")
    bars = h.load_bars()
    slip = Decimal("0.0005")
    for f in run.fills[:12] + run.fills[-12:]:
        day = pd.Timestamp(f["fill_ts"], tz="UTC")
        open_ = Decimal(repr(float(bars[f["symbol"]].loc[day, "open"])))
        factor = 1 + slip if f["side"] == "BUY" else 1 - slip
        assert abs(Decimal(f["price"]) - open_ * factor) < Decimal("0.00001"), f
        sig = pd.Timestamp(f["signal_ts"], tz="UTC")
        assert sig in bars[f["symbol"]].index and sig < day
        assert Decimal(f["fee"]) == Decimal("0.00")  # equity fee_bps = 0


def test_spy_benchmark_return_recomputed_from_bars() -> None:
    """Buy-and-hold: fill at the open of bar two with 5 bps slippage, hold to the last close."""
    rep = h.run_golden("G1").report
    spy = h.load_bars()["SPY"]
    window = spy.loc[
        (spy.index >= pd.Timestamp(h.START, tz="UTC"))
        & (spy.index <= pd.Timestamp(h.END, tz="UTC"))
    ]
    entry = window["open"].iloc[1] * 1.0005
    expected = window["close"].iloc[-1] / entry - 1.0
    assert rep.benchmark_metrics["SPY"].total_return == pytest.approx(
        expected, abs=3e-3
    )  # whole shares leave < 1 share of cash


def test_benchmark_and_fund_share_start_and_index() -> None:
    rep = h.run_golden("G1").report
    spy_eq = rep.benchmark_equity["SPY"]
    assert spy_eq.index.equals(rep.result.equity.index)
    assert rep.result.equity.index[0].date() == h.START
    # invested from the second bar (signal on bar one fills at bar two's open), not idle
    assert spy_eq.iloc[1] != spy_eq.iloc[0] or spy_eq.iloc[2] != spy_eq.iloc[1]
    assert float(spy_eq.iloc[0]) == 100000.0


def test_final_equity_is_cash_plus_marked_positions() -> None:
    run = h.run_golden("G2")
    bars = h.load_bars()
    last = {s: Decimal(repr(float(bars[s]["close"].iloc[-1]))) for s in run.final_qty}
    mv = sum((q * last[s] for s, q in run.final_qty.items()), Decimal(0))
    assert abs(run.final_cash + mv - run.final_equity) < Decimal("1.00")


def test_run_before_warmup_is_refused() -> None:
    """SPEC-002 section 6: fewer than ``warmup_bars`` bars before ``start`` raises."""
    cfg = h.golden_config("G2").model_copy(update={"start": date(2018, 3, 1)})
    with pytest.raises(ValueError, match="warmup_bars"):
        run_backtest(cfg, h.load_bars())
    just_enough = h.golden_config("G2").model_copy(
        update={"start": date(2019, 1, 2) - timedelta(0)}
    )
    assert run_backtest(just_enough.model_copy(update={"end": date(2019, 3, 1)}), h.load_bars())
