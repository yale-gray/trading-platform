"""Wave 6b platform follow-ups for 15m portfolios (ADR-0001 A21): schedule, DB, reset, API, CLI."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import SQLModel, select
from support.intraday_synth import make_intraday_bars
from test_daytrading_preset import FakeIntradayData, _defn, _frames, utc

from tradebot import cli
from tradebot.api.app import create_app
from tradebot.clock import FixedClock
from tradebot.config import Settings
from tradebot.data.types import Timeframe
from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.db.engine import migrate_asof
from tradebot.db.models import LedgerEventRow, OrderRow, PositionSnapshot
from tradebot.portfolio.models import EventType
from tradebot.runner import Runner, create_portfolio, reset_from, restore_context
from tradebot.runner.clock import pause_end_bar_15m
from tradebot.runner.scheduler import NY, _intraday_window, build_scheduler, intraday_trigger
from tradebot.runner.state import RunnerError

V1 = "/api/v1"

# ------------------------------------------------------------------ the 15-minute schedule


def test_intraday_job_fires_at_bar_open_plus_16_minutes_on_sessions_only() -> None:
    trig = intraday_trigger()
    t = datetime(2024, 1, 5, 9, 0, tzinfo=NY)  # Friday, before the open
    fires = []
    for _ in range(5):
        t = trig.get_next_fire_time(None, t)
        fires.append(t.strftime("%a %H:%M"))
        t += timedelta(seconds=1)
    assert fires == ["Fri 09:01", "Fri 09:16", "Fri 09:31", "Fri 09:46", "Fri 10:01"]
    after_last = trig.get_next_fire_time(None, datetime(2024, 1, 5, 16, 47, tzinfo=NY))
    assert after_last.strftime("%a %H:%M") == "Mon 09:01"  # no weekend fires


def test_intraday_window_covers_first_to_last_bar_and_early_closes() -> None:
    def w(y: int, m: int, d: int, hh: int, mm: int) -> bool:
        return _intraday_window(datetime(y, m, d, hh, mm, tzinfo=NY))

    assert not w(2024, 1, 8, 9, 31)  # first bar (09:30) is not complete until 09:46
    assert w(2024, 1, 8, 9, 46) and w(2024, 1, 8, 12, 1)
    assert w(2024, 1, 8, 16, 1)  # last bar (15:45) is due at 16:01
    assert not w(2024, 1, 8, 16, 31)
    assert not w(2024, 1, 13, 10, 1)  # Saturday
    assert not w(2024, 1, 15, 10, 1)  # MLK holiday
    assert w(2023, 11, 24, 13, 1) and not w(2023, 11, 24, 14, 1)  # early close 13:00
    assert not w(2024, 1, 8, 20, 0) and not w(2024, 1, 8, 7, 0)


def test_intraday_job_ticks_only_15m_portfolios_inside_the_window(db: Engine) -> None:
    class Spy(Runner):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            self.calls: list[str] = []

        def tick(self, now: datetime | None = None, **kw: Any):  # type: ignore[override]
            self.calls.append(kw.get("scope", "all"))
            return []

    clock = FixedClock(utc(8, 15, 1))  # 10:01 ET Monday
    runner = Spy(db, FakeIntradayData({}), Settings(), clock, refresh=False)  # type: ignore[arg-type]
    sched = build_scheduler(runner, BackgroundScheduler(timezone="UTC"), clock=clock)
    job = next(j for j in sched.get_jobs() if j.id == "intraday_bar")
    job.func()
    assert runner.calls == ["intraday"]
    clock.set(utc(13, 15, 1))  # Saturday
    job.func()
    assert runner.calls == ["intraday"]


def test_intraday_scope_skips_daily_portfolios_and_restart_goes_through_tick(
    db: Engine, tmp_path: Path
) -> None:
    from runner_support import DATA_START, seed

    from tradebot.runner import fund_definition

    with session_scope(db) as s:
        create_portfolio(s, _defn(tmp_path), utc(8, 12, 0))
    seed(db, [fund_definition(date(2019, 1, 2), data_start=DATA_START, benchmark=None)])
    runner = Runner(
        db, FakeIntradayData(_frames()), Settings(), FixedClock(utc(8, 21, 30)), refresh=False
    )  # type: ignore[arg-type]
    res = runner.tick(scope="intraday")
    assert [(t.portfolio, t.status) for t in res] == [("day_trading", "ok")]
    assert len(res[0].processed) == 26
    # A restart (new Runner, same DB) catches up through the very same tick.
    again = Runner(
        db, FakeIntradayData(_frames()), Settings(), FixedClock(utc(9, 21, 30)), refresh=False
    )  # type: ignore[arg-type]
    res = again.tick(scope="intraday")
    assert res[0].status == "ok" and len(res[0].processed) == 26


# ------------------------------------------------------------------ DB timestamps


def _old_schema_db(path: Path) -> None:
    """Create a DB exactly as the date-only schema did: ``DATE`` asof columns."""
    eng = make_engine(path)
    try:
        SQLModel.metadata.create_all(eng)
    finally:
        eng.dispose()
    con = sqlite3.connect(path)
    try:
        for table, cols in (
            ("ledger_event", ("asof",)),
            ("order", ("created_asof", "fill_asof")),
            ("position_snapshot", ("entry_asof",)),
        ):
            sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            for c in cols:
                assert re.search(rf"\b{c} VARCHAR", sql)
                sql = re.sub(rf"\b{c} VARCHAR", f"{c} DATE", sql)
            con.execute(f'DROP TABLE "{table}"')
            con.execute(sql)
        con.commit()
    finally:
        con.close()


def _ledger_asofs(db: Engine) -> list[Any]:
    with session_scope(db) as s:
        return [r.asof for r in s.exec(select(LedgerEventRow).order_by(LedgerEventRow.seq))]


def test_asof_columns_hold_full_timestamps_and_daily_rows_stay_dates(
    db: Engine, tmp_path: Path
) -> None:
    with session_scope(db) as s:
        create_portfolio(s, _defn(tmp_path), utc(8, 12, 0))
    Runner(
        db, FakeIntradayData(_frames()), Settings(), FixedClock(utc(8, 21, 30)), refresh=False
    ).tick()  # type: ignore[arg-type]
    asofs = _ledger_asofs(db)
    stamped = [a for a in asofs if isinstance(a, datetime)]
    assert asofs and len(stamped) == len(asofs) - 2  # all but the two sleeve deposits (date.min)
    assert any(a.hour * 60 + a.minute != 0 for a in stamped)  # time of day survives
    with session_scope(db) as s:
        orders = list(s.exec(select(OrderRow)))
        assert orders and all(isinstance(o.created_asof, datetime) for o in orders)
        fills = [o.fill_asof for o in orders if o.fill_asof is not None]
        assert fills and all(isinstance(f, datetime) for f in fills)


def test_migration_repairs_a_db_created_by_the_old_schema(tmp_path: Path) -> None:
    # 1. a current DB with 15m history; remember what the ledger/orders hold.
    good = make_engine(tmp_path / "good.db")
    create_all(good)
    try:
        with session_scope(good) as s:
            create_portfolio(s, _defn(tmp_path), utc(8, 12, 0))
        Runner(
            good, FakeIntradayData(_frames()), Settings(), FixedClock(utc(8, 21, 30)), refresh=False
        ).tick()  # type: ignore[arg-type]
        expected = _ledger_asofs(good)
        with session_scope(good) as s:
            exp_orders = [
                (o.order_id, o.created_asof, o.fill_asof) for o in s.exec(select(OrderRow))
            ]
    finally:
        good.dispose()
    # 2. degrade it to what the old schema left behind: DATE columns holding truncated dates.
    old_path = tmp_path / "old.db"
    _old_schema_db(old_path)
    con = sqlite3.connect(old_path)
    src = sqlite3.connect(tmp_path / "good.db")
    try:
        names = [
            r[0]
            for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not r[0].startswith("sqlite_")
        ]
        for table in names:
            cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
            q = ",".join("?" * len(cols))
            for row in src.execute(f'SELECT * FROM "{table}"'):
                row = [
                    v[:10] if c in ("asof", "created_asof", "fill_asof") and v else v
                    for c, v in zip(cols, row, strict=True)
                ]
                con.execute(f'INSERT INTO "{table}" VALUES ({q})', row)
        con.commit()
        assert (
            con.execute('SELECT sql FROM sqlite_master WHERE name="ledger_event"')
            .fetchone()[0]
            .count(" DATE")
        )
    finally:
        con.close()
        src.close()
    # 3. opening it with the new code migrates it (idempotently) and it restores/verifies.
    db = make_engine(old_path)
    try:
        create_all(db)
        assert _ledger_asofs(db) == expected
        with session_scope(db) as s:
            got = [(o.order_id, o.created_asof, o.fill_asof) for o in s.exec(select(OrderRow))]
            assert got == exp_orders
            p = repo.get_portfolio(s, "day_trading")
            assert p is not None
            restore_context(s, p).engine.verify()
        assert migrate_asof(db) == 0  # idempotent
        create_all(db)
        assert _ledger_asofs(db) == expected
    finally:
        db.dispose()


def test_daily_rows_are_not_rewritten_by_the_migration(db: Engine) -> None:
    assert migrate_asof(db) == 0  # nothing to repair on a fresh DB; daily JSON holds plain dates


@pytest.fixture
def cli_db() -> Iterator[Engine]:
    """The DB the CLI will open (``TRADEBOT_DB_PATH`` from the autouse isolation fixture)."""
    engine = make_engine(Settings().db_path)
    yield engine
    engine.dispose()


@pytest.fixture
def live_db_copy(tmp_path: Path) -> Path | None:
    src = Path(__file__).resolve().parents[2] / "tradebot.db"
    if not src.exists():
        return None
    dst = tmp_path / "copy.db"
    ro = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    try:
        ro.backup(out)  # a consistent snapshot, the live file is never written
    finally:
        out.close()
        ro.close()
    return dst


def test_a_copy_of_the_live_db_still_opens_and_restores(live_db_copy: Path | None) -> None:
    if live_db_copy is None:
        pytest.skip("no local tradebot.db")
    db = make_engine(live_db_copy)
    try:
        create_all(db)
        with session_scope(db) as s:
            for p in repo.list_portfolios(s):
                restore_context(s, p).engine.verify()
    finally:
        db.dispose()


# ------------------------------------------------------------------ reset-from


def _seeded_intraday(db: Engine, tmp_path: Path, until: datetime) -> Runner:
    with session_scope(db) as s:
        create_portfolio(s, _defn(tmp_path), utc(8, 12, 0))
    runner = Runner(db, FakeIntradayData(_frames()), Settings(), FixedClock(until), refresh=False)  # type: ignore[arg-type]
    assert runner.tick()[0].status == "ok"
    return runner


def test_reset_from_a_session_date_resumes_at_its_first_bar(db: Engine, tmp_path: Path) -> None:
    runner = _seeded_intraday(db, tmp_path, utc(9, 16, 1))  # Mon + Tue morning, positions held
    res = reset_from(
        db, "day_trading", date(2024, 1, 11), operator="t", note="n", now=utc(11, 12, 0)
    )
    assert res.open_positions  # reported, flattened at the first resumed session's close
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None and p.id is not None
        assert repo.get_runner_state(s, p.id).last_processed_bar_ts == datetime(2024, 1, 11, 14, 15)
        ctx = restore_context(s, p)
        assert not [o for o in ctx.engine.orders() if o.status.value == "PENDING"]
    runner.discard_contexts()
    out = Runner(
        db, FakeIntradayData(_frames()), Settings(), FixedClock(utc(11, 21, 30)), refresh=False
    ).tick()  # type: ignore[arg-type]
    assert (
        out[0].processed[0] == pd.Timestamp("2024-01-11T14:30:00Z") and len(out[0].processed) == 26
    )
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None
        snap = restore_context(s, p).engine.snapshot()
        assert all(sl.positions == [] for sl in snap.sleeves)  # flat after the resumed session


def test_reset_from_refuses_non_sessions_the_past_and_the_future(
    db: Engine, tmp_path: Path
) -> None:
    _seeded_intraday(db, tmp_path, utc(9, 16, 1))
    now = utc(10, 22, 0)
    with pytest.raises(RunnerError, match=r"not an XNYS session.*2024-01-16"):
        reset_from(db, "day_trading", date(2024, 1, 13), operator="t", note="n", now=utc(20, 1, 0))
    with pytest.raises(RunnerError, match="after the last processed bar"):
        reset_from(db, "day_trading", date(2024, 1, 9), operator="t", note="n", now=now)
    with pytest.raises(RunnerError, match="future"):
        reset_from(db, "day_trading", date(2024, 1, 12), operator="t", note="n", now=now)


def test_pause_end_bar_15m_counts_bars_across_sessions() -> None:
    assert pause_end_bar_15m(datetime(2024, 1, 8, 20, 30), 1) == datetime(2024, 1, 8, 20, 45)
    assert pause_end_bar_15m(datetime(2024, 1, 8, 20, 45), 26) == datetime(2024, 1, 9, 20, 45)
    assert pause_end_bar_15m(datetime(2024, 1, 8, 20, 45), 520) is not None


# ------------------------------------------------------------------ API


@pytest.fixture
def client15(db: Engine, tmp_path: Path) -> Iterator[TestClient]:
    _seeded_intraday(db, tmp_path, utc(9, 21, 30))
    app = create_app(
        Settings(), engine=db, data_factory=lambda: None, clock=FixedClock(utc(9, 21, 30))
    )  # type: ignore[arg-type]
    with TestClient(app) as c:
        yield c


def test_api_exposes_intraday_timestamps(client15: TestClient) -> None:
    pid = client15.get(f"{V1}/portfolios").json()[0]["id"]
    eq = client15.get(f"{V1}/portfolios/{pid}/equity").json()
    assert len(eq) == 52 and eq[0]["ts"].startswith("2024-01-08T14:30:00")
    assert eq[1]["ts"].startswith("2024-01-08T14:45:00")
    sub = client15.get(
        f"{V1}/portfolios/{pid}/equity",
        params={"from": "2024-01-09T14:30:00", "to": "2024-01-09T14:45:00"},
    ).json()
    assert [p["ts"][:19] for p in sub] == ["2024-01-09T14:30:00", "2024-01-09T14:45:00"]
    day = client15.get(
        f"{V1}/portfolios/{pid}/equity", params={"from": "2024-01-09", "to": "2024-01-09"}
    ).json()
    assert len(day) == 26
    assert client15.get(f"{V1}/portfolios/{pid}/equity", params={"from": "junk"}).status_code == 422
    trades = client15.get(f"{V1}/portfolios/{pid}/trades", params={"limit": 500}).json()
    assert trades["total"] > 0
    stamps = [t["ts"] for t in trades["items"]]
    assert all("T" in x and not x.endswith("T00:00:00") for x in stamps)
    assert len({x[:10] for x in stamps}) > 1 and len(set(stamps)) > 2
    orders = client15.get(f"{V1}/portfolios/{pid}/orders").json()
    assert orders and all(len(o["created_asof"]) > 10 for o in orders)
    assert (
        client15.get(f"{V1}/portfolios/{pid}")
        .json()["last_processed_bar"]
        .startswith("2024-01-09T20:45:00")
    )


# ------------------------------------------------------------------ CLI end to end


class CliData(FakeIntradayData):
    """Synthetic 15m bars for any symbol the portfolio asks for (the real source is offline)."""

    def __init__(self) -> None:
        super().__init__({})

    def get_bars_many(
        self, symbols: Sequence[str], start: date, end: date, **kw: Any
    ) -> dict[str, pd.DataFrame]:
        missing = [s for s in symbols if s not in self.frames]
        if missing:
            self.frames.update(
                make_intraday_bars(
                    missing, date(2023, 12, 1), date(2024, 1, 31), seed=3, bar_vol=0.006
                )
            )
        return super().get_bars_many(symbols, start, end, **kw)


@pytest.mark.slow
def test_cli_end_to_end_daytrading_ticks_and_flattens_at_the_close(
    cli_db: Engine, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    data = CliData()
    monkeypatch.setattr(cli, "_market_data", lambda _s: data)
    now = {"v": utc(8, 21, 30)}
    monkeypatch.setattr(cli, "_now", lambda: now["v"])
    assert cli.main(["db", "init"]) == 0
    for preset in ("daytrading", "bench_spy_15m"):
        assert cli.main(["portfolio", "create", "--preset", preset, "--start", "2024-01-08"]) == 0
    capsys.readouterr()
    # Mid-session first: only completed bars (09:30 .. 10:30 ET) are processed.
    assert cli.main(["runner", "tick", "--now", "2024-01-08T15:31:00+00:00"]) == 0
    assert "day_trading: ok (4 bars)" in capsys.readouterr().out
    # Then the rest of Monday and Tuesday.
    assert cli.main(["runner", "tick", "--now", "2024-01-09T21:30:00+00:00"]) == 0
    out = capsys.readouterr().out
    assert "day_trading: ok (48 bars)" in out and "bench_spy_15m: ok (48 bars)" in out
    assert cli.main(["runner", "tick", "--now", "2024-01-09T21:31:00+00:00"]) == 0
    assert "day_trading: noop" in capsys.readouterr().out
    db = cli_db
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "day_trading")
        assert p is not None and p.id is not None
        ctx = restore_context(s, p)
        fills = [e for e in ctx.engine.ledger if e.type is EventType.FILL]
        assert fills and Timeframe.M15 in data.calls
        # No position survives a night: after every session's last bar the books are empty.
        assert repo.latest_positions(s, p.id) == []
        held = {
            r.ts
            for r in s.exec(select(PositionSnapshot).where(PositionSnapshot.portfolio_id == p.id))
        }
        assert held, "the strategies never held anything; the test would prove nothing"
        for day in (8, 9):
            assert datetime(2024, 1, day, 20, 45) not in held  # empty after each session's last bar
        flat = [e for e in fills if e.stage.value == "CLOSE"]
        assert flat and {e.asof.hour * 60 + e.asof.minute for e in flat} == {
            20 * 60 + 45
        }  # 15:45 ET
        # Buys happen only before the close bar; the ledger asof is the bar open (a datetime).
        assert all(isinstance(e.asof, datetime) for e in fills)
        ctx.engine.verify()
