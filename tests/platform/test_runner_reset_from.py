"""``runner.reset_from``: the operator's gap override (audit 004 F1, F2, G1)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from runner_support import (
    DATA_START,
    START,
    SYNTH_NOW,
    after_close,
    db_fingerprint,
    make_data,
    make_runner,
    seed,
)
from sqlalchemy import Engine
from sqlmodel import select

from tradebot.db import create_all, make_engine, repo, session_scope
from tradebot.db.models import EngineCheckpoint, OrderRow, RunLog
from tradebot.runner import RunnerError, fund_definition, reset_from, restore_context

pytestmark = pytest.mark.restart

RESUME = START + timedelta(days=20)
_OPEN: list[Engine] = []


@pytest.fixture(autouse=True)
def _dispose_engines() -> Iterator[None]:
    yield
    while _OPEN:
        _OPEN.pop().dispose()


def _world(tmp_path: Path, cache: Path, tag: str) -> Engine:
    db = make_engine(tmp_path / f"{tag}.db")
    create_all(db)
    _OPEN.append(db)
    seed(db, [fund_definition(START, data_start=DATA_START, benchmark=None)])
    make_runner(db, make_data(cache)).tick(after_close(START))  # one bar: orders now pending
    return db


def _pending(db: Engine) -> list[str]:
    with session_scope(db) as s:
        p = repo.get_portfolio(s, "paper_fund")
        assert p is not None
        ctx = restore_context(s, p)
        return [o.order_id for o in ctx.engine.orders() if o.status.value == "PENDING"]


def test_reset_from_cancels_pending_orders_clears_the_book_and_next_tick_is_gap_skipping(
    tmp_path: Path, market_cache: Path
) -> None:
    a = _world(tmp_path, market_cache, "a")
    b = _world(tmp_path, market_cache, "b")
    pending = _pending(a)
    assert pending, "the first bar must leave orders pending for the reset to cancel"

    now = after_close(RESUME)
    res = reset_from(a, "paper_fund", RESUME, operator="op", note="gap", now=now)
    assert res.skipped_after == START and sorted(res.cancelled_orders) == sorted(pending)
    assert _pending(a) == []
    with session_scope(a) as s:
        p = repo.get_portfolio(s, "paper_fund")
        assert p is not None and p.id is not None
        cp = repo.latest_checkpoint(s, p.id)
        assert cp is not None and cp.bar_ts == datetime(RESUME.year, RESUME.month, RESUME.day - 1)
        books = json.loads(cp.target_book_json)["sleeves"]
        assert all(v["decision_ts"] is None and v["weights"] == {} for v in books.values())
        log = s.exec(select(RunLog).where(RunLog.outcome == "reset")).one()
        detail = json.loads(log.detail_json)
        assert detail["operator"] == "op" and detail["target_book_cleared"] is True
        rows = {r.order_id: r.status for r in s.exec(select(OrderRow))}
        assert all(rows[oid] == "CANCELLED" for oid in pending)

    # run A straight on; run B does the same reset but restarts the process before ticking
    reset_from(b, "paper_fund", RESUME, operator="op", note="gap", now=now)
    day = RESUME
    for _ in range(3):
        make_runner(a, make_data(market_cache)).tick(after_close(day))
        day += timedelta(days=1)
    make_runner(b, make_data(market_cache)).tick(after_close(day - timedelta(days=1)))
    assert db_fingerprint(a) == db_fingerprint(b)
    # nothing filled from the cancelled pre-gap orders
    with session_scope(a) as s:
        rows = {r.order_id: r.status for r in s.exec(select(OrderRow))}
        assert all(rows[oid] == "CANCELLED" for oid in pending)
        cps = [c.bar_ts.date() for c in s.exec(select(EngineCheckpoint))]
        assert RESUME - timedelta(days=1) in cps and RESUME in cps


def test_reset_from_refuses_a_future_date_and_a_non_advancing_one(
    tmp_path: Path, market_cache: Path
) -> None:
    db = _world(tmp_path, market_cache, "f")
    before = db_fingerprint(db)
    now = SYNTH_NOW
    with pytest.raises(RunnerError, match="future"):
        reset_from(db, "paper_fund", now.date() + timedelta(days=1), operator="o", note="", now=now)
    with pytest.raises(RunnerError, match="after the last processed bar"):
        reset_from(db, "paper_fund", START, operator="o", note="", now=now)
    with pytest.raises(RunnerError, match="unknown portfolio"):
        reset_from(db, "nope", RESUME, operator="o", note="", now=now)
    assert db_fingerprint(db) == before


def test_missing_benchmark_snapshot_is_logged_in_run_log(
    tmp_path: Path, market_cache: Path
) -> None:
    db = make_engine(tmp_path / "m.db")
    create_all(db)
    _OPEN.append(db)
    seed(db, [fund_definition(START, data_start=DATA_START, benchmark="bench_spy")])
    make_runner(db, make_data(market_cache)).tick(after_close(START))
    with session_scope(db) as s:
        logs = list(s.exec(select(RunLog)))
        assert logs and json.loads(logs[0].detail_json)["benchmark_missing"] == "bench_spy"
