"""Portfolio-wide drawdown kill switch (SPEC-003 §7, I11/I12, ADR D8).

Trips when ``1 - equity/peak >= threshold`` on an end-of-day snapshot. Peak
starts at initial capital and only rises, except on re-arm / manual
:meth:`reset`. While killed: BUYs are blocked, exits allowed, no
auto-liquidation, and rebalance is frozen (a pending ``RebalancePlan`` is
dropped on trip; the caller acts on ``KillObservation.tripped_now``).

ADR-0001 A18 (pause and re-arm): the pause lasts ``pause_bars`` processed bars
after the trip bar; on the ``pause_bars``-th later bar the switch re-arms
automatically with ``peak = that bar's equity`` (``KillObservation.rearmed_now``).
A manual reset (CLI) ends the pause early.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from tradebot.portfolio.asof import AsOf, asof_from_iso
from tradebot.portfolio.money import from_text, money_context, q_ratio, to_text

DEFAULT_THRESHOLD = Decimal("0.20")
DEFAULT_PAUSE_BARS = 20


@dataclass(frozen=True)
class KillObservation:
    """Result of observing one end-of-day equity."""

    drawdown: Decimal
    peak_equity: Decimal
    tripped_now: bool  # True only on the transition; caller drops the pending plan
    killed: bool
    rearmed_now: bool = False  # True only on the auto re-arm transition


class KillSwitch:
    """Drawdown kill switch with JSON-serializable state."""

    def __init__(
        self,
        initial_capital: Decimal,
        threshold: Decimal = DEFAULT_THRESHOLD,
        pause_bars: int = DEFAULT_PAUSE_BARS,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be > 0")
        if pause_bars < 1:
            raise ValueError("pause_bars must be >= 1")
        self.threshold = threshold
        self.pause_bars = pause_bars
        self.bars_paused = 0  # processed bars since the trip bar (0 on the trip bar)
        self.peak_equity = initial_capital
        self.last_equity = initial_capital
        self.killed = False
        self.tripped_asof: AsOf | None = None
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

    def observe(self, equity: Decimal, asof: AsOf) -> KillObservation:
        """Process an end-of-day equity: raise peak, test the trip threshold.

        The test is exact: ``1 - equity/peak >= t`` iff ``equity <= peak*(1-t)``.
        While killed, the bar only counts toward the pause; on the
        ``pause_bars``-th bar after the trip the switch re-arms with
        ``peak = equity`` (no trip test on the re-arm bar).
        """
        with money_context():
            if self.killed:
                self.last_equity = equity
                self.bars_paused += 1
                if self.bars_paused >= self.pause_bars:
                    self.peak_equity = equity
                    self.killed = False
                    self.tripped_asof = None
                    self.bars_paused = 0
                    return KillObservation(
                        Decimal("0E-10"), self.peak_equity, False, False, rearmed_now=True
                    )
                dd = Decimal(1) - equity / self.peak_equity
                return KillObservation(
                    q_ratio(dd) if dd >= 0 else Decimal("0E-10"), self.peak_equity, False, True
                )
            if equity > self.peak_equity:
                self.peak_equity = equity
            self.last_equity = equity
            dd = Decimal(1) - equity / self.peak_equity
            trip = equity <= self.peak_equity * (Decimal(1) - self.threshold)
            tripped_now = trip and not self.killed
            if tripped_now:
                self.killed = True
                self.tripped_asof = asof
                self.bars_paused = 0
            dd_q = q_ratio(dd) if dd >= 0 else Decimal("0E-10")
        return KillObservation(dd_q, self.peak_equity, tripped_now, self.killed)

    def reset(self, operator: str, note: str, asof: AsOf) -> None:
        """Manual reset (CLI only): clear the flag, peak = last observed equity."""
        if not self.killed:
            raise ValueError("kill switch not tripped; nothing to reset")
        with money_context():
            self.peak_equity = self.last_equity.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        self.killed = False
        self.tripped_asof = None
        self.bars_paused = 0
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
            "pause_bars": self.pause_bars,
            "bars_paused": self.bars_paused,
            "peak_equity": to_text(self.peak_equity),
            "last_equity": to_text(self.last_equity),
            "killed": self.killed,
            "tripped_asof": None if self.tripped_asof is None else self.tripped_asof.isoformat(),
            "resets": list(self.resets),
        }

    @classmethod
    def from_state(
        cls, state: dict[str, Any], default_pause_bars: int = DEFAULT_PAUSE_BARS
    ) -> KillSwitch:
        """Inverse of :meth:`to_state`. States saved before A18 lack the pause fields."""
        ks = cls(
            from_text(state["peak_equity"]),
            from_text(state["threshold"]),
            int(state.get("pause_bars", default_pause_bars)),
        )
        ks.bars_paused = int(state.get("bars_paused", 0))
        ks.last_equity = from_text(state["last_equity"])
        ks.killed = bool(state["killed"])
        ta = state["tripped_asof"]
        ks.tripped_asof = None if ta is None else asof_from_iso(ta)
        ks.resets = [dict(r) for r in state["resets"]]
        return ks
