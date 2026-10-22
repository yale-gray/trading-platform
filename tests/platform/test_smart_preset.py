"""The $10k "smart investing" preset (ADR-0001 A12, A17) and the index_hold strategy."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from tradebot import cli
from tradebot.config import Settings
from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.runner import restore_context, smart_definition
from tradebot.runner.state import RunnerError
from tradebot.strategy.io import STRATEGIES_DIR, load_strategy

START = date(2019, 1, 2)


@pytest.fixture
def cli_db() -> Iterator[Engine]:
    engine = make_engine(Settings().db_path)
    create_all(engine)
    yield engine
    engine.dispose()


def test_index_hold_strategy_loads() -> None:
    spec = load_strategy(STRATEGIES_DIR / "index_hold.yaml")
    assert spec.universe == ["SPY", "QQQ"] and spec.stops == []


def test_smart_definition_layout_and_caps() -> None:
    d = smart_definition(START)
    cfg = d.engine_config()
    assert d.name == "smart_investing" and d.benchmark == "bench_spy" and d.kind == "fund"
    assert cfg.initial_capital == Decimal("10000.00")
    assert {s.sleeve_id: s.target_weight for s in cfg.sleeves} == {
        "index": Decimal("0.50"),
        "stocks": Decimal("0.40"),
        "crypto": Decimal("0.10"),
    }
    assert cfg.risk.max_asset_fraction == Decimal("0.30")
    assert cfg.risk.max_crypto_fraction == Decimal("0.15")
    assert all(s.strategy is not None for s in d.sleeves)  # no cash sleeve
    assert {"SPY", "QQQ", "BTC/USD"} <= set(d.required_symbols())


def test_missing_stocks_file_is_a_clear_error(tmp_path: Path) -> None:
    for stem in ("index_hold", "crypto_trend"):
        (tmp_path / f"{stem}.yaml").write_text((STRATEGIES_DIR / f"{stem}.yaml").read_text())
    with pytest.raises(RunnerError, match="stocks_momentum_cash.yaml"):
        smart_definition(START, strategies_dir=tmp_path)


def test_cli_create_smart_preset_and_restore(cli_db: Engine, capsys: Any) -> None:
    assert cli.main(["portfolio", "create", "--preset", "smart", "--start", "2019-01-02"]) == 0
    with session_scope(cli_db) as s:
        p = repo.get_portfolio(s, "smart_investing")
        assert p is not None and p.started_at.date() == START
        assert Decimal(p.initial_cash) == Decimal("10000.00")
        ctx = restore_context(s, p)
        assert ctx.engine.snapshot().equity == Decimal("10000.00")
    assert cli.main(["portfolio", "create", "--preset", "smart", "--start", "2019-01-02"]) == 1


def test_smart_account_runs_and_holds_each_etf_near_25_percent(
    db: Engine, market_cache: Path, tmp_path: Path
) -> None:
    """End to end on the synthetic cache. The stocks sleeve is stubbed with a SPY-only hold."""
    from runner_support import DATA_START, after_close, make_data, make_runner, seed

    for stem in ("index_hold", "crypto_trend"):
        (tmp_path / f"{stem}.yaml").write_text((STRATEGIES_DIR / f"{stem}.yaml").read_text())
    stub = (STRATEGIES_DIR / "index_hold.yaml").read_text()
    stub = (
        stub.replace("id: index_hold", "id: stocks_momentum_cash")
        .replace("universe: [SPY, QQQ]", "universe: [SPY]")
        .replace("weight: 0.5", "weight: 0.5")
    )
    (tmp_path / "stocks_momentum_cash.yaml").write_text(stub)
    seed(
        db,
        [smart_definition(START, data_start=DATA_START, strategies_dir=tmp_path, benchmark=None)],
    )
    make_runner(db, make_data(market_cache)).tick(after_close(date(2019, 2, 15)))
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "smart_investing")
        assert p is not None
        snap = restore_context(s, p).engine.snapshot()
    assert not snap.killed
    held = {(sl.sleeve_id, pos.symbol): pos for sl in snap.sleeves for pos in sl.positions}
    for sym in ("SPY", "QQQ"):
        w = held[("index", sym)].mv / snap.equity
        assert Decimal("0.20") < w < Decimal("0.30"), (sym, w)


def test_smart_preset_uses_the_no_fallback_stocks_variant() -> None:
    d = smart_definition(START)
    stocks = next(s for s in d.sleeves if s.sleeve_id == "stocks").strategy
    assert stocks is not None and stocks.id == "stocks_momentum_cash" and stocks.fallback is None
    single = load_strategy(STRATEGIES_DIR / "stocks_momentum.yaml")
    assert single.fallback is not None and single.fallback.symbol == "SPY"
    cash = load_strategy(STRATEGIES_DIR / "stocks_momentum_cash.yaml")
    assert cash.universe == single.universe and cash.parameters == single.parameters


def test_bear_market_leaves_stocks_sleeve_in_cash_with_no_spy_clipping(
    db: Engine, market_cache: Path, tmp_path: Path
) -> None:
    """No stock passes the filter (a bear market): the stocks sleeve stays cash (audit F2)."""
    import yaml
    from runner_support import DATA_START, after_close, make_data, make_runner, seed

    for stem in ("index_hold", "crypto_trend"):
        (tmp_path / f"{stem}.yaml").write_text((STRATEGIES_DIR / f"{stem}.yaml").read_text())
    raw = yaml.safe_load((STRATEGIES_DIR / "stocks_momentum_cash.yaml").read_text())
    raw["universe"] = ["XLK", "XLF", "XLE"]
    raw["parameters"]["top_n"] = 3
    raw["eligibility"] = {"gt": [{"ind": "sma", "period": 20}, {"ind": "sma", "period": 20}]}
    (tmp_path / "stocks_momentum_cash.yaml").write_text(yaml.safe_dump(raw))
    seed(
        db,
        [smart_definition(START, data_start=DATA_START, strategies_dir=tmp_path, benchmark=None)],
    )
    make_runner(db, make_data(market_cache)).tick(after_close(date(2019, 3, 15)))
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "smart_investing")
        assert p is not None
        snap = restore_context(s, p).engine.snapshot()
    stocks = next(sl for sl in snap.sleeves if sl.sleeve_id == "stocks")
    assert stocks.positions == [] and stocks.cash > 0
    spy_mv = sum(pos.mv for sl in snap.sleeves for pos in sl.positions if pos.symbol == "SPY")
    assert Decimal("0.20") < spy_mv / snap.equity <= Decimal("0.30")


def test_smart_description_carries_the_survivorship_label(cli_db: Engine, capsys: Any) -> None:
    from tradebot.data.universe import SP500_LABEL
    from tradebot.runner.bootstrap import SMART_DESCRIPTION

    assert SP500_LABEL == "current S&P 500 constituents (survivorship-biased)"
    assert SP500_LABEL in SMART_DESCRIPTION
    assert cli.main(["portfolio", "create", "--preset", "smart", "--start", "2019-01-02"]) == 0
    assert SP500_LABEL in capsys.readouterr().out
