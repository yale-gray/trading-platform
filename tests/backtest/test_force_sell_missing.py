"""Delisted holdings (ADR-0001 A33 B1): ``EngineConfig.force_sell_missing`` in daily mode."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from tradebot.backtest.driver import BarDriver, build_engine_config
from tradebot.backtest.engine import BacktestConfig, SleeveSpec, config_hash
from tradebot.backtest.trades import closed_trades
from tradebot.data.types import AssetClass
from tradebot.portfolio.config import CostConfig, EngineConfig, SleeveConfig, default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType, FillSide
from tradebot.risk.config import RiskConfig
from tradebot.strategy.schema import RulesSpec

pytestmark = pytest.mark.unit

D = Decimal
S = "stk"
SLIP = D(10)  # bp
DAYS = pd.bdate_range("2024-01-02", periods=30, tz="UTC")


def _engine_cfg(force: bool) -> EngineConfig:
    base = default_config()
    return EngineConfig(
        initial_capital=D("10000.00"),
        sleeves=(
            SleeveConfig(
                sleeve_id=S,
                name="stk",
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
                asset_class=AssetClass.EQUITY, fee_bps=D(0), slip_bps=SLIP
            ),
            AssetClass.CRYPTO: base.costs[AssetClass.CRYPTO],
        },
        force_sell_missing=force,
    )


def _frame(closes: list[float], start: int = 0) -> pd.DataFrame:
    idx = DAYS[start : start + len(closes)]
    c = np.asarray(closes, dtype="float64")
    return pd.DataFrame(
        {"open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 1000.0}, index=idx
    )


SPEC = RulesSpec.model_validate(
    {
        "id": "hold_two",
        "name": "hold",
        "asset_class": "equity",
        "universe": ["AAA", "BBB"],
        "schedule": {"freq": "daily"},
        "entry": {"always": True},
        "position": {"weight": 0.5},
    }
)


def _run(aaa: list[float], force: bool, bbb_len: int = 30):  # noqa: ANN202
    bars = {"AAA": _frame(aaa), "BBB": _frame([50.0] * bbb_len)}
    cfg = BacktestConfig(
        sleeves=[SleeveSpec(sleeve_id=S, strategy=SPEC)],
        start=DAYS[0].date(),
        end=date(2024, 3, 1),
        engine=_engine_cfg(force),
        decision_lag_bars=0,
    )
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
    driver = BarDriver(
        engine, cfg.sleeves, bars, start=cfg.start, decision_lag_bars=cfg.decision_lag_bars
    )
    steps = list(driver.run(end=cfg.end))
    return engine, steps


def _aaa_sells(engine: PortfolioEngine):  # noqa: ANN202
    return [
        e
        for e in engine.ledger
        if e.type is EventType.FILL and e.symbol == "AAA" and e.side is FillSide.SELL
    ]


def _expected_sell(last_close: str) -> Decimal:
    return (D(last_close) * (1 - (SLIP + 50) / D(10000))).quantize(
        D("1E-8"), rounding="ROUND_FLOOR"
    )


def test_acquisition_like_ending_is_sold_at_last_close_minus_haircut() -> None:
    engine, steps = _run([100.0] * 15, force=True)  # AAA's bars end on bar 15
    sells = _aaa_sells(engine)
    assert len(sells) == 1
    assert sells[0].price == _expected_sell("100")
    assert sells[0].asof == DAYS[15].date()  # the first calendar bar without an AAA bar
    assert sells[0].metadata["flag"] == "DELIST_NO_BAR"
    trades = [t for t in closed_trades(engine.ledger, {o.order_id: o for o in engine.orders()})]
    assert [t.exit_reason for t in trades if t.symbol == "AAA"] == ["delist_no_bar"]
    held = {p.symbol for p in steps[-1].bar_result.snapshot.sleeves[0].positions}
    assert "AAA" not in held
    engine.verify()


def test_crash_like_ending_is_written_off() -> None:
    closes = [100.0] * 10 + [90.0, 70.0, 50.0, 40.0, 30.0]  # -70% over the last 5 bars
    engine, _ = _run(closes, force=True)
    sells = _aaa_sells(engine)
    assert len(sells) == 1
    assert sells[0].price == D("1E-8")
    assert sells[0].amount == D("0.00")
    assert sells[0].metadata["flag"] == "DELIST_NO_BAR"
    engine.verify()


def test_one_day_halt_is_sold_conservatively() -> None:
    # AAA has no bar on bar 12 only, then resumes: the halt triggers the exit anyway.
    full = _frame([100.0] * 30)
    bars = {"AAA": full.drop(full.index[12]), "BBB": _frame([50.0] * 30)}
    cfg = BacktestConfig(
        sleeves=[SleeveSpec(sleeve_id=S, strategy=SPEC)],
        start=DAYS[0].date(),
        end=date(2024, 3, 1),
        engine=_engine_cfg(True),
        decision_lag_bars=0,
    )
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))
    driver = BarDriver(engine, cfg.sleeves, bars, start=cfg.start, decision_lag_bars=0)
    list(driver.run(end=cfg.end))
    first = _aaa_sells(engine)[0]
    assert first.asof == DAYS[12].date()
    assert first.price == _expected_sell("100")
    engine.verify()


def test_flag_off_keeps_the_old_behaviour_position_stays_open() -> None:
    engine, steps = _run([100.0] * 15, force=False)
    assert _aaa_sells(engine) == []
    held = {p.symbol for p in steps[-1].bar_result.snapshot.sleeves[0].positions}
    assert "AAA" in held


def test_flag_is_omitted_from_serialisation_and_hash_unless_set() -> None:
    off, on = _engine_cfg(False), _engine_cfg(True)
    assert "force_sell_missing" not in off.model_dump(mode="json")
    assert on.model_dump(mode="json")["force_sell_missing"] is True

    def h(eng: EngineConfig) -> str:
        return config_hash(
            BacktestConfig(
                sleeves=[SleeveSpec(sleeve_id=S, strategy=SPEC)],
                start=DAYS[0].date(),
                end=date(2024, 3, 1),
                engine=eng,
            )
        )

    assert h(off) != h(on)
    assert h(off) == h(off.model_copy(update={"force_sell_missing": False}))


def _raw(closes: list[float], vols: list[float]) -> pd.DataFrame:
    f = _frame(closes)
    f["high"] = f["low"] = f["open"] = f["close"]
    f["volume"] = vols
    return f


def test_clean_alpaca_frame_drops_filler_and_cuts_after_a_run_of_five() -> None:
    from tradebot.data.pit_prices import clean_alpaca_frame

    closes = [10.0, 11.0, 12.0, 12.0, 13.0, 14.0, 14.0, 14.0, 14.0, 14.0, 14.0, 9.0, 9.5]
    vols = [5.0, 5.0, 5.0, 0.0, 5.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 5.0, 5.0]
    out, stats = clean_alpaca_frame(_raw(closes, vols))
    # lone filler at 3 is dropped; run of 5 (bars 6..10) cuts everything from bar 6 on
    assert list(out["close"]) == [10.0, 11.0, 12.0, 13.0, 14.0]
    assert stats == {"filler_dropped": 1, "truncated_rows": 7}
    # a run of four is only dropped, not cut
    short, s2 = clean_alpaca_frame(_raw(closes[:11] + [9.0], vols[:6] + [0.0] * 4 + [5.0, 5.0]))
    assert s2["truncated_rows"] == 0 and s2["filler_dropped"] == 5
    assert len(short) == 7


def test_quality_report_counts_filler_bars() -> None:
    from datetime import UTC, datetime

    from tradebot.data.quality import validate_bars

    f = _raw([10.0 + i for i in range(10)], [5.0] * 4 + [0.0, 0.0] + [5.0] * 4)
    _, rep = validate_bars(f, "AAA", datetime(2025, 1, 1, tzinfo=UTC))
    assert len(rep.filler_bars) == 2
