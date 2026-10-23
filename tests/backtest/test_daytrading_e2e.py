"""The real day-trading preset (both strategies, flatten, T+1) on seeded synthetic 15m bars.

Not a performance test: it checks the account rules hold over a few weeks of trading.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from support.intraday_synth import make_intraday_bars

from tradebot.backtest.driver import BarDriver, build_engine_config
from tradebot.backtest.engine import BacktestConfig, SleeveSpec, run_with_benchmark
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType, FillSide
from tradebot.runner.bootstrap import daytrading_definition

NY = ZoneInfo("America/New_York")
START, END = date(2024, 1, 12), date(2024, 1, 31)


@pytest.fixture(scope="module")
def run():  # noqa: ANN201
    defn = daytrading_definition(START)
    syms = sorted(defn.required_symbols())[:40]
    sleeves = [
        SleeveSpec(sleeve_id=s.sleeve_id, strategy=s.strategy.model_copy(update={"universe": syms}))
        for s in defn.sleeves
        if s.strategy
    ]
    bars = make_intraday_bars(syms, date(2024, 1, 2), END, seed=21, bar_vol=0.004)
    cfg = BacktestConfig(sleeves=sleeves, start=START, end=END, engine=defn.engine)
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
    driver = BarDriver(engine, cfg.sleeves, bars, start=START)
    steps = list(driver.run(end=END))
    return cfg, bars, engine, driver, steps


def test_no_position_is_ever_held_overnight(run) -> None:  # noqa: ANN001
    _cfg, _bars, engine, driver, steps = run
    ends = [s for s in steps if driver.is_session_end(s.ts)]
    assert len(ends) == 13  # Jan 12 .. 31 2024: 1 + 4 + 5 + 3 sessions (MLK day closed)
    for s in ends:
        assert all(sl.positions == [] for sl in s.bar_result.snapshot.sleeves)
    engine.verify()


def test_at_most_five_positions_per_sleeve_and_trades_stay_inside_a_day(run) -> None:  # noqa: ANN001
    cfg, _bars, engine, _driver, steps = run
    busiest = max(len(sl.positions) for s in steps for sl in s.bar_result.snapshot.sleeves)
    assert 0 < busiest <= 5
    from tradebot.backtest.trades import closed_trades

    orders = {o.order_id: o for o in engine.orders()}
    trades = closed_trades(engine.ledger, orders)
    assert len(trades) > 20  # the seeded data really trades
    assert {t.exit_reason for t in trades} <= {"signal", "stop", "flatten"}
    for t in trades:
        assert t.entry_asof.astimezone(NY).date() == t.exit_asof.astimezone(NY).date()


def test_no_buy_is_created_on_a_session_end_bar(run) -> None:  # noqa: ANN001
    _cfg, _bars, engine, driver, steps = run
    ends = {s.asof for s in steps if driver.is_session_end(s.ts)}
    assert not any(o.created_asof in ends for o in engine.orders() if o.side.value == "BUY")


def test_buys_never_use_the_proceeds_of_the_same_day(run) -> None:  # noqa: ANN001
    """Each day a sleeve starts flat with all its cash settled; it can never buy more than that."""
    cfg, _bars, engine, _driver, steps = run
    day = lambda ts: ts.astimezone(NY).date()  # noqa: E731
    start_cash: dict[tuple[str, date], Decimal] = {}
    prev: dict[str, Decimal] = {}
    prev_day: date | None = None
    for s in steps:
        d = day(s.asof)
        if d != prev_day:
            start_cash.update({(sid, d): v for sid, v in prev.items()})
            prev_day = d
        prev = {sl.sleeve_id: sl.cash for sl in s.bar_result.snapshot.sleeves}
    spent: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
    for e in engine.ledger:
        if e.type is EventType.FILL and e.side is FillSide.BUY:
            spent[(e.sleeve_id, day(e.asof))] += e.amount
    assert spent
    for key, amount in spent.items():
        if key in start_cash:  # (the first day has no prior close; it starts from the deposit)
            assert amount <= start_cash[key], key


def test_the_preset_runs_through_the_benchmarked_backtest() -> None:
    defn = daytrading_definition(START)
    syms = sorted(defn.required_symbols())[:10]
    sleeves = [
        SleeveSpec(sleeve_id=s.sleeve_id, strategy=s.strategy.model_copy(update={"universe": syms}))
        for s in defn.sleeves
        if s.strategy
    ]
    bars = make_intraday_bars([*syms, "SPY"], date(2024, 1, 2), date(2024, 1, 19), seed=4)
    cfg = BacktestConfig(sleeves=sleeves, start=START, end=date(2024, 1, 19), engine=defn.engine)
    rep = run_with_benchmark(cfg, bars)
    assert set(rep.benchmark_metrics) == {"SPY"}
    assert rep.result.open_positions == []  # nothing carried past the final close
    assert rep.result.periods_per_year == 6552
    assert isinstance(rep.result.equity.index, pd.DatetimeIndex)
    assert rep.to_json() == run_with_benchmark(cfg, bars).to_json()  # deterministic
