"""Kill-switch mirror and the operator reset (SPEC-004 AC 15, ADR-0001 D8)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from runner_support import DATA_START, START, after_close, make_data, make_runner, seed
from sqlalchemy import Engine
from sqlmodel import select

from tradebot.db import repo, session_scope
from tradebot.db.models import KillSwitchEvent, LedgerEventRow
from tradebot.portfolio.config import default_config
from tradebot.risk.config import RiskConfig
from tradebot.runner import fund_definition, reset_kill_switch, restore_context

pytestmark = pytest.mark.restart

NOW = after_close(date(2019, 3, 1))


@pytest.fixture
def tripped(db: Engine, market_cache: Path) -> Engine:
    """A fund with a hair-trigger kill switch, run until it trips."""
    engine_cfg = default_config().model_copy(
        update={"risk": RiskConfig(kill_switch_drawdown=Decimal("0.0005"))}
    )
    seed(db, [fund_definition(START, data_start=DATA_START, engine=engine_cfg, benchmark=None)])
    make_runner(db, make_data(market_cache)).tick(NOW)
    return db


def test_trip_is_mirrored_and_survives_restore(tripped: Engine) -> None:
    with session_scope(tripped) as s:
        p = repo.get_portfolio(s, "paper_fund")
        assert p is not None and p.id is not None
        assert p.status == "halted"
        ks = repo.get_kill_switch(s, p.id)
        assert ks is not None and ks.tripped and ks.tripped_at_bar == datetime(2019, 1, 4)
        assert ks.trigger_drawdown is not None and Decimal(ks.trigger_drawdown) > Decimal("0.0005")
        kinds = [e.kind for e in s.exec(select(KillSwitchEvent))]
        assert kinds == ["trip"]
        ctx = restore_context(s, p)  # restore + verify() works on a tripped book
        assert ctx.engine.snapshot().killed


def test_api_ack_does_not_reset_but_cli_reset_does(tripped: Engine, market_cache: Path) -> None:
    with session_scope(tripped) as s:
        p = repo.get_portfolio(s, "paper_fund")
        assert p is not None and p.id is not None
        assert repo.acknowledge_kill_switch(s, p.id, "looked at it", NOW)
        ks = repo.get_kill_switch(s, p.id)
        assert ks is not None and ks.tripped and ks.acknowledged_at is not None
        before = repo.ledger_count(s, p.id)

    asof = reset_kill_switch(tripped, "paper_fund", operator="yale", note="reviewed", now=NOW)
    assert asof == date(2019, 3, 1)

    with session_scope(tripped) as s:
        p = repo.get_portfolio(s, "paper_fund")
        assert p is not None and p.id is not None
        ks = repo.get_kill_switch(s, p.id)
        assert ks is not None and not ks.tripped
        assert p.status == "active"
        assert repo.ledger_count(s, p.id) == before + 1
        last = s.exec(
            select(LedgerEventRow)
            .where(LedgerEventRow.portfolio_id == p.id)
            .order_by(
                LedgerEventRow.seq.desc()  # type: ignore[attr-defined]
            )
        ).first()
        assert last is not None and last.type == "KILL_SWITCH_RESET"
        assert [e.kind for e in s.exec(select(KillSwitchEvent))] == ["trip", "ack", "reset"]
        ctx = restore_context(s, p)  # fresh checkpoint is consistent with the ledger rows
        assert not ctx.engine.snapshot().killed

    # the runner continues from the reset state
    res = make_runner(tripped, make_data(market_cache)).tick(after_close(date(2019, 3, 3)))
    assert res[0].status == "ok" and res[0].processed == [date(2019, 3, 2), date(2019, 3, 3)]


def test_reset_of_an_untripped_switch_is_refused(db: Engine, market_cache: Path) -> None:
    seed(db, [fund_definition(START, data_start=DATA_START, benchmark=None)])
    make_runner(db, make_data(market_cache)).tick(after_close(date(2019, 1, 4)))
    with pytest.raises(ValueError, match="not tripped"):
        reset_kill_switch(db, "paper_fund", operator="x", note="y", now=NOW)
