"""Monthly sleeve rebalance (spec 003 §8, invariants I7 and I16, ADR-0001 §5).

Flow (the engine calls these; nothing here reads a clock or keeps hidden state):

* step F: :func:`is_rebalance_day` then :func:`plan_rebalance` -> ``RebalancePlan``;
  :func:`book_planned` annotates the ledger (``REBALANCE_PLANNED``).
* while the plan is pending, :func:`sizing_equity` gives each sleeve's
  ``target_value`` for ``check_target_weights(..., sizing_equity=...)``.
* step C of later bars: :func:`execute_transfers` moves cash only (never
  positions) from overweight to underweight sleeves as paired
  ``CASH_TRANSFER_OUT`` / ``CASH_TRANSFER_IN`` events sharing a ``transfer_id``,
  retrying up to ``MAX_TRANSFER_ATTEMPTS`` bars before logging
  ``REBALANCE_INCOMPLETE`` and dropping the plan.

All money is integer cents internally; every split uses largest-remainder
rounding with ties broken by sleeve id, so totals net to exactly zero and the
result is independent of the ambient decimal context.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.portfolio.asof import AsOf
from tradebot.portfolio.config import EngineConfig
from tradebot.portfolio.ledger import Ledger
from tradebot.portfolio.models import Dec, EventType, LedgerEvent, PortfolioSnapshot, Stage
from tradebot.portfolio.money import money_context, q2
from tradebot.portfolio.portfolio import Portfolio
from tradebot.risk.killswitch import KillSwitch

MAX_TRANSFER_ATTEMPTS = 3
"""Step-C bars on which transfers are tried (first attempt + 2 retries)."""

PORTFOLIO_SLEEVE_ID = "__portfolio__"
"""``sleeve_id`` carried by portfolio-wide annotation events."""

ZERO = Decimal("0.00")


class RebalancePlan(BaseModel):
    """A pending monthly rebalance. JSON round-trips (Decimals as canonical text).

    ``surplus[s] = sleeve_equity_s - target_value_s`` (negative = deficit).
    ``moved_out`` / ``moved_in`` accumulate cents already transferred, so the
    remaining work survives persistence between bars.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asof: AsOf
    equity: Dec
    target_values: dict[str, Dec]
    surplus: dict[str, Dec]
    moved_out: dict[str, Dec] = {}
    moved_in: dict[str, Dec] = {}
    attempts: int = 0

    def remaining_surplus(self) -> dict[str, Decimal]:
        """Cash still to give up per overweight sleeve (> 0 entries only)."""
        with money_context():
            out = {s: v - self.moved_out.get(s, ZERO) for s, v in self.surplus.items() if v > 0}
        return {s: v for s, v in sorted(out.items()) if v > 0}

    def remaining_deficit(self) -> dict[str, Decimal]:
        """Cash still to receive per underweight sleeve (> 0 entries only)."""
        with money_context():
            out = {s: -v - self.moved_in.get(s, ZERO) for s, v in self.surplus.items() if v < 0}
        return {s: v for s, v in sorted(out.items()) if v > 0}


