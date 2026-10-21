"""The three sleeves end to end on the synthetic fixtures (SPEC-002 AC 6, 8, 9, 13).

Runs go through ``run_with_benchmark`` -> ``BarDriver`` -> the real ``PortfolioEngine``. The
fixtures are generated (not real prices). Expected values are structural (which sleeves trade,
which benchmarks exist, P, E_0) and are not pinned golden numbers; golden updates need a PM note.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from support.backtest_fixtures import RUN_END, RUN_START

from tradebot.backtest.engine import BacktestConfig, SleeveSpec, config_hash, run_with_benchmark
from tradebot.data.types import AssetClass
from tradebot.portfolio.config import CostConfig, default_config
from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.io import STRATEGIES_DIR, load_strategy
from tradebot.strategy.schema import STRATEGIES

pytestmark = pytest.mark.integration

STRATEGY_SLEEVES = ("core_momentum", "mean_reversion", "crypto_trend")
INITIAL_CAPITAL = Decimal("100000.00")


def _fund_config() -> BacktestConfig:
    sleeves = [SleeveSpec(sleeve_id=s, strategy=STRATEGIES[s]) for s in STRATEGY_SLEEVES]
    sleeves.append(SleeveSpec(sleeve_id="cash", strategy=None))
    return BacktestConfig(sleeves=sleeves, start=RUN_START, end=RUN_END, engine=default_config())


def _single_sleeve_config(sleeve_id: str) -> BacktestConfig:
    """One strategy sleeve holding 100% of the fund (no cash sleeve)."""
    base = default_config()
    sc = next(s for s in base.sleeves if s.sleeve_id == sleeve_id)
    engine = base.model_copy(
        update={"sleeves": (sc.model_copy(update={"target_weight": Decimal(1)}),)}
    )
    return BacktestConfig(
        sleeves=[SleeveSpec(sleeve_id=sleeve_id, strategy=STRATEGIES[sleeve_id])],
        start=RUN_START,
        end=RUN_END,
        engine=engine,
    )


@pytest.fixture(scope="module")
def fund_report(fixture_bars):
    return run_with_benchmark(_fund_config(), fixture_bars)


def load_bars_for(sleeve_id: str) -> dict[str, pd.DataFrame]:
    from support.backtest_fixtures import load_fixture_bars

    symbols = {str(s) for s in STRATEGIES[sleeve_id].required_symbols()} | {"SPY", "BTC/USD"}
    return load_fixture_bars(symbols)


def _fills_by_sleeve(report) -> dict[str, list[date]]:
    out: dict[str, list[date]] = {}
    for ev in report.result.ledger:
        if ev.type.value == "FILL":
            out.setdefault(ev.sleeve_id, []).append(ev.asof)
    return out


def test_fund_trades_all_three_sleeves_and_the_cash_buffer_never_trades(fund_report) -> None:
    fills = _fills_by_sleeve(fund_report)
    for sid in STRATEGY_SLEEVES:
        assert fills.get(sid), f"{sid} made no fills on the fixtures"
    assert "cash" not in fills
    assert fund_report.result.n_signals > 0
    assert len(fund_report.result.trades) > 0


def test_fund_starts_at_initial_capital_and_uses_the_mixed_portfolio_calendar(fund_report) -> None:
    # E_0 equals initial capital: no fill can happen on the first bar (signals fill next open).
    eq = fund_report.result.equity
    assert eq.iloc[0] == float(INITIAL_CAPITAL)
    assert eq.index[0] == pd.Timestamp(RUN_START, tz="UTC")
    # The fund holds crypto, so the mixed portfolio is annualised with P = 365 (SPEC-002 section 7).
    assert fund_report.result.periods_per_year == 365
    assert fund_report.metrics.periods_per_year == 365


def test_benchmarks_are_spy_btc_and_blend_for_the_full_fund(fund_report) -> None:
    # Fund weights: equity = core 0.50 + mean_reversion 0.20 = 0.70; crypto = 0.20; cash 0.10.
    # Benchmarks: SPY (equity), BTC/USD (crypto), and a static blend 0.2 BTC / 0.7 SPY / 0.1 cash.
    assert set(fund_report.benchmark_metrics) == {"SPY", "BTC/USD", "blend"}
    assert set(fund_report.benchmark_equity) == {"SPY", "BTC/USD", "blend"}
    for name, eq in fund_report.benchmark_equity.items():
        assert eq.index.equals(fund_report.result.equity.index), name
        assert eq.iloc[0] == float(INITIAL_CAPITAL), name  # benchmarks also start from E_0
    for name, delta in fund_report.deltas.items():
        assert set(delta) == {"total_return", "cagr", "max_drawdown", "sharpe", "sortino"}, name


def test_no_fill_before_the_strategy_warmup_is_covered(fund_report, fixture_bars) -> None:
    # The first trade may fill no earlier than calendar bar warmup_bars (0-based), i.e. bar
    # warmup + 1 in 1-based terms. This is a floor; with the 2019 run start it always holds.
    fills = _fills_by_sleeve(fund_report)
    for sid in STRATEGY_SLEEVES:
        compiled = compile_strategy(STRATEGIES[sid], sid)
        calendar = compiled.prepare(fixture_bars).calendar
        earliest = calendar[compiled.warmup_bars]
        assert min(fills[sid]) >= earliest.date(), sid


@pytest.mark.timeout(90)
def test_repeated_run_gives_a_byte_identical_report(fund_report, fixture_bars) -> None:
    again = run_with_benchmark(_fund_config(), fixture_bars)
    assert again.to_json() == fund_report.to_json()


def test_config_hash_is_stable_and_changes_with_a_resolved_parameter(tmp_path) -> None:
    base = _fund_config()
    assert config_hash(base) == config_hash(_fund_config())

    # stop_mult 2.5 -> 3 in mean_reversion changes a resolved parameter, so the hash must change.
    text = (STRATEGIES_DIR / "mean_reversion.yaml").read_text()
    assert "stop_mult: 2.5" in text
    path = tmp_path / "mean_reversion_wide_stop.yaml"
    path.write_text(text.replace("stop_mult: 2.5", "stop_mult: 3"))
    sleeves = [
        SleeveSpec(
            sleeve_id=s, strategy=load_strategy(path) if s == "mean_reversion" else STRATEGIES[s]
        )
        for s in STRATEGY_SLEEVES
    ] + [SleeveSpec(sleeve_id="cash", strategy=None)]
    changed = BacktestConfig(sleeves=sleeves, start=RUN_START, end=RUN_END, engine=default_config())
    assert config_hash(changed) != config_hash(base)

    # An equity slippage change (5 -> 6 bps) is also a resolved input, so the hash changes too.
    base_engine = default_config()
    costs = dict(base_engine.costs)
    costs[AssetClass.EQUITY] = CostConfig(
        asset_class=AssetClass.EQUITY, fee_bps=Decimal("0"), slip_bps=Decimal("6")
    )
    slipped = base.model_copy(update={"engine": base_engine.model_copy(update={"costs": costs})})
    assert config_hash(slipped) != config_hash(base)


def test_single_equity_sleeve_is_benchmarked_against_spy_only_on_a_252_day_year() -> None:
    report = run_with_benchmark(
        _single_sleeve_config("core_momentum"), load_bars_for("core_momentum")
    )
    assert set(report.benchmark_metrics) == {"SPY"}
    assert report.result.periods_per_year == 252
    assert set(_fills_by_sleeve(report)) == {"core_momentum"}


def test_single_crypto_sleeve_is_benchmarked_against_btc_only_on_a_365_day_year() -> None:
    report = run_with_benchmark(
        _single_sleeve_config("crypto_trend"), load_bars_for("crypto_trend")
    )
    assert set(report.benchmark_metrics) == {"BTC/USD"}
    assert report.result.periods_per_year == 365
    assert set(_fills_by_sleeve(report)) == {"crypto_trend"}
