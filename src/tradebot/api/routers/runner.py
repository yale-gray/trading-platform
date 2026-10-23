"""Runner status derived from ``runner_state`` and the heartbeat row."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request

from tradebot.api.deps import Db
from tradebot.api.schemas import RunnerStatus, SleeveStatus
from tradebot.db import repo
from tradebot.db.models import RunnerHeartbeat
from tradebot.runner.clock import next_equity_run_after

router = APIRouter(prefix="/runner", tags=["runner"])

ALIVE_WITHIN = timedelta(minutes=15)  # heartbeat is written every 5 minutes


@router.get("/status", response_model=RunnerStatus)
def runner_status(db: Db, request: Request) -> RunnerStatus:
    """Heartbeat age plus the per-portfolio processing marker (``key`` = portfolio name)."""
    hb = db.get(RunnerHeartbeat, 1)
    now = repo.naive_utc(request.app.state.clock.now())
    rows = []
    for p in repo.list_portfolios(db):
        assert p.id is not None
        st = repo.get_runner_state(db, p.id)
        rows.append(
            SleeveStatus(
                key=p.name,
                last_processed_bar=st.last_processed_bar_ts,
                last_status=st.last_status,
                last_error=st.last_error,
            )
        )
    return RunnerStatus(
        alive=bool(hb and now - hb.ts <= ALIVE_WITHIN),
        last_heartbeat=hb.ts if hb else None,
        sleeves=rows,
        next_run_after=next_equity_run_after(request.app.state.clock.now()),
    )
