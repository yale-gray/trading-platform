"""Append-only ledger and its pure fold (spec 003 §9).

Cash and positions are a deterministic fold of the events. ``Ledger.append``
folds incrementally and validates (I3, no shorts); ``replay`` rebuilds the
same state from a list of events.

All arithmetic runs inside ``money_context()``, so results never depend on the
caller's ambient decimal context. A BUY/SELL fill and its fee are booked
together through :meth:`Ledger.append_fill` (atomic), and a cash transfer's OUT
and IN legs through :meth:`Ledger.append_transfer` (atomic); the low-level
:meth:`Ledger.append` refuses FILL and FEE so a fill can never exist without
its fee.

Rules enforced by the fold (so also on a hand-made event list):

* a FILL's ``order_id`` is unique (I14); a FEE must follow a FILL of the same
  order and sleeve, at most one FEE per order;
* equity fill quantities lie on the configured equity grid (``equity_qty_quantum``;
  whole shares when 1, else any grid up to 8 dp when unset); crypto quantities have at most 8 dp
  (the 8 dp / 1e-8 price grid for every fill is enforced by ``LedgerEvent``);
* a DEPOSIT is initial funding only: it must precede every FILL in its sleeve;
* a SELL whose proceeds round to 0.00 is rejected unless it is a full exit of
  the position (a dust position may be closed at zero proceeds; a partial
  zero-proceeds SELL would only erase basis) (R3);
* each ``transfer_id`` has at most one OUT and one IN leg, with equal amounts in
  different sleeves; unpaired legs are reported by :meth:`LedgerState.unpaired_transfers`
  and rejected when a :class:`Ledger` is built from an event list (I7).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal

from tradebot.data.symbols import asset_class
from tradebot.data.types import AssetClass
from tradebot.portfolio.models import (
    ANNOTATION_TYPES,
    EventType,
    FillSide,
    LedgerEvent,
    Position,
    Stage,
    on_grid,
)
from tradebot.portfolio.money import QTY8_Q, money_context, q2

ZERO = Decimal("0.00")


class LedgerError(ValueError):
    """An event would break a ledger rule (dense seq, no negative cash, no shorts)."""


class LedgerState:
    """Per-sleeve cash and positions, plus running totals for I6."""

    def __init__(self, equity_qty_quantum: Decimal | None = None) -> None:
        # None: equity quantities need only be on the 8 dp grid (no config known).
        self.equity_qty_quantum = equity_qty_quantum
        self.cash: dict[str, Decimal] = {}
        self.positions: dict[str, dict[str, Position]] = {}
        self.deposits = ZERO
        self.fees = ZERO
        self.realized = ZERO
        self.last_seq = 0
        # (sleeve, symbol) -> seq of the FILL that took the position from flat.
        # Identifies one holding episode; used to expire the stop overlay.
        self.opened_seq: dict[tuple[str, str], int] = {}
        # order_id -> sleeve of its FILL (I14); order_id -> booked FEE amount.
        self.fill_orders: dict[str, str] = {}
        self.fee_by_order: dict[str, Decimal] = {}
        # Sleeves that already have a FILL (DEPOSIT is initial funding only).
        self.filled_sleeves: set[str] = set()
        # transfer_id -> {"OUT"|"IN": (sleeve, amount)}; I7 pairing.
        self.transfers: dict[str, dict[str, tuple[str, Decimal]]] = {}

    def unpaired_transfers(self) -> list[str]:
        """``transfer_id``s that lack an OUT or an IN leg (I7), sorted."""
        return sorted(t for t, legs in self.transfers.items() if len(legs) != 2)

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
                if sid in self.filled_sleeves:
                    raise LedgerError(
                        f"seq {ev.seq}: DEPOSIT into {sid} after a FILL (initial funding only)"
                    )
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
        if ev.type in (EventType.CASH_TRANSFER_IN, EventType.CASH_TRANSFER_OUT):
            self._check_transfer_leg(ev)
        if ev.type is EventType.CASH_TRANSFER_IN:
            self.cash[sid] = cash + ev.amount
            self._record_transfer_leg(ev)
            return
        if ev.type is EventType.FEE:
            self._check_fee_link(ev)
        if ev.type is EventType.FILL:
            self._check_fill(ev)
        if ev.type is EventType.FILL and ev.side is FillSide.SELL:
            assert ev.symbol is not None and ev.qty is not None
            reduce, left = self.sell_reduction(sid, ev.symbol, ev.qty)
            if ev.amount == 0 and left != 0:
                raise LedgerError(
                    f"seq {ev.seq}: SELL of {ev.qty} {ev.symbol} has zero proceeds but is not "
                    "a full exit (R3)"
                )
            self._record_fill(ev)
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
        if ev.type is EventType.CASH_TRANSFER_OUT:
            self._record_transfer_leg(ev)
        elif ev.type is EventType.FEE:
            self.fees += ev.amount
            assert ev.order_id is not None
            self.fee_by_order[ev.order_id] = ev.amount
        elif ev.type is EventType.FILL:
            assert ev.symbol is not None and ev.qty is not None
            self._record_fill(ev)
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

    def _check_fill(self, ev: LedgerEvent) -> None:
        assert ev.order_id is not None and ev.symbol is not None and ev.qty is not None
        if ev.order_id in self.fill_orders:
            raise LedgerError(f"seq {ev.seq}: duplicate FILL for order {ev.order_id} (I14)")
        check_qty_grid(ev.symbol, ev.qty, f"seq {ev.seq}", self.equity_qty_quantum)

    def _record_fill(self, ev: LedgerEvent) -> None:
        assert ev.order_id is not None
        self.fill_orders[ev.order_id] = ev.sleeve_id
        self.filled_sleeves.add(ev.sleeve_id)

    def _check_fee_link(self, ev: LedgerEvent) -> None:
        assert ev.order_id is not None
        owner = self.fill_orders.get(ev.order_id)
        if owner is None:
            raise LedgerError(f"seq {ev.seq}: FEE for order {ev.order_id} has no FILL")
        if owner != ev.sleeve_id:
            raise LedgerError(f"seq {ev.seq}: FEE sleeve {ev.sleeve_id} != FILL sleeve {owner}")
        if ev.order_id in self.fee_by_order:
            raise LedgerError(f"seq {ev.seq}: second FEE for order {ev.order_id}")

    def _check_transfer_leg(self, ev: LedgerEvent) -> None:
        assert ev.transfer_id is not None
        leg = "OUT" if ev.type is EventType.CASH_TRANSFER_OUT else "IN"
        other = "IN" if leg == "OUT" else "OUT"
        legs = self.transfers.get(ev.transfer_id, {})
        if leg in legs:
            raise LedgerError(f"seq {ev.seq}: transfer {ev.transfer_id} already has an {leg} leg")
        if other in legs:
            o_sleeve, o_amount = legs[other]
            if o_amount != ev.amount:
                raise LedgerError(
                    f"seq {ev.seq}: transfer {ev.transfer_id} legs differ "
                    f"({o_amount} vs {ev.amount}) (I7)"
                )
            if o_sleeve == ev.sleeve_id:
                raise LedgerError(f"seq {ev.seq}: transfer {ev.transfer_id} stays in one sleeve")

    def _record_transfer_leg(self, ev: LedgerEvent) -> None:
        assert ev.transfer_id is not None
        leg = "OUT" if ev.type is EventType.CASH_TRANSFER_OUT else "IN"
        self.transfers.setdefault(ev.transfer_id, {})[leg] = (ev.sleeve_id, ev.amount)

    def checkpoint(self) -> LedgerState:
        """Cheap rollback point: copies the small maps, shares nothing mutable.

        The per-order and per-transfer maps grow with the ledger and are NOT
        copied; :meth:`rollback` undoes their additions from the events applied
        since the checkpoint.
        """
        new = LedgerState.__new__(LedgerState)
        new.cash = dict(self.cash)
        new.positions = {s: dict(b) for s, b in self.positions.items()}
        new.deposits, new.fees, new.realized = self.deposits, self.fees, self.realized
        new.last_seq = self.last_seq
        new.opened_seq = dict(self.opened_seq)
        new.filled_sleeves = set(self.filled_sleeves)
        return new

    def rollback(self, point: LedgerState, applied: Sequence[LedgerEvent]) -> None:
        """Restore the state saved by :meth:`checkpoint` after ``applied`` events."""
        self.cash, self.positions = point.cash, point.positions
        self.deposits, self.fees, self.realized = point.deposits, point.fees, point.realized
        self.last_seq, self.opened_seq = point.last_seq, point.opened_seq
        self.filled_sleeves = point.filled_sleeves
        for ev in applied:
            if ev.type is EventType.FILL and ev.order_id is not None:
                self.fill_orders.pop(ev.order_id, None)
            elif ev.type is EventType.FEE and ev.order_id is not None:
                self.fee_by_order.pop(ev.order_id, None)
            elif ev.transfer_id is not None:
                leg = "OUT" if ev.type is EventType.CASH_TRANSFER_OUT else "IN"
                legs = self.transfers.get(ev.transfer_id)
                if legs is not None:
                    legs.pop(leg, None)
                    if not legs:
                        del self.transfers[ev.transfer_id]

    def copy(self) -> LedgerState:
        """Independent copy (positions are frozen models, so shallow dict copies suffice)."""
        new = LedgerState(self.equity_qty_quantum)
        new.cash = dict(self.cash)
        new.positions = {s: dict(b) for s, b in self.positions.items()}
        new.deposits, new.fees, new.realized = self.deposits, self.fees, self.realized
        new.last_seq = self.last_seq
        new.opened_seq = dict(self.opened_seq)
        new.fill_orders = dict(self.fill_orders)
        new.fee_by_order = dict(self.fee_by_order)
        new.filled_sleeves = set(self.filled_sleeves)
        new.transfers = {t: dict(legs) for t, legs in self.transfers.items()}
        return new


def check_qty_grid(
    symbol: str, qty: Decimal, where: str = "", equity_quantum: Decimal | None = None
) -> None:
    """I4: equity qty on the ``equity_quantum`` grid, crypto at most 8 dp (``qty > 0``).

    ``equity_quantum=None`` means no config is known: equity needs only the 8 dp grid.
    """
    if qty <= 0:
        raise LedgerError(f"{where}: quantity {qty} of {symbol} must be > 0")
    if (
        equity_quantum is not None
        and asset_class(symbol) is AssetClass.EQUITY
        and not on_grid(qty, equity_quantum)
    ):
        raise LedgerError(
            f"{where}: equity {symbol} quantity {qty} is off the {equity_quantum} grid (I4)"
        )
    if not on_grid(qty, QTY8_Q):
        raise LedgerError(f"{where}: {symbol} quantity {qty} has more than 8 decimals (I4)")


def replay(
    events: Iterable[LedgerEvent], equity_qty_quantum: Decimal | None = None
) -> LedgerState:
    """Fold events (in order) into per-sleeve cash and positions."""
    state = LedgerState(equity_qty_quantum)
    for ev in events:
        state.apply(ev)
    return state


class Ledger:
    """Append-only event list with an incrementally maintained fold."""

    def __init__(
        self,
        events: Iterable[LedgerEvent] = (),
        *,
        expected_fee: Callable[[LedgerEvent], Decimal] | None = None,
        equity_qty_quantum: Decimal | None = None,
    ) -> None:
        """Build a ledger by folding ``events`` (dense ``seq`` from 1).

        Besides the per-event rules, the finished list must have every
        ``transfer_id`` paired (OUT and IN, I7). If ``expected_fee`` (FILL ->
        fee by cost config) is given, every FILL must also carry exactly that
        fee as a FEE event (R1); without cost context a FILL with no FEE cannot
        be told from a zero-fee fill, but a FEE with no FILL is always rejected.
        """
        self._events: list[LedgerEvent] = []
        self.equity_qty_quantum = equity_qty_quantum
        self._state = LedgerState(equity_qty_quantum)
        for ev in events:
            self._commit(ev)
        bad = self._state.unpaired_transfers()
        if bad:
            raise LedgerError(f"unpaired transfer legs (I7): {bad}")
        if expected_fee is not None:
            for ev in self._events:
                if ev.type is EventType.FILL:
                    assert ev.order_id is not None
                    want = expected_fee(ev)
                    got = self._state.fee_by_order.get(ev.order_id, ZERO)
                    if want != got:
                        raise LedgerError(
                            f"FILL {ev.order_id} (seq {ev.seq}): expected fee {want}, booked {got}"
                        )

    @property
    def state_view(self) -> LedgerState:
        """The live folded state WITHOUT copying. Read-only by convention: never mutate it."""
        return self._state

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
        """Apply ``evs`` in order; on any failure roll the state back (all or none)."""
        state = self._state
        point = state.checkpoint()
        done: list[LedgerEvent] = []
        try:
            for ev in evs:
                ev = state.complete(ev) if ev.seq == state.last_seq + 1 else ev
                state.apply(ev)
                done.append(ev)
        except Exception:
            state.rollback(point, done)
            raise
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

    def append_transfer(
        self,
        asof: date,
        stage: Stage,
        from_sleeve: str,
        to_sleeve: str,
        amount: Decimal,
        transfer_id: str,
        metadata: Mapping[str, str] | None = None,
    ) -> list[LedgerEvent]:
        """Book a cash transfer as one atomic OUT + IN pair (I7).

        Both legs are validated (``amount > 0`` on the cent grid, distinct and
        known sleeves, the giver's cash covers ``amount``, ``transfer_id`` unused)
        before either is recorded. Each leg gets ``{"to": ...}`` / ``{"from": ...}``
        metadata merged with ``metadata``. Returns ``[OUT, IN]``.
        """
        if amount <= 0:
            raise LedgerError(f"transfer {transfer_id}: amount must be > 0, got {amount}")
        if from_sleeve == to_sleeve:
            raise LedgerError(f"transfer {transfer_id}: from and to are both {from_sleeve}")
        extra = dict(metadata or {})
        common = {"asof": asof, "stage": stage, "amount": amount, "transfer_id": transfer_id}
        out = LedgerEvent(
            seq=self.next_seq,
            sleeve_id=from_sleeve,
            type=EventType.CASH_TRANSFER_OUT,
            metadata={"to": to_sleeve, **extra},
            **common,  # type: ignore[arg-type]
        )
        inn = LedgerEvent(
            seq=self.next_seq + 1,
            sleeve_id=to_sleeve,
            type=EventType.CASH_TRANSFER_IN,
            metadata={"from": from_sleeve, **extra},
            **common,  # type: ignore[arg-type]
        )
        return self._commit_many([out, inn])
