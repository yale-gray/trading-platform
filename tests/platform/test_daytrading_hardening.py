"""Audit 007 fixes through the runner: SIP-only parity (M1/A22), no-bar flatten (M2), late flatten
(m7) and batched SIP refreshes (M3)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine
from support.intraday_synth import make_intraday_bars
from test_daytrading_preset import ALWAYS_IN, FakeIntradayData, utc

from tradebot.backtest.engine import BacktestConfig, run_backtest
from tradebot.clock import FixedClock
from tradebot.config import Settings
from tradebot.data import MarketData
from tradebot.data.config import DataConfig
from tradebot.data.providers.alpaca_provider import AlpacaCredentials, AlpacaProvider
from tradebot.data.types import AssetClass, Timeframe
from tradebot.db import repo, session_scope
from tradebot.db.models import RunLog
from tradebot.portfolio.models import EventType
from tradebot.runner import Runner, create_portfolio, daytrading_definition, restore_context
from tradebot.runner.state import PortfolioDef

START = date(2024, 1, 8)
SIP_DELAY = timedelta(minutes=16)
BAR = timedelta(minutes=15)


def hold_defn(
    tmp_path: Path, a: str = "AAA", b: str = "BBB", benchmark: str | None = None
) -> PortfolioDef:
    """Two always-in sleeves, one symbol each (``dip`` holds ``a``, ``breakout`` holds ``b``)."""
    (tmp_path / "stocks_dt_dip.yaml").write_text(
        ALWAYS_IN.format(sid="stocks_dt_dip", universe=f"[{a}]")
    )
    (tmp_path / "stocks_dt_breakout.yaml").write_text(
        ALWAYS_IN.format(sid="stocks_dt_breakout", universe=f"[{b}]")
    )
    return daytrading_definition(START, strategies_dir=tmp_path, benchmark=benchmark)


class SipData(FakeIntradayData):
    """Serves only bars a SIP feed would have by ``now``: a bar opened at t is there at t+31 min."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        super().__init__(frames)
        self.now = datetime(2024, 1, 1, tzinfo=UTC)

    def get_bars_many(
        self, symbols: Sequence[str], start: date, end: date, **kw: Any
    ) -> dict[str, pd.DataFrame]:
        out = super().get_bars_many(symbols, start, end, **kw)
        cut = pd.Timestamp(self.now - BAR - SIP_DELAY)
        return {s: f.loc[f.index <= cut] for s, f in out.items()}


def _ledger_json(db: Engine, name: str) -> list[str]:
    with session_scope(db) as s:
        p = repo.get_portfolio(s, name)
        assert p is not None and p.id is not None
        return [e.model_dump_json() for e in repo.ledger_events(s, p.id)]


def test_runner_on_sip_only_data_matches_a_backtest_with_the_same_decision_lag(
    db: Engine, tmp_path: Path
) -> None:
    """A22: live path (latest bar withheld, 15-min ticks) == run_backtest(default lag 2)."""
    frames = make_intraday_bars(
        ["AAA", "BBB", "CCC"], date(2023, 12, 11), date(2024, 1, 12), seed=5, bar_vol=0.004
    )
    defn = hold_defn(tmp_path, "AAA", "CCC")
    with session_scope(db) as s:
        create_portfolio(s, defn, utc(8, 12, 0))
    data = SipData(frames)
    runner = Runner(db, data, Settings(), FixedClock(utc(8, 12, 0)), refresh=False)  # type: ignore[arg-type]
    now = utc(8, 14, 46)
    while now <= utc(9, 22, 30):
        data.now = now
        runner.tick(now)
        now += BAR
    data.now = utc(10, 0, 0)
    runner.tick(data.now)  # the last bar arrives late
    live = _ledger_json(db, "day_trading")
    assert any('"FILL"' in line for line in live)

    cfg = BacktestConfig(
        sleeves=list(defn.sleeves),
        start=defn.start,
        end=date(2024, 1, 9),
        engine=defn.engine_config(),
        decision_lag_bars=None,  # driver default: 2 bars for 15m (A22)
    )
    result = run_backtest(cfg, frames)
    assert live == [e.model_dump_json() for e in result.ledger]
    # And the lag is real: the first BUY fills three bars after the first decision bar.
    first_buy = next(e for e in result.ledger if e.type is EventType.FILL)
    assert first_buy.asof == datetime(2024, 1, 8, 15, 15, tzinfo=UTC)  # decided 09:30, filled 10:15


def _frames_without(sym: str, ts: str, *, all_symbols: bool = False) -> dict[str, pd.DataFrame]:
    frames = make_intraday_bars(
        ["AAA", "BBB", "CCC"], date(2023, 12, 11), date(2024, 1, 12), seed=5, bar_vol=0.004
    )
    drop = pd.Timestamp(ts)
    for s in frames if all_symbols else [sym]:
        frames[s] = frames[s].drop(drop)
    return frames


def _run_log(db: Engine, name: str) -> list[dict[str, Any]]:
    import json

    with session_scope(db) as s:
        p = repo.get_portfolio(s, name)
        assert p is not None and p.id is not None
        rows = s.exec(RunLog.__table__.select().where(RunLog.portfolio_id == p.id)).all()  # type: ignore[attr-defined]
    return [json.loads(r.detail_json) for r in rows]


