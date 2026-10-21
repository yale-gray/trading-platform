"""Append-only ledger and its pure fold (spec 003 §9).

Cash and positions are a deterministic fold of the events. ``Ledger.append``
folds incrementally and validates (I3, no shorts); ``replay`` rebuilds the
same state from a list of events.

All arithmetic runs inside ``money_context()``, so results never depend on the
caller's ambient decimal context. A BUY/SELL fill and its fee are booked
together through :meth:`Ledger.append_fill` (atomic); the low-level
:meth:`Ledger.append` refuses FILL and FEE so a fill can never exist without
its fee.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal

from tradebot.portfolio.models import (
    ANNOTATION_TYPES,
    EventType,
    FillSide,
    LedgerEvent,
    Position,
    Stage,
)
from tradebot.portfolio.money import money_context, q2

ZERO = Decimal("0.00")


class LedgerError(ValueError):
    """An event would break a ledger rule (dense seq, no negative cash, no shorts)."""


class LedgerState:
    """Per-sleeve cash and positions, plus running totals for I6."""

    def __init__(self) -> None:
        self.cash: dict[str, Decimal] = {}
        self.positions: dict[str, dict[str, Position]] = {}
        self.deposits = ZERO
        self.fees = ZERO
        self.realized = ZERO
        self.last_seq = 0
        # (sleeve, symbol) -> seq of the FILL that took the position from flat.
        # Identifies one holding episode; used to expire the stop overlay.
        self.opened_seq: dict[tuple[str, str], int] = {}

    def sell_reduction(self, sleeve_id: str, symbol: str, qty: Decimal) -> tuple[Decimal, Decimal]:
        """Return ``(cost_basis_reduce, remaining_qty)`` for selling ``qty``."""
        pos = self.positions.get(sleeve_id, {}).get(symbol)
        if pos is None or qty > pos.qty:
            raise LedgerError(f"sell {qty} {symbol} exceeds holdings in {sleeve_id}")
        if qty == pos.qty:
            return pos.cost_basis, Decimal(0)
        with money_context():
            return q2(pos.cost_basis * qty / pos.qty), pos.qty - qty

    def complete(self, ev: LedgerEvent) -> LedgerEvent:
        """Fill in ``realized_pnl`` on a SELL fill (or verify it if present)."""
        if ev.type is EventType.FILL and ev.side is FillSide.SELL:
            assert ev.symbol is not None and ev.qty is not None
            reduce, _ = self.sell_reduction(ev.sleeve_id, ev.symbol, ev.qty)
            with money_context():
                realized = ev.amount - reduce
            if ev.realized_pnl is None:
                return ev.model_copy(update={"realized_pnl": realized})
            if ev.realized_pnl != realized:
                raise LedgerError(f"seq {ev.seq}: realized_pnl {ev.realized_pnl} != {realized}")
        return ev

    def apply(self, ev: LedgerEvent) -> None:
        """Fold one event; validates first, mutates only on success."""
        if ev.seq != self.last_seq + 1:
            raise LedgerError(f"seq must be dense: expected {self.last_seq + 1}, got {ev.seq}")
        with money_context():
            ev = self.complete(ev)
            sid = ev.sleeve_id
            if ev.type is EventType.DEPOSIT:
                self.cash[sid] = self.cash.get(sid, ZERO) + ev.amount
                self.deposits += ev.amount
            elif ev.type not in ANNOTATION_TYPES:
                self._apply_cash(ev)
            self.last_seq = ev.seq

    def _apply_cash(self, ev: LedgerEvent) -> None:
        sid = ev.sleeve_id
        cash = self.cash.get(sid)
        if cash is None:
            raise LedgerError(f"unknown sleeve {sid} (no DEPOSIT yet)")
        if ev.type is EventType.CASH_TRANSFER_IN:
            self.cash[sid] = cash + ev.amount
            return
        if ev.type is EventType.FILL and ev.side is FillSide.SELL:
            assert ev.symbol is not None and ev.qty is not None
            reduce, left = self.sell_reduction(sid, ev.symbol, ev.qty)
            book = self.positions[sid]
            if left == 0:
                del book[ev.symbol]
                self.opened_seq.pop((sid, ev.symbol), None)
            else:
                p = book[ev.symbol]
                book[ev.symbol] = p.model_copy(
                    update={"qty": left, "cost_basis": p.cost_basis - reduce}
                )
            self.cash[sid] = cash + ev.amount
            self.realized += ev.amount - reduce
            return
        # Cash-out events: BUY fill, FEE, transfer out.
        if ev.type is EventType.FILL and ev.amount <= 0:
            raise LedgerError(f"seq {ev.seq}: zero-notional BUY fill rejected")
        if ev.amount > cash:
            raise LedgerError(f"seq {ev.seq}: cash {cash} in {sid} cannot cover {ev.amount} (I3)")
        self.cash[sid] = cash - ev.amount
        if ev.type is EventType.FEE:
            self.fees += ev.amount
        elif ev.type is EventType.FILL:
            assert ev.symbol is not None and ev.qty is not None
            book = self.positions.setdefault(sid, {})
            p = book.get(ev.symbol)
            if p is None:
                book[ev.symbol] = Position(
                    symbol=ev.symbol, qty=ev.qty, cost_basis=ev.amount, entry_asof=ev.asof
                )
                self.opened_seq[(sid, ev.symbol)] = ev.seq
            else:
                book[ev.symbol] = p.model_copy(
                    update={"qty": p.qty + ev.qty, "cost_basis": p.cost_basis + ev.amount}
                )

    def copy(self) -> LedgerState:
        """Independent copy (positions are frozen models, so shallow dict copies suffice)."""
        new = LedgerState()
        new.cash = dict(self.cash)
        new.positions = {s: dict(b) for s, b in self.positions.items()}
        new.deposits, new.fees, new.realized = self.deposits, self.fees, self.realized
        new.last_seq = self.last_seq
        new.opened_seq = dict(self.opened_seq)
        return new


def replay(events: Iterable[LedgerEvent]) -> LedgerState:
    """Fold events (in order) into per-sleeve cash and positions."""
    state = LedgerState()
    for ev in events:
        state.apply(ev)
    return state


class Ledger:
    """Append-only event list with an incrementally maintained fold."""

    def __init__(self, events: Iterable[LedgerEvent] = ()) -> None:
        self._events: list[LedgerEvent] = []
        self._state = LedgerState()
        for ev in events:
            self._commit(ev)

    @property
    def events(self) -> Sequence[LedgerEvent]:
        """Read-only view (a tuple copy; events themselves are frozen)."""
        return tuple(self._events)

    @property
    def state(self) -> LedgerState:
        """A copy of the current folded state."""
        return self._state.copy()

    @property
    def next_seq(self) -> int:
        return len(self._events) + 1

    def __len__(self) -> int:
        return len(self._events)

    def _commit(self, ev: LedgerEvent) -> LedgerEvent:
        ev = self._state.complete(ev) if ev.seq == self._state.last_seq + 1 else ev
        self._state.apply(ev)
        self._events.append(ev)
        return ev

    def _commit_many(self, evs: Sequence[LedgerEvent]) -> list[LedgerEvent]:
        """Validate ``evs`` on a state copy; commit all or none."""
        trial = self._state.copy()
        done: list[LedgerEvent] = []
        for ev in evs:
            ev = trial.complete(ev) if ev.seq == trial.last_seq + 1 else ev
            trial.apply(ev)
            done.append(ev)
        self._state = trial
        self._events.extend(done)
        return done

    def append(
        self,
        *,
        asof: date,
        stage: Stage,
        sleeve_id: str,
        type: EventType,
        **fields: object,
    ) -> LedgerEvent:
        """Create the next event (dense ``seq``), validate and record it.

        FILL and FEE events are refused here: use :meth:`append_fill`, which books
        a fill and its fee atomically.
        """
        if type in (EventType.FILL, EventType.FEE):
            raise LedgerError(f"{type} must be booked through append_fill (atomic fill + fee)")
        ev = LedgerEvent(
            seq=self.next_seq,
            asof=asof,
            stage=stage,
            sleeve_id=sleeve_id,
            type=type,
            **fields,  # type: ignore[arg-type]
        )
        return self._commit(ev)

    def append_fill(
        self,
        *,
        asof: date,
        stage: Stage,
        sleeve_id: str,
        symbol: str,
        side: FillSide,
        qty: Decimal,
        price: Decimal,
        order_id: str,
        fee: Decimal,
        **fields: object,
    ) -> list[LedgerEvent]:
        """Book a BUY/SELL FILL and its FEE as one atomic step.

        ``amount`` is derived as ``q2(qty * price)``. Both events are validated
        against a copy of the state (BUY: ``notional + fee`` must fit in cash;
        SELL: oversell and a fee larger than cash-after-proceeds are rejected);
        nothing is recorded unless both pass. A zero ``fee`` records no FEE
        event. Extra ``fields`` (``slippage_cost``, ``metadata``, ...) go on the
        FILL. Returns the recorded events (FILL first).
        """
        with money_context():
            amount = q2(qty * price)
        fill = LedgerEvent(
            seq=self.next_seq,
            asof=asof,
            stage=stage,
            sleeve_id=sleeve_id,
            type=EventType.FILL,
            amount=amount,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            order_id=order_id,
            **fields,  # type: ignore[arg-type]
        )
        evs = [fill]
        if fee != 0:
            evs.append(
                LedgerEvent(
                    seq=self.next_seq + 1,
                    asof=asof,
                    stage=stage,
                    sleeve_id=sleeve_id,
                    type=EventType.FEE,
                    amount=fee,
                    symbol=symbol,
                    order_id=order_id,
                )
            )
        return self._commit_many(evs)
