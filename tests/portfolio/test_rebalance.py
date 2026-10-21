"""Monthly rebalance (SPEC-003 section 8, I7, I16; ADR-0001 section 5).

Covers the step-C transfer retry path (REBALANCE_INCOMPLETE), cash-short givers, the attempt
boundary, month seeding, the cents remainder split, and the no-plan-while-killed rule.
Every expected number is worked out by hand in a comment.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tradebot.portfolio.config import default_config
from tradebot.portfolio.ledger import Ledger
from tradebot.portfolio.models import EventType, Stage
from tradebot.portfolio.portfolio import Portfolio
from tradebot.portfolio.rebalance import (
    MAX_TRANSFER_ATTEMPTS,
    RebalancePlan,
    execute_transfers,
    is_rebalance_day,
    month_key,
    plan_rebalance,
)
from tradebot.risk.killswitch import KillSwitch

pytestmark = pytest.mark.unit

D = Decimal
D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
D4, D5 = date(2024, 1, 5), date(2024, 1, 8)
GIVER = "core_momentum"
RECV = "cash"


def _ledger(cash: dict[str, str]) -> Ledger:
    """Ledger with one DEPOSIT per sleeve, so each sleeve has the given cash."""
    led = Ledger()
    for sid, amt in cash.items():
        led.append(asof=D1, stage=Stage.CLOSE, sleeve_id=sid, type=EventType.DEPOSIT, amount=D(amt))
    return led


def _plan(surplus: dict[str, str]) -> RebalancePlan:
    """Plan with the given signed surplus (positive = give, negative = receive). Cents."""
    return RebalancePlan(
        asof=D1,
        equity=D("100000.00"),
        target_values={s: D("0.00") for s in surplus},
        surplus={s: D(v) for s, v in surplus.items()},
    )


def _types(events: list) -> list[EventType]:
    return [e.type for e in events]


# --------------------------------------------------------------------------- transfers


def test_full_transfer_completes_and_books_one_out_in_pair() -> None:
    # Giver surplus 30.00 with 30.00 cash; receiver deficit 30.00. Pool = min(30, 30) = 30.00.
    led = _ledger({GIVER: "30.00", RECV: "5.00"})
    res = execute_transfers(_plan({GIVER: "30.00", RECV: "-30.00"}), led, D2, 1)

    assert res.complete is True
    assert res.incomplete is False
    assert res.plan is None
    assert [(t.from_sleeve, t.to_sleeve, t.amount) for t in res.transfers] == [
        (GIVER, RECV, D("30.00"))
    ]
    assert _types(res.events) == [EventType.CASH_TRANSFER_OUT, EventType.CASH_TRANSFER_IN]
    assert res.next_transfer_n == 2
    assert led.state.cash[GIVER] == D("0.00")
    assert led.state.cash[RECV] == D("35.00")


def test_cash_short_giver_transfers_only_available_cash_and_keeps_plan() -> None:
    # Giver surplus 30.00 but only 10.00 cash. Pool = min(30, 10, need 30) = 10.00.
    # Remaining: giver still owes 20.00, receiver still needs 20.00. Plan stays pending.
    led = _ledger({GIVER: "10.00", RECV: "5.00"})
    res = execute_transfers(_plan({GIVER: "30.00", RECV: "-30.00"}), led, D2, 1)

    assert res.complete is False
    assert res.incomplete is False
    assert res.plan is not None
    assert res.plan.attempts == 1
    assert sum(t.amount for t in res.transfers) == D("10.00")
    assert res.plan.remaining_surplus() == {GIVER: D("20.00")}
    assert res.plan.remaining_deficit() == {RECV: D("20.00")}
    assert led.state.cash[GIVER] == D("0.00")  # never overdrawn


def test_retry_books_rebalance_incomplete_on_the_third_attempt() -> None:
    # MAX_TRANSFER_ATTEMPTS = 3 (first attempt + 2 retries). Giver has 10.00 and the plan needs
    # 30.00. Attempt 1 moves 10.00. Attempts 2 and 3 move nothing (giver cash is 0).
    assert MAX_TRANSFER_ATTEMPTS == 3
    led = _ledger({GIVER: "10.00", RECV: "5.00"})

    r1 = execute_transfers(_plan({GIVER: "30.00", RECV: "-30.00"}), led, D2, 1)
    assert r1.plan is not None and r1.plan.attempts == 1
    assert EventType.REBALANCE_INCOMPLETE not in _types(r1.events)

    r2 = execute_transfers(r1.plan, led, D3, r1.next_transfer_n)
    assert r2.transfers == []
    assert r2.plan is not None and r2.plan.attempts == 2
    assert r2.incomplete is False
    assert EventType.REBALANCE_INCOMPLETE not in _types(r2.events)

    r3 = execute_transfers(r2.plan, led, D4, r2.next_transfer_n)
    assert r3.incomplete is True
    assert r3.complete is False
    assert r3.plan is None  # dropped
    assert r3.transfers == []
    assert _types(r3.events) == [EventType.REBALANCE_INCOMPLETE]

    # Remaining shortfalls: giver 30.00 - 10.00 = 20.00 short, receiver the same.
    md = r3.events[0].metadata
    assert md["attempts"] == "3"
    assert D(md["short_give.core_momentum"]) == D("20.00")
    assert D(md["short_recv.cash"]) == D("20.00")
    assert md["planned_asof"] == D1.isoformat()


def test_transfer_completing_on_the_last_allowed_attempt_is_not_incomplete() -> None:
    # Attempts 1 and 2 move 10.00 and 0.00. A 20.00 deposit before attempt 3 lets it finish.
    # Total moved: 10.00 + 20.00 = 30.00, with no double move.
    led = _ledger({GIVER: "10.00", RECV: "5.00"})
    r1 = execute_transfers(_plan({GIVER: "30.00", RECV: "-30.00"}), led, D2, 1)
    assert r1.plan is not None
    r2 = execute_transfers(r1.plan, led, D3, r1.next_transfer_n)
    assert r2.plan is not None and r2.plan.attempts == 2

    led.append(
        asof=D4, stage=Stage.CLOSE, sleeve_id=GIVER, type=EventType.DEPOSIT, amount=D("20.00")
    )
    r3 = execute_transfers(r2.plan, led, D5, r2.next_transfer_n)

    assert r3.complete is True
    assert r3.incomplete is False
    assert r3.plan is None
    assert EventType.REBALANCE_INCOMPLETE not in _types(r3.events)
    moved = sum(t.amount for t in r1.transfers + r3.transfers)
    assert moved == D("30.00")
    assert led.state.cash[RECV] == D("35.00")  # 5.00 deposit + 30.00 received


def test_cents_remainder_split_sums_exactly_across_receivers() -> None:
    # Giver surplus 10.00, three receivers each needing 10.00. Pool = min(10, 30) = 10.00 = 1000c.
    # Pro-rata split of 1000c over equal needs 1000c each: base 1000*1000//3000 = 333 each,
    # remainder 1000 - 999 = 1c goes to the smallest sleeve id ("cash").
    # Expected receivers: cash 3.34, crypto_trend 3.33, mean_reversion 3.33. Total 10.00.
    led = _ledger(
        {GIVER: "10.00", "cash": "5.00", "crypto_trend": "5.00", "mean_reversion": "5.00"}
    )
    plan = _plan(
        {GIVER: "10.00", "cash": "-10.00", "crypto_trend": "-10.00", "mean_reversion": "-10.00"}
    )
    res = execute_transfers(plan, led, D2, 1)

    received = {t.to_sleeve: t.amount for t in res.transfers}
    assert received == {"cash": D("3.34"), "crypto_trend": D("3.33"), "mean_reversion": D("3.33")}
    assert sum(t.amount for t in res.transfers) == D("10.00")
    assert led.state.cash[GIVER] == D("0.00")


# --------------------------------------------------------------------------- decision and gating


def test_month_seeding_skips_a_day_one_plan() -> None:
    # never rebalanced: any bar qualifies.
    assert is_rebalance_day(D1, None) is True
    # seeded with the first bar's month (Jan 2): no plan again until February.
    assert is_rebalance_day(D1, month_key(D1)) is False
    assert is_rebalance_day(date(2024, 1, 31), "2024-01") is False
    assert is_rebalance_day(date(2024, 2, 1), "2024-01") is True
    assert month_key(date(2024, 2, 1)) == "2024-02"


def test_no_plan_while_killed() -> None:
    cfg = default_config()
    pf = Portfolio(
        _ledger(
            {
                "core_momentum": "50000.00",
                "mean_reversion": "20000.00",
                "crypto_trend": "20000.00",
                "cash": "10000.00",
            }
        )
    )
    snap = pf.snapshot(D2, {}, killed=False)  # equity 100000, all sleeves exactly on target

    # Control: not killed, so a plan is produced (all surpluses are zero).
    assert plan_rebalance(snap, cfg, D2) is not None

    ks = KillSwitch(D("100000.00"))
    ks.observe(D("79000.00"), D2)  # 21% drawdown: trips
    assert ks.killed is True
    assert plan_rebalance(snap, cfg, D2, kill_switch=ks) is None
    assert plan_rebalance(pf.snapshot(D2, {}, killed=True), cfg, D2) is None


def test_killed_switch_drops_pending_plan_without_transferring() -> None:
    led = _ledger({GIVER: "30.00", RECV: "5.00"})
    before = len(led)
    ks = KillSwitch(D("100000.00"))
    ks.observe(D("79000.00"), D2)

    res = execute_transfers(_plan({GIVER: "30.00", RECV: "-30.00"}), led, D2, 1, kill_switch=ks)

    assert res.dropped_killed is True
    assert res.plan is None
    assert res.transfers == []
    assert res.events == []
    assert len(led) == before
    assert led.state.cash[GIVER] == D("30.00")