class Transfer(BaseModel):
    """One paired OUT/IN movement of ``amount`` cents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transfer_id: str
    from_sleeve: str
    to_sleeve: str
    amount: Dec


class TransferResult(BaseModel):
    """Outcome of one step-C transfer attempt.

    ``plan`` is the updated plan to keep pending, or ``None`` when the plan is
    finished (``complete``), dropped as ``incomplete``, or dropped because the
    kill switch is tripped (``dropped_killed``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: RebalancePlan | None
    transfers: list[Transfer]
    events: list[LedgerEvent]
    next_transfer_n: int
    complete: bool = False
    incomplete: bool = False
    dropped_killed: bool = False


# --------------------------------------------------------------------------- helpers


def _cents(d: Decimal) -> int:
    with money_context():
        return int(q2(d).scaleb(2))


def _dec(cents: int) -> Decimal:
    with money_context():
        return Decimal(cents).scaleb(-2)


def _split(total: int, weights: Mapping[str, int]) -> dict[str, int]:
    """Split ``total`` cents pro-rata to integer ``weights`` (largest remainder).

    Sums to exactly ``total``. Remainder cents go to the largest fractional
    parts, ties to the smallest sleeve id.
    """
    wsum = sum(weights.values())
    if total <= 0 or wsum <= 0:
        return {s: 0 for s in weights}
    base = {s: total * w // wsum for s, w in weights.items()}
    frac = {s: total * w % wsum for s, w in weights.items()}
    left = total - sum(base.values())
    for s in sorted(frac, key=lambda k: (-frac[k], k))[:left]:
        base[s] += 1
    return base


def month_key(asof: date) -> str:
    """Persistable calendar-month key, ``"YYYY-MM"``."""
    return f"{asof.year:04d}-{asof.month:02d}"


def transfer_id(n: int) -> str:
    """Deterministic transfer id ``T{n:06d}``."""
    return f"T{n:06d}"


# --------------------------------------------------------------------------- decision


def is_rebalance_day(asof: date, last_rebalance_month: str | None) -> bool:
    """True on the first processed bar of a calendar month not yet rebalanced.

    ``last_rebalance_month`` is the ``month_key`` of the last month in which a
    plan was decided (``None`` if never; then any bar qualifies, so the engine
    should seed it with the first bar's month to skip a no-op day-one plan).
    """
    return last_rebalance_month is None or month_key(asof) != last_rebalance_month


def plan_rebalance(
    snapshot: PortfolioSnapshot,
    config: EngineConfig,
    asof: AsOf,
    kill_switch: KillSwitch | None = None,
) -> RebalancePlan | None:
    """Decide the monthly rebalance at step F; ``None`` while the kill switch is tripped.

    ``target_value_s = q2(target_weight_s * equity)``; ``surplus_s`` is the
    sleeve's snapshot equity minus that (ADR-0001 §5: no plan while tripped).
    """
    if snapshot.killed or (kill_switch is not None and kill_switch.rebalance_frozen):
        return None
    if snapshot.equity <= 0:
        return None
    by_id = {s.sleeve_id: s for s in snapshot.sleeves}
    with money_context():
        targets: dict[str, Decimal] = {}
        surplus: dict[str, Decimal] = {}
        for sc in sorted(config.sleeves, key=lambda c: c.sleeve_id):
            sl = by_id.get(sc.sleeve_id)
            if sl is None:
                raise KeyError(f"sleeve {sc.sleeve_id} missing from snapshot")
            targets[sc.sleeve_id] = q2(sc.target_weight * snapshot.equity)
            surplus[sc.sleeve_id] = sl.equity - targets[sc.sleeve_id]
    return RebalancePlan(asof=asof, equity=snapshot.equity, target_values=targets, surplus=surplus)


def sizing_equity(plan: RebalancePlan | None, sleeve_id: str) -> Decimal | None:
    """The sleeve's ``target_value`` while a plan is pending, else ``None`` (use sleeve equity)."""
    if plan is None:
        return None
    return plan.target_values.get(sleeve_id)


def book_planned(ledger: Ledger, plan: RebalancePlan) -> LedgerEvent:
    """Append the zero-cash ``REBALANCE_PLANNED`` annotation (stage CLOSE)."""
    return ledger.append(
        asof=plan.asof,
        stage=Stage.CLOSE,
        sleeve_id=PORTFOLIO_SLEEVE_ID,
        type=EventType.REBALANCE_PLANNED,
        metadata={
            "equity": str(plan.equity),
            **{f"surplus.{s}": str(v) for s, v in sorted(plan.surplus.items())},
        },
    )


# --------------------------------------------------------------------------- transfers


def execute_transfers(
    plan: RebalancePlan,
    portfolio: Portfolio | Ledger,
    asof: AsOf,
    next_transfer_n: int,
    kill_switch: KillSwitch | None = None,
) -> TransferResult:
    """One step-C attempt: move ``min(surplus, sleeve cash)`` to the underweight sleeves.

    Overweight sleeves give ``min(remaining surplus, cash)``; the pool is capped
    at the total remaining deficit and split pro-rata to deficits (cents,
    largest remainder, ties by sleeve id). Givers and receivers are matched
    greedily in sleeve-id order into paired OUT/IN events sharing a
    ``transfer_id`` (``T{n:06d}``, counter passed in and returned). Cash only.

    After the attempt: complete if nothing remains to give or receive; else
    after ``MAX_TRANSFER_ATTEMPTS`` attempts book ``REBALANCE_INCOMPLETE`` and
    drop the plan; else keep the updated plan for the next bar. A tripped kill
    switch drops the plan without transferring.
    """
    ledger = portfolio.ledger if isinstance(portfolio, Portfolio) else portfolio
    n = next_transfer_n
    if kill_switch is not None and kill_switch.rebalance_frozen:
        return TransferResult(
            plan=None, transfers=[], events=[], next_transfer_n=n, dropped_killed=True
        )

    view = ledger.state_view
    cash = {sid: view.spendable(sid, asof) for sid in view.cash}  # T+1: settled cash only
    for sid in plan.surplus:
        if sid not in cash:
            raise KeyError(f"sleeve {sid} unknown to the ledger")

    give_cap = {
        s: min(_cents(v), max(_cents(cash[s]), 0)) for s, v in plan.remaining_surplus().items()
    }
    need = {s: _cents(v) for s, v in plan.remaining_deficit().items()}
    pool = min(sum(give_cap.values()), sum(need.values()))
    gives = _split(pool, give_cap)
    recvs = _split(pool, need)

    transfers: list[Transfer] = []
    events: list[LedgerEvent] = []
    giver_left = {s: v for s, v in sorted(gives.items()) if v > 0}
    recv_left = {s: v for s, v in sorted(recvs.items()) if v > 0}
    moved_out = dict(plan.moved_out)
    moved_in = dict(plan.moved_in)
    with money_context():
        for g in list(giver_left):
            for r in list(recv_left):
                amt = min(giver_left[g], recv_left[r])
                if amt <= 0:
                    continue
                n += 1
                tid = transfer_id(n)
                a = _dec(amt)
                # One atomic OUT+IN pair (I7).
                events.extend(ledger.append_transfer(asof, Stage.CLOSE, g, r, a, tid))
                transfers.append(Transfer(transfer_id=tid, from_sleeve=g, to_sleeve=r, amount=a))
                moved_out[g] = moved_out.get(g, ZERO) + a
                moved_in[r] = moved_in.get(r, ZERO) + a
                giver_left[g] -= amt
                recv_left[r] -= amt
                if giver_left[g] == 0:
                    break

    updated = plan.model_copy(
        update={"moved_out": moved_out, "moved_in": moved_in, "attempts": plan.attempts + 1}
    )
    rem_give = updated.remaining_surplus()
    rem_need = updated.remaining_deficit()
    if not rem_give or not rem_need:
        return TransferResult(
            plan=None, transfers=transfers, events=events, next_transfer_n=n, complete=True
        )
    if updated.attempts >= MAX_TRANSFER_ATTEMPTS:
        events.append(
            ledger.append(
                asof=asof,
                stage=Stage.CLOSE,
                sleeve_id=PORTFOLIO_SLEEVE_ID,
                type=EventType.REBALANCE_INCOMPLETE,
                metadata={
                    "attempts": str(updated.attempts),
                    "planned_asof": plan.asof.isoformat(),
                    **{f"short_give.{s}": str(v) for s, v in sorted(rem_give.items())},
                    **{f"short_recv.{s}": str(v) for s, v in sorted(rem_need.items())},
                },
            )
        )
        return TransferResult(
            plan=None, transfers=transfers, events=events, next_transfer_n=n, incomplete=True
        )
    return TransferResult(plan=updated, transfers=transfers, events=events, next_transfer_n=n)