def test_a_held_symbol_with_no_bar_at_flatten_is_sold_with_a_haircut_and_counted(
    db: Engine, tmp_path: Path
) -> None:
    frames = _frames_without("BBB", "2024-01-08T20:45:00Z")  # BBB has no last bar on Monday
    defn = hold_defn(tmp_path)
    with session_scope(db) as s:
        create_portfolio(s, defn, utc(8, 12, 0))
    runner = Runner(
        db, FakeIntradayData(frames), Settings(), FixedClock(utc(8, 22, 0)), refresh=False
    )  # type: ignore[arg-type]
    assert runner.tick()[0].status == "ok"
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None and p.id is not None
        fills = [e for e in repo.ledger_events(s, p.id) if e.type is EventType.FILL]
    sells = [e for e in fills if e.side is not None and e.side.value == "SELL"]
    bbb = next(e for e in sells if e.symbol == "BBB")
    aaa = next(e for e in sells if e.symbol == "AAA")
    assert bbb.metadata.get("flag") == "FLATTEN_NO_BAR" and "flag" not in aaa.metadata
    slip = defn.engine_config().costs[AssetClass.EQUITY].slip_bps  # normal slippage + 50 bps
    last_close = float(frames["BBB"]["close"].loc["2024-01-08T20:30:00Z"])
    assert float(bbb.price) <= last_close * (1 - float(slip + 50) / 10000) * (1 + 1e-6)
    assert float(bbb.price) >= last_close * (1 - float(slip + 50) / 10000) * (1 - 1e-6)
    counts = [d.get("flatten_no_bar", 0) for d in _run_log(db, "day_trading")]
    assert sum(counts) == 1 and counts[-1] == 1


def test_a_session_whose_last_bar_never_arrives_is_flattened_at_the_next_open(
    db: Engine, tmp_path: Path
) -> None:
    frames = _frames_without("", "2024-01-08T20:45:00Z", all_symbols=True)
    defn = hold_defn(tmp_path)
    with session_scope(db) as s:
        create_portfolio(s, defn, utc(8, 12, 0))
    runner = Runner(
        db, FakeIntradayData(frames), Settings(), FixedClock(utc(8, 22, 0)), refresh=False
    )  # type: ignore[arg-type]
    res = runner.tick()
    assert res[0].status == "data_unavailable"  # Monday's last bar is not there (yet)
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None
        snap = restore_context(s, p).engine.snapshot()
    held = [x for sl in snap.sleeves for x in sl.positions]
    assert held  # the stranded positions
    res = runner.tick(utc(9, 22, 0))  # Tuesday: later bars exist, the missing one is skipped
    assert res[0].status == "ok" and res[0].processed[0] == pd.Timestamp("2024-01-09T14:30:00Z")
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None and p.id is not None
        events = repo.ledger_events(s, p.id)
    late = [
        e
        for e in events
        if e.type is EventType.FILL and "FLATTEN_LATE" in str(e.metadata.get("flag"))
    ]
    assert {e.symbol for e in late} == {"AAA", "BBB"}
    assert all(e.asof == datetime(2024, 1, 9, 14, 30, tzinfo=UTC) for e in late)  # Tuesday's open
    tuesday = [
        e
        for e in events
        if isinstance(e.asof, datetime) and e.asof >= datetime(2024, 1, 9, 14, 30, tzinfo=UTC)
    ]
    assert tuesday[0].type is EventType.FILL and "FLATTEN_LATE" in str(tuesday[0].metadata)
    assert sum(d.get("flatten_late", 0) for d in _run_log(db, "day_trading")) == 2
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None
        snap = restore_context(s, p).engine.snapshot()
    assert all(sl.positions == [] for sl in snap.sleeves)  # and Tuesday itself ended flat


# ------------------------------------------------------------------ M3: batched SIP refresh


class _Resp:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _CountingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get(
        self, url: str, *, params: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> _Resp:
        self.calls.append(dict(params))
        return _Resp({"bars": {}})


def test_a_500_symbol_sip_refresh_uses_five_batched_requests(tmp_path: Path) -> None:
    now = FixedClock(datetime(2024, 6, 14, 22, 0, tzinfo=UTC))
    client = _CountingClient()
    provider = AlpacaProvider(
        credentials=AlpacaCredentials("PKTESTKEY1234567890ABCDEF", "x" * 40),
        client=client,  # type: ignore[arg-type]
        clock=now,
        sleep=lambda s: None,
    )
    md = MarketData(
        DataConfig(cache_dir=tmp_path), providers=[], clock=now, intraday_history=provider
    )
    symbols = [
        f"Q{chr(65 + i // 26 // 26)}{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}" for i in range(500)
    ]
    md.get_bars_many(
        symbols, date(2024, 6, 13), date(2024, 6, 14), timeframe=Timeframe.M15, refresh=True
    )
    assert len(client.calls) == 5  # not 500 (one per symbol) nor 50 (groups of 10)
    sizes = [len(c["symbols"].split(",")) for c in client.calls]
    assert sizes == [100] * 5 and all(c["feed"] == "sip" for c in client.calls)
