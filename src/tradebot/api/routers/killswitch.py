"""Kill-switch endpoints. The ack records human review only; it never resets (ADR D8)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from tradebot.api.deps import Db
from tradebot.api.routers.portfolios import kill_switch_out, require_portfolio
from tradebot.api.schemas import KillSwitchAckIn, KillSwitchOut
from tradebot.db import repo

router = APIRouter(prefix="/portfolios", tags=["kill-switch"])


@router.get("/{portfolio_id}/kill-switch", response_model=KillSwitchOut)
def get_kill_switch(db: Db, portfolio_id: int) -> KillSwitchOut:
    """Read-only kill-switch state."""
    return kill_switch_out(db, require_portfolio(db, portfolio_id))


@router.post("/{portfolio_id}/kill-switch/ack", response_model=KillSwitchOut)
def ack_kill_switch(
    db: Db, portfolio_id: int, body: KillSwitchAckIn, request: Request
) -> KillSwitchOut:
    """Record that a human reviewed the trip. ``tripped`` is left untouched."""
    p = require_portfolio(db, portfolio_id)
    assert p.id is not None
    now = repo.naive_utc(request.app.state.clock.now())
    if not repo.acknowledge_kill_switch(db, p.id, body.note, now):
        raise HTTPException(409, "kill switch is not tripped; nothing to acknowledge")
    db.flush()
    return kill_switch_out(db, p)
