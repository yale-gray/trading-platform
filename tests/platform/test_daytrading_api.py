"""Day Trading page API: cash split, round trips, per-session P&L, the ``unsettled`` column."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlmodel import select
from test_daytrading_hardening import START, _frames_without, hold_defn
from test_daytrading_preset import FakeIntradayData, utc

from tradebot.api.app import create_app
from tradebot.backtest.trades import closed_trades
from tradebot.clock import FixedClock
from tradebot.config import Settings
from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.db.engine import migrate_unsettled
from tradebot.db.models import EquitySnapshot
from tradebot.runner import Runner, benchmark_definition, create_portfolio, restore_context
from tradebot.sim.orders import Order

V1 = "/api/v1"


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, Engine, int]]:
    """Mon + Tue of a day-trading portfolio; BBB has no last bar on Monday (a no-bar flatten)."""
    tmp = tmp_path_factory.mktemp("dtapi")
    db = make_engine(tmp / "dt.db")
    create_all(db)
    frames = _frames_without("BBB", "2024-01-08T20:45:00Z")
    with session_scope(db) as s:
        create_portfolio(s, hold_defn(tmp), utc(8, 12, 0))
    runner = Runner(
        db, FakeIntradayData(frames), Settings(), FixedClock(utc(9, 22, 0)), refresh=False
    )  # type: ignore[arg-type]
    assert runner.tick()[0].status == "ok"
    app = create_app(
        Settings(),
        engine=db,
        data_factory=lambda: None,
        clock=FixedClock(utc(9, 22, 0)),  # type: ignore[arg-type]
    )
    with TestClient(app) as c:
        pid = c.get(f"{V1}/portfolios").json()[0]["id"]
        yield c, db, pid
    db.dispose()


def test_detail_splits_cash_into_spendable_and_arriving_tomorrow(api) -> None:  # noqa: ANN001
    client, db, pid = api
    d = client.get(f"{V1}/portfolios/{pid}").json()
    cash, uns, settled = (
        Decimal(d["cash"]),
        Decimal(d["unsettled_cash"]),
        Decimal(d["settled_cash"]),
    )
    assert settled == cash - uns and uns > 0  # Tuesday's sales arrive on Wednesday
    assert cash == Decimal(d["equity"])  # flat at the close
    assert d["as_of"].startswith("2024-01-09T20:45:00")
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None
        snap = restore_context(s, p).engine.snapshot()
    assert (snap.cash, snap.unsettled) == (cash, uns)  # the same as the engine's own view


def test_roundtrips_pair_entries_and_exits_and_name_the_exit_reason(api) -> None:  # noqa: ANN001
    client, db, pid = api
    r = client.get(f"{V1}/portfolios/{pid}/roundtrips").json()
    items = r["items"]
    assert r["total"] == len(items) >= 4  # 2 sleeves x 2 sessions
    assert [i["exit_ts"] for i in items] == sorted((i["exit_ts"] for i in items), reverse=True)
    assert {i["exit_reason"] for i in items} == {"flatten", "flatten_no_bar"}
    nobar = [i for i in items if i["exit_reason"] == "flatten_no_bar"]
    assert [i["symbol"] for i in nobar] == ["BBB"] and nobar[0]["exit_ts"].startswith("2024-01-08")
    one = items[0]
    assert set(one) >= {
        "sleeve_key", "symbol", "entry_ts", "exit_ts", "qty", "entry_price", "exit_price",
        "fees", "pnl", "pnl_pct", "exit_reason",
    }  # fmt: skip
    assert one["entry_ts"] < one["exit_ts"]
    assert sum(Decimal(i["pnl"]) for i in items) == Decimal(r["total_pnl"])
    assert r["win_rate"] == sum(Decimal(i["pnl"]) > 0 for i in items) / len(items)
    # Same pairing as the backtester's own function over the persisted ledger.
    with session_scope(db) as s:
        orders = {
            o.order_id: Order.model_validate_json(o.order_json)
            for o in s.exec(
                __import__("sqlmodel").select(__import__("tradebot.db.models").db.models.OrderRow)
            )
        }
        expected = closed_trades(repo.ledger_events(s, pid), orders)
    assert sorted(Decimal(i["pnl"]) for i in items) == sorted(t.net_pnl for t in expected)
    for i in items:
        cost = Decimal(i["qty"]) * Decimal(i["entry_price"])
        assert i["pnl_pct"] == pytest.approx(float(Decimal(i["pnl"]) / cost * 100), rel=1e-9)


def test_roundtrips_filter_by_exit_time_sleeve_and_limit(api) -> None:  # noqa: ANN001
    client, _db, pid = api
    base = f"{V1}/portfolios/{pid}/roundtrips"
    monday = client.get(base, params={"from": "2024-01-08", "to": "2024-01-08"}).json()
    assert monday["total"] == 2 and all(
        i["exit_ts"].startswith("2024-01-08") for i in monday["items"]
    )
    assert (
        client.get(base, params={"limit": 1}).json()["items"][0]["exit_ts"].startswith("2024-01-09")
    )
    assert len(client.get(base, params={"limit": 1}).json()["items"]) == 1
    assert client.get(base, params={"sleeve": "dip"}).json()["total"] == 2
    empty = client.get(base, params={"from": "2030-01-01"}).json()
    assert empty["total"] == 0 and empty["win_rate"] is None and empty["items"] == []
    assert client.get(base, params={"from": "junk"}).status_code == 422
    assert client.get(f"{V1}/portfolios/999/roundtrips").status_code == 404


def test_daily_gives_one_row_per_new_york_session(api) -> None:  # noqa: ANN001
    client, db, pid = api
    rows = client.get(f"{V1}/portfolios/{pid}/daily").json()
    assert [r["date"] for r in rows] == ["2024-01-08", "2024-01-09"]
    mon, tue = rows
    assert Decimal(mon["start_equity"]) == Decimal("10000.00")
    assert Decimal(tue["start_equity"]) == Decimal(mon["end_equity"])
    for r in rows:
        assert Decimal(r["pnl"]) == Decimal(r["end_equity"]) - Decimal(r["start_equity"])
        assert r["pnl_pct"] == pytest.approx(
            float(Decimal(r["pnl"]) / Decimal(r["start_equity"]) * 100), rel=1e-9
        )
        assert r["n_roundtrips"] == 2
        assert r["benchmark_pnl_pct"] is None  # this portfolio has no benchmark
    detail = client.get(f"{V1}/portfolios/{pid}").json()
    assert Decimal(tue["end_equity"]) == Decimal(detail["equity"])
    assert [
        r["date"]
        for r in client.get(f"{V1}/portfolios/{pid}/daily", params={"from": "2024-01-09"}).json()
    ] == ["2024-01-09"]
    assert [
        r["date"]
        for r in client.get(f"{V1}/portfolios/{pid}/daily", params={"to": "2024-01-08"}).json()
    ] == ["2024-01-08"]


def test_daily_carries_the_benchmarks_session_return(tmp_path: Path) -> None:

    db = make_engine(tmp_path / "b.db")
    create_all(db)
    try:
        frames = _frames_without("BBB", "2024-01-08T20:45:00Z")
        frames["SPY"] = frames["AAA"] * 1.0
        defn = hold_defn(tmp_path, benchmark="bench_spy_15m")
        with session_scope(db) as s:
            create_portfolio(s, benchmark_definition("SPY", START, timeframe="15m"), utc(8, 12, 0))
            create_portfolio(s, defn, utc(8, 12, 0))
        runner = Runner(
            db, FakeIntradayData(frames), Settings(), FixedClock(utc(9, 22, 0)), refresh=False
        )  # type: ignore[arg-type]
        assert {t.status for t in runner.tick()} == {"ok"}
        app = create_app(
            Settings(), engine=db, data_factory=lambda: None, clock=FixedClock(utc(9, 22, 0))
        )  # type: ignore[arg-type]
        with TestClient(app) as c:
            pid = next(
                p["id"] for p in c.get(f"{V1}/portfolios").json() if p["name"] == "day_trading"
            )
            rows = c.get(f"{V1}/portfolios/{pid}/daily").json()
            bench = next(
                p for p in c.get(f"{V1}/portfolios").json() if p["name"] == "bench_spy_15m"
            )
        assert all(isinstance(r["benchmark_pnl_pct"], float) for r in rows)
        total = (1 + rows[0]["benchmark_pnl_pct"] / 100) * (1 + rows[1]["benchmark_pnl_pct"] / 100)
        assert total == pytest.approx(
            float(Decimal(bench["equity"]) / Decimal("10000.00")), rel=1e-6
        )
    finally:
        db.dispose()


# ------------------------------------------------------------------ the unsettled column


def test_snapshots_store_unsettled_and_old_rows_fall_back_to_the_ledger(api) -> None:  # noqa: ANN001
    client, db, pid = api
    with session_scope(db) as s:
        total = [r for r in s.exec(select(EquitySnapshot)) if r.sleeve_id == 0]
        assert all(r.unsettled is not None for r in total)
        assert any(Decimal(r.unsettled or "0") > 0 for r in total)  # type: ignore[arg-type]
    before = client.get(f"{V1}/portfolios/{pid}").json()
    with session_scope(db) as s:
        s.connection().execute(text("UPDATE equity_snapshot SET unsettled = NULL"))
    after = client.get(f"{V1}/portfolios/{pid}").json()
    assert (after["cash"], after["unsettled_cash"], after["settled_cash"]) == (
        before["cash"],
        before["unsettled_cash"],
        before["settled_cash"],
    )


def test_migration_adds_the_nullable_column_once_and_keeps_old_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    eng = make_engine(path)
    create_all(eng)
    eng.dispose()
    con = sqlite3.connect(path)
    con.execute('ALTER TABLE "equity_snapshot" DROP COLUMN "unsettled"')  # a pre-A21 database
    con.commit()
    cols = [r[1] for r in con.execute('PRAGMA table_info("equity_snapshot")')]
    con.close()
    assert "unsettled" not in cols
    eng = make_engine(path)
    try:
        assert migrate_unsettled(eng) is True
        assert migrate_unsettled(eng) is False  # idempotent
        create_all(eng)  # and create_all runs it too
    finally:
        eng.dispose()
    con = sqlite3.connect(path)
    info = {r[1]: r for r in con.execute('PRAGMA table_info("equity_snapshot")')}
    con.close()
    assert "unsettled" in info and info["unsettled"][3] == 0  # nullable (notnull flag 0)
