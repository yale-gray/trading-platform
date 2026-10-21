"""Portfolio-wide drawdown kill switch (SPEC-003 §7, I11/I12, ADR D8).

Trips when ``1 - equity/peak >= threshold`` on an end-of-day snapshot. Peak
starts at initial capital and only rises, except on manual :meth:`reset`.
While killed: BUYs are blocked, exits allowed, no auto-liquidation, and
rebalance is frozen (a pending ``RebalancePlan`` is dropped on trip; the
caller acts on ``KillObservation.tripped_now``). Reset is reached via CLI only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from tradebot.portfolio.money import from_text, money_context, q_ratio, to_text

DEFAULT_THRESHOLD = Decimal("0.20")


@dataclass(frozen=True)
class KillObservation:
    """Result of observing one end-of-day equity."""

    drawdown: Decimal
    peak_equity: Decimal
    tripped_now: bool  # True only on the transition; caller drops the pending plan
    killed: bool


class KillSwitch:
    """Drawdown kill switch with JSON-serializable state."""

    def __init__(self, initial_capital: Decimal, threshold: Decimal = DEFAULT_THRESHOLD) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be > 0")
        self.threshold = threshold
        self.peak_equity = initial_capital
        self.last_equity = initial_capital
        self.killed = False
        self.tripped_asof: date | None = None
        self.resets: list[dict[str, str]] = []

    @property
    def rebalance_frozen(self) -> bool:
        """No new RebalancePlan while tripped."""
        return self.killed

    def allows_buy(self) -> bool:
        """R0: BUYs are rejected while killed (SELLs and STOP exits always allowed)."""
        return not self.killed

    def drawdown(self, equity: Decimal) -> Decimal:
        """``1 - equity/peak`` quantized to RATIO_Q (informational)."""
        with money_context():
            return q_ratio(Decimal(1) - equity / self.peak_equity)

    def observe(self, equity: Decimal, asof: date) -> KillObservation:
        """Process an end-of-day equity: raise peak, test the trip threshold.

        The test is exact: ``1 - equity/peak >= t`` iff ``equity <= peak*(1-t)``.
        """
        with money_context():
            if equity > self.peak_equity:
                self.peak_equity = equity
            self.last_equity = equity
            dd = Decimal(1) - equity / self.peak_equity
            trip = equity <= self.peak_equity * (Decimal(1) - self.threshold)
            tripped_now = trip and not self.killed
            if tripped_now:
                self.killed = True
                self.tripped_asof = asof
            dd_q = q_ratio(dd) if dd >= 0 else Decimal("0E-10")
        return KillObservation(dd_q, self.peak_equity, tripped_now, self.killed)

    def reset(self, operator: str, note: str, asof: date) -> None:
        """Manual reset (CLI only): clear the flag, peak = last observed equity."""
        if not self.killed:
            raise ValueError("kill switch not tripped; nothing to reset")
        with money_context():
            self.peak_equity = self.last_equity.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        self.killed = False
        self.tripped_asof = None
        self.resets.append(
            {
                "operator": operator,
                "note": note,
                "asof": asof.isoformat(),
                "peak_equity": to_text(self.peak_equity),
            }
        )

    def to_state(self) -> dict[str, Any]:
        """JSON-serializable state (Decimals as text)."""
        return {
            "threshold": to_text(self.threshold),
            "peak_equity": to_text(self.peak_equity),
            "last_equity": to_text(self.last_equity),
            "killed": self.killed,
            "tripped_asof": None if self.tripped_asof is None else self.tripped_asof.isoformat(),
            "resets": list(self.resets),
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> KillSwitch:
        """Inverse of :meth:`to_state`."""
        ks = cls(from_text(state["peak_equity"]), from_text(state["threshold"]))
        ks.last_equity = from_text(state["last_equity"])
        ks.killed = bool(state["killed"])
        ta = state["tripped_asof"]
        ks.tripped_asof = None if ta is None else date.fromisoformat(ta)
        ks.resets = [dict(r) for r in state["resets"]]
        return ks
