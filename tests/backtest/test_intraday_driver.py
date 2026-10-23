"""15-minute bars through ``BarDriver`` and the engine (ADR-0001 A21).

Flatten before the close, no BUY into the close, early-close days, lean (fast) marking giving
the same ledger as full marking, and restoring an intraday engine from its saved state.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest
from support.intraday_synth import (
    assert_bar_contract,
    frame_from_closes,
    make_intraday_bars,
    multi_session_index,
)

from tradebot.backtest.driver import BarDriver, TargetBook, build_engine_config
from tradebot.backtest.engine import BacktestConfig, SleeveSpec, run_backtest, run_with_benchmark
from tradebot.data.types import AssetClass
from tradebot.portfolio.config import CostConfig, EngineConfig, SleeveConfig, default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType, FillSide, Stage
from tradebot.portfolio.state import EngineState
from tradebot.risk.config import RiskConfig
from tradebot.sim.orders import OrderOrigin, Side
from tradebot.strategy.schema import RulesSpec

D = Decimal
S = "day"


def _engine_cfg(*, flatten: bool = True, t1: bool = False) -> EngineConfig:
    base = default_config()
    return EngineConfig(
        initial_capital=D("10000.00"),
        sleeves=(
            SleeveConfig(
                sleeve_id=S,
                name="day",
                target_weight=D(1),
                vol_scaled=False,
                allowed_asset_classes=frozenset({AssetClass.EQUITY}),
            ),
        ),
        risk=RiskConfig(
            max_asset_fraction=D(1), max_crypto_fraction=D(1), kill_switch_drawdown=D(1)
        ),
        costs={
            AssetClass.EQUITY: CostConfig(
                asset_class=AssetClass.EQUITY, fee_bps=D(0), slip_bps=D(0)
            ),
            AssetClass.CRYPTO: base.costs[AssetClass.CRYPTO],
        },
        intraday_flatten=flatten,
        settlement="t1" if t1 else "none",
    )


def hold_spec(universe: list[str], **position: object) -> RulesSpec:
    """15m rules strategy that wants every universe symbol, every bar."""
    return RulesSpec.model_validate(
        {
            "id": "stocks_hold15",
            "name": "hold",
            "asset_class": "equity",
            "timeframe": "15m",
            "universe": universe,
            "schedule": {"freq": "bar"},
            "entry": {"always": True},
            "position": {"weight": 1.0 / len(universe), **position},
        }
    )


def _cfg(spec: RulesSpec, start: date, end: date, **kw: bool) -> BacktestConfig:
    return BacktestConfig(
        sleeves=[SleeveSpec(sleeve_id=S, strategy=spec)],
        start=start,
        end=end,
        engine=_engine_cfg(**kw),
        decision_lag_bars=0,  # these tests pin exact fills; lag behaviour has its own tests
    )


def _run_driver(cfg: BacktestConfig, bars: dict[str, pd.DataFrame]):  # noqa: ANN202
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
    driver = BarDriver(
        engine, cfg.sleeves, bars, start=cfg.start, decision_lag_bars=cfg.decision_lag_bars
    )
    steps = list(driver.run(end=cfg.end))
    return engine, driver, steps


def _two_sessions(last_close: float = 103.0) -> dict[str, pd.DataFrame]:
    """AAA flat at 100 over Fri Jan 5 (warm-up), Mon Jan 8 and Tue Jan 9 2024.

    The last bar of Monday closes at ``last_close`` (the flatten price); a unique price makes the
    exit identifiable.
    """
    idx = multi_session_index(date(2024, 1, 5), date(2024, 1, 9))
    closes = [100.0] * len(idx)
    n_mon_last = (
        int(
            (
                idx.tz_convert("America/New_York").normalize()
                <= pd.Timestamp("2024-01-08", tz="America/New_York")
            ).sum()
        )
        - 1
    )
    closes[n_mon_last] = last_close
    return {"AAA": frame_from_closes(idx, closes)}


def test_synthetic_fixtures_meet_the_bar_contract() -> None:
    bars = make_intraday_bars(["AAA", "BBB"], date(2024, 1, 3), date(2024, 1, 12), seed=1)
    assert_bar_contract(bars)
    again = make_intraday_bars(["AAA", "BBB"], date(2024, 1, 3), date(2024, 1, 12), seed=1)
    pd.testing.assert_frame_equal(bars["AAA"], again["AAA"])  # seeded: reproducible
    other = make_intraday_bars(["AAA"], date(2024, 1, 3), date(2024, 1, 12), seed=2)
    assert not bars["AAA"].equals(other["AAA"])
    assert len(bars["AAA"]) == 8 * 26  # 8 sessions between Jan 3 and Jan 12 2024


def test_every_session_ends_flat_and_the_exit_fills_at_the_last_bars_close() -> None:
    bars = _two_sessions(last_close=103.0)
    cfg = _cfg(hold_spec(["AAA"]), date(2024, 1, 8), date(2024, 1, 9))
    engine, driver, steps = _run_driver(cfg, bars)
    assert len(steps) == 52  # two full sessions
    assert all(isinstance(s.asof, datetime) for s in steps)  # datetime asof
    fills = [e for e in engine.ledger if e.type is EventType.FILL]
    assert [f.side for f in fills] == [FillSide.BUY, FillSide.SELL, FillSide.BUY, FillSide.SELL]
    mon_buy, mon_sell, tue_buy, tue_sell = fills
    # Monday: decision at bar 0 (09:30), BUY fills at bar 1's open: 10000 / 100 = 100 shares.
    assert mon_buy.asof.isoformat() == "2024-01-08T14:45:00+00:00" and mon_buy.qty == D(100)
    # Flatten: the whole position is sold at the last bar's CLOSE (103), booked at that bar's open
    # timestamp (20:45 UTC = 15:45 New York), stage CLOSE: proceeds 100 x 103 = 10300.
    assert mon_sell.asof.isoformat() == "2024-01-08T20:45:00+00:00"
    assert (mon_sell.price, mon_sell.qty, mon_sell.stage) == (
        D("103.00000000"),
        D(100),
        Stage.CLOSE,
    )
    assert mon_sell.amount == D("10300.00") and mon_sell.realized_pnl == D("300.00")
    # Tuesday restarts from cash 10300 (decision bar 0, fill bar 1: 10300 / 100 = 103 shares) ...
    assert tue_buy.asof.isoformat() == "2024-01-09T14:45:00+00:00" and tue_buy.qty == D(103)
    # ... and is flat again at the end of the run, at the 100.0 close.
    assert tue_sell.asof.isoformat() == "2024-01-09T20:45:00+00:00" and tue_sell.amount == D(
        "10300.00"
    )
    assert engine.snapshot().sleeves[0].positions == []
    assert not any(o.status.value == "PENDING" for o in engine.orders())
    engine.verify()


def test_no_buy_is_submitted_on_the_last_bar_and_nothing_is_held_overnight() -> None:
    bars = _two_sessions()
    cfg = _cfg(hold_spec(["AAA"]), date(2024, 1, 8), date(2024, 1, 9))
    engine, driver, steps = _run_driver(cfg, bars)
    last_mon = next(s for s in steps if s.asof.isoformat() == "2024-01-08T20:45:00+00:00")
    assert last_mon.signals == {} and last_mon.targets == {}  # no decision, no order
    orders = engine.orders()
    assert not any(o.created_asof == last_mon.asof for o in orders)
    # The flatten order is origin FLATTEN, created before the bar it fills on (I8).
    flat = [o for o in orders if o.origin is OrderOrigin.FLATTEN]
    assert len(flat) == 2 and all(o.side is Side.SELL for o in flat)
    assert all(o.created_asof < o.fill_asof for o in flat)
    # At every session end the position count is zero.
    for s in steps:
        if driver.is_session_end(s.ts):
            assert s.bar_result.snapshot.sleeves[0].positions == []
    # And the target book was cleared at the end of the session, then re-decided next morning.
    assert driver.book.weights(S) == {}


def test_without_the_flag_the_position_is_carried_overnight() -> None:
    bars = _two_sessions()
    cfg = _cfg(hold_spec(["AAA"]), date(2024, 1, 8), date(2024, 1, 9), flatten=False)
    res = run_backtest(cfg, bars)
    assert len(res.open_positions) == 1  # the control: only the flag removes the overnight hold


def test_flatten_flag_requires_a_15m_bar_strategy() -> None:
    daily = RulesSpec.model_validate(
        {
            "id": "d",
            "name": "d",
            "asset_class": "equity",
            "universe": ["AAA"],
            "schedule": {"freq": "daily"},
            "entry": {"always": True},
        }
    )
    with pytest.raises(ValueError, match="intraday_flatten"):
        build_engine_config(_engine_cfg(), [SleeveSpec(sleeve_id=S, strategy=daily)])


def test_early_close_day_is_flattened_at_its_own_last_bar() -> None:
    # Fri 2023-11-24 closes at 13:00 New York (14 bars, last opens 17:45 UTC).
    idx = multi_session_index(date(2023, 11, 22), date(2023, 11, 27))
    assert len(idx.normalize().unique()) == 3  # Wed, Fri, Mon (Thanksgiving closed)
    closes = [50.0] * len(idx)
    bars = {"AAA": frame_from_closes(idx, closes)}
    cfg = _cfg(hold_spec(["AAA"]), date(2023, 11, 24), date(2023, 11, 24))
    engine, driver, steps = _run_driver(cfg, bars)
    assert len(steps) == 14
    sells = [e for e in engine.ledger if e.type is EventType.FILL and e.side is FillSide.SELL]
    assert [s.asof.isoformat() for s in sells] == ["2023-11-24T17:45:00+00:00"]
    assert engine.snapshot().sleeves[0].positions == []


def test_session_end_of_a_partial_final_day_is_not_assumed() -> None:
    """A live run whose data stops mid-session must not flatten on the last bar it has."""
    idx = multi_session_index(date(2024, 1, 5), date(2024, 1, 9))
    cut = idx[:-8]  # Tuesday's data ends at 14:00 New York
    bars = {"AAA": frame_from_closes(cut, [100.0] * len(cut))}
    cfg = _cfg(hold_spec(["AAA"]), date(2024, 1, 8), date(2024, 1, 9))
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
    driver = BarDriver(engine, cfg.sleeves, bars, start=cfg.start)
    assert driver.is_session_end(pd.Timestamp("2024-01-08T20:45:00Z"))
    assert not driver.is_session_end(cut[-1])
    assert driver.is_session_end(pd.Timestamp("2024-01-05T20:45:00Z"))


def test_pending_orders_are_cancelled_at_the_close() -> None:
    """A BUY that could not fill (no bar for its symbol) is cancelled, not carried overnight."""
    idx = multi_session_index(date(2024, 1, 5), date(2024, 1, 8))
    aaa = frame_from_closes(idx, [100.0] * len(idx))
    bbb_idx = idx[idx < pd.Timestamp("2024-01-08T20:15:00Z")]  # BBB stops trading before the end
    bbb = frame_from_closes(bbb_idx, [20.0] * len(bbb_idx))
    spec = hold_spec(["AAA", "BBB"])
    cfg = _cfg(spec, date(2024, 1, 8), date(2024, 1, 8))
    engine, _driver, steps = _run_driver(cfg, {"AAA": aaa, "BBB": bbb})
    assert not any(o.status.value == "PENDING" for o in engine.orders())
    assert engine.snapshot().sleeves[0].positions == []
    assert steps[-1].bar_result.snapshot.sleeves[0].positions == []


def test_benchmark_runs_on_the_same_15m_clock_without_flatten() -> None:
    bars = _two_sessions()
    cfg = _cfg(hold_spec(["AAA"]), date(2024, 1, 8), date(2024, 1, 9))
    bars["SPY"] = frame_from_closes(bars["AAA"].index, [400.0] * len(bars["AAA"]))
    rep = run_with_benchmark(cfg, bars)
    assert rep.result.periods_per_year == 252 * 26
    spy = rep.benchmark_equity["SPY"]
    assert len(spy) == len(rep.result.equity) == 52
    # The benchmark is invested from bar 2 on, nights included.
    assert rep.benchmark_metrics["SPY"].exposure_avg == pytest.approx(51 / 52)
    assert rep.metrics.exposure_avg == pytest.approx(48 / 52)  # 24 of 26 bars a day, then flat
    assert "T" in rep.to_json() and '"2024-01-08T14:30:00+00:00"' in rep.to_json()


# ------------------------------------------------------------------ lean marking == full marking


def _random_cfg() -> tuple[BacktestConfig, dict[str, pd.DataFrame]]:
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    bars = make_intraday_bars(syms, date(2024, 1, 2), date(2024, 1, 19), seed=11, bar_vol=0.004)
    spec = RulesSpec.model_validate(
        {
            "id": "stocks_rand15",
            "name": "r",
            "asset_class": "equity",
            "timeframe": "15m",
            "universe": syms,
            "parameters": {"k": 2},
            "schedule": {"freq": "bar"},
            "entry": {"lt": [{"ind": "rsi", "period": 2}, 30]},
            "exit": {"gt": [{"ind": "rsi", "period": 2}, 60]},
            "position": {
                "weight": "equal",
                "max_positions": "$k",
                "rank": {"by": {"ind": "rsi", "period": 2}, "order": "asc"},
            },
            "stops": [
                {
                    "type": "atr_trailing",
                    "period": 14,
                    "mult": 1.5,
                    "anchor": "highest_close_since_entry",
                }
            ],
        }
    )
    return _cfg(spec, date(2024, 1, 5), date(2024, 1, 19), t1=True), bars


def _ledger_dump(engine: PortfolioEngine) -> list[str]:
    return [e.model_dump_json() for e in engine.ledger]


def test_lean_and_full_marking_give_the_identical_ledger() -> None:
    cfg, bars = _random_cfg()
    results = {}
    for lean in (True, False):
        engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
        driver = BarDriver(engine, cfg.sleeves, bars, start=cfg.start, lean=lean)
        for _ in driver.run(end=cfg.end):
            pass
        engine.verify()
        results[lean] = (_ledger_dump(engine), engine.snapshot().equity)
    assert len(results[True][0]) > 20  # the scenario really trades
    assert results[True] == results[False]


@pytest.mark.slow
@pytest.mark.timeout(120)
def test_daily_golden_is_identical_in_lean_mode() -> None:
    from golden import harness as h

    bars = h.load_bars()
    cfg = h.golden_config("G4")
    out = {}
    for lean in (False, True):
        engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
        driver = BarDriver(engine, cfg.sleeves, bars, start=cfg.start, lean=lean)
        for _ in driver.run(end=cfg.end):
            pass
        out[lean] = (h.fills_sha(h.fills_of(engine)), engine.snapshot().equity)
    assert out[True] == out[False]
    assert out[False][1] == h.run_golden("G4").final_equity


# ------------------------------------------------------------------ restore


def test_restoring_an_intraday_engine_mid_run_continues_identically() -> None:
    cfg, bars = _random_cfg()
    ecfg = build_engine_config(cfg.engine, cfg.sleeves)

    full = PortfolioEngine(ecfg)
    full_driver = BarDriver(full, cfg.sleeves, bars, start=cfg.start)
    steps = list(full_driver.run(end=cfg.end))

    cut = len(steps) // 2
    part = PortfolioEngine(ecfg)
    part_driver = BarDriver(part, cfg.sleeves, bars, start=cfg.start)
    cal = part_driver.master_calendar(end=cfg.end)
    for ts in cal[:cut]:
        part_driver.step(ts)
    text = part.export_state().to_json()
    book = part_driver.book.state()

    restored = PortfolioEngine(ecfg, state=EngineState.from_json(text))
    restored.verify()
    driver2 = BarDriver(
        restored, cfg.sleeves, bars, start=cfg.start, book=TargetBook.from_state([S], book)
    )
    rest = list(driver2.run(end=cfg.end))  # resumes after the saved bar-open timestamp
    assert len(rest) == len(steps) - cut
    assert _ledger_dump(restored) == _ledger_dump(full)
    assert restored.export_state().to_json() == full.export_state().to_json()
