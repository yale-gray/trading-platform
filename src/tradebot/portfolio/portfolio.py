"""Sleeve/portfolio accounting over the ledger fold, and equity snapshots (spec 003 §9)."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date
from decimal import Decimal

from tradebot.portfolio.ledger import (
    ZERO,
    Ledger,
    LedgerError,
    LedgerState,
    check_qty_grid,
    replay,
)
from tradebot.portfolio.models import (
    PortfolioSnapshot,
    Position,
    PositionSnapshot,
    SleeveSnapshot,
)
from tradebot.portfolio.money import money_context, q2, q_ratio, q_weight


class Portfolio:
    """Sleeves (cash + positions) derived from a :class:`Ledger`.

    ``high_water_close`` / ``stop_level`` are carried on positions as overlay
    state (Wave 3 maintains them); cash and quantities come only from the fold.
    Each overlay entry is tagged with the fold's ``opened_seq`` for that
    position, so it applies only to the holding episode it was set for: once the
    position goes flat, the entry is dead and a re-entry starts fresh (SPEC-003
    §7), however the ledger was appended to.
    """

    def __init__(self, ledger: Ledger | None = None) -> None:
        self.ledger = ledger if ledger is not None else Ledger()
        # (sleeve, symbol) -> (opened_seq, high_water_close, stop_level)
        self._overlay: dict[tuple[str, str], tuple[int, Decimal | None, Decimal | None]] = {}

    @property
    def sleeve_ids(self) -> list[str]:
        return sorted(self.ledger.state_view.cash)

    def cash(self, sleeve_id: str) -> Decimal:
        return self.ledger.state_view.cash[sleeve_id]

    def unsettled(self, sleeve_id: str, asof: date) -> Decimal:
        """Part of the sleeve's cash that cannot fund a BUY at ``asof`` (T+1 accounts only)."""
        return self.ledger.state_view.unsettled(sleeve_id, asof)

    def settled_cash(self, sleeve_id: str, asof: date) -> Decimal:
        """Cash the sleeve can spend on BUYs or transfers at ``asof``."""
        return self.ledger.state_view.spendable(sleeve_id, asof)

    def positions(self, sleeve_id: str) -> dict[str, Position]:
        """Positions of a sleeve with the stop overlay applied."""
        out: dict[str, Position] = {}
        state = self.ledger.state_view
        for sym, p in sorted(state.positions.get(sleeve_id, {}).items()):
            hw: Decimal | None = None
            stop: Decimal | None = None
            entry = self._overlay.get((sleeve_id, sym))
            if entry is not None:
                if entry[0] == state.opened_seq[(sleeve_id, sym)]:
                    _, hw, stop = entry
                else:
                    del self._overlay[(sleeve_id, sym)]
            out[sym] = p.model_copy(update={"high_water_close": hw, "stop_level": stop})
        return out

    def set_stop_fields(
        self,
        sleeve_id: str,
        symbol: str,
        high_water_close: Decimal | None,
        stop_level: Decimal | None,
    ) -> None:
        """Record trailing-stop state for an open position (dropped when the position goes flat)."""
        state = self.ledger.state_view
        if symbol not in state.positions.get(sleeve_id, {}):
            raise LedgerError(f"no open position {symbol} in {sleeve_id}")
        self._overlay[(sleeve_id, symbol)] = (
            state.opened_seq[(sleeve_id, symbol)],
            high_water_close,
            stop_level,
        )

    def snapshot(
        self,
        asof: date,
        closes: Mapping[str, Decimal],
        *,
        peak_equity: Decimal | None = None,
        killed: bool = False,
        crypto_symbols: Collection[str] = (),
    ) -> PortfolioSnapshot:
        """Mark every position at ``closes`` and compute the equity snapshot."""
        with money_context():
            return self._snapshot(asof, closes, peak_equity, killed, crypto_symbols)

    def _snapshot(
        self,
        asof: date,
        closes: Mapping[str, Decimal],
        peak_equity: Decimal | None,
        killed: bool,
        crypto_symbols: Collection[str],
    ) -> PortfolioSnapshot:
        state = self.ledger.state_view
        uns = {sid: state.unsettled(sid, asof) for sid in state.cash} if state.t1 else {}
        marked: list[tuple[str, Decimal, list[PositionSnapshot]]] = []
        for sid in sorted(state.cash):
            pss: list[PositionSnapshot] = []
            for sym, p in self.positions(sid).items():
                if sym not in closes:
                    raise LedgerError(f"no close to mark {sym}")
                close = closes[sym]
                if not isinstance(close, Decimal) or not close.is_finite() or close <= 0:
                    raise LedgerError(
                        f"close for held symbol {sym} must be a Decimal > 0, got {close!r}"
                    )
                mv = q2(p.qty * close)
                pss.append(
                    PositionSnapshot(
                        symbol=sym,
                        qty=p.qty,
                        mv=mv,
                        cost_basis=p.cost_basis,
                        unrealized=mv - p.cost_basis,
                        entry_asof=p.entry_asof,
                        high_water_close=p.high_water_close,
                        stop_level=p.stop_level,
                    )
                )
            eq = state.cash[sid] + sum((x.mv for x in pss), ZERO)
            marked.append((sid, eq, pss))
        equity = sum((eq for _, eq, _ in marked), ZERO)
        sleeves = [
            SleeveSnapshot(
                sleeve_id=sid,
                cash=state.cash[sid],
                equity=eq,
                weight=q_weight(eq / equity) if equity > 0 else Decimal("0.000000"),
                positions=pss,
                unsettled=uns.get(sid, ZERO),
            )
            for sid, eq, pss in marked
        ]
        by_symbol: dict[str, Decimal] = {}
        for _, _, pss in marked:
            for x in pss:
                by_symbol[x.symbol] = by_symbol.get(x.symbol, ZERO) + x.mv
        peak = max(peak_equity if peak_equity is not None else equity, equity)
        crypto = set(crypto_symbols)

        def frac(v: Decimal) -> Decimal:
            return q_weight(v / equity) if equity > 0 else Decimal("0.000000")

        return PortfolioSnapshot(
            asof=asof,
            equity=equity,
            cash=sum(state.cash.values(), ZERO),
            unsettled=sum(uns.values(), ZERO),
            drawdown=q_ratio(1 - equity / peak) if peak > 0 else q_ratio(Decimal(0)),
            peak_equity=peak,
            killed=killed,
            last_seq=state.last_seq,
            sleeves=sleeves,
            exposures={s: frac(v) for s, v in sorted(by_symbol.items())},
            crypto_fraction=frac(sum((v for s, v in by_symbol.items() if s in crypto), ZERO)),
        )

    def verify(
        self,
        snap: PortfolioSnapshot,
        *,
        state: LedgerState | None = None,
        closes: Mapping[str, Decimal] | None = None,
        full_replay: bool | None = None,
    ) -> None:
        """Check I1-I7, I18 against ``snap``; raises ``LedgerError``.

        * ``state``: an already-folded state (e.g. the one the caller just used
          to build ``snap``). When given, no replay is done unless
          ``full_replay=True``; comparisons use this state. Default ``None``
          keeps the old behaviour: compare against the live fold and replay the
          whole ledger.
        * ``closes``: if given, every position's ``mv`` is recomputed as
          ``q2(qty * close)`` and must match the snapshot (so a self-consistent
          but mis-marked snapshot is caught), and I6 uses the recomputed values.
        * ``full_replay``: force (True) or skip (False) the O(events) replay.
        """
        with money_context():
            self._verify(snap, state, closes, full_replay)

    def _verify(
        self,
        snap: PortfolioSnapshot,
        given: LedgerState | None,
        closes: Mapping[str, Decimal] | None,
        full_replay: bool | None,
    ) -> None:
        state: LedgerState = given if given is not None else self.ledger.state_view
        do_replay = full_replay if full_replay is not None else given is None
        if do_replay:
            again = (
                replay(self.ledger.events, state.equity_qty_quantum, True)
                if state.t1
                else replay(self.ledger.events, state.equity_qty_quantum)
            )
            live = self.ledger.state_view
            if again.cash != live.cash or again.positions != live.positions:
                raise LedgerError("I5: replay does not reproduce state")
            if given is not None and (given.cash != live.cash or given.positions != live.positions):
                raise LedgerError("I5: supplied state does not match the ledger")
        _check_state_rules(state)
        total_mv = ZERO
        unreal = ZERO
        for s in snap.sleeves:
            if s.sleeve_id not in state.cash:
                raise LedgerError(f"I5: sleeve {s.sleeve_id} unknown to the ledger")
            mv = sum((p.mv for p in s.positions), ZERO)
            total_mv += mv
            if s.cash + mv != s.equity:
                raise LedgerError(f"I1: sleeve {s.sleeve_id}")
            if s.cash < 0:
                raise LedgerError(f"I3: sleeve {s.sleeve_id} cash < 0")
            if s.cash != state.cash[s.sleeve_id]:
                raise LedgerError(f"I5: sleeve {s.sleeve_id} cash != fold")
            held = state.positions.get(s.sleeve_id, {})
            for p in s.positions:
                if p.qty <= 0 or p.entry_asof is None:
                    raise LedgerError(f"I18: {s.sleeve_id}/{p.symbol}")
                fp = held.get(p.symbol)
                if fp is None or fp.qty != p.qty or fp.cost_basis != p.cost_basis:
                    raise LedgerError(f"I5: {s.sleeve_id}/{p.symbol} != fold")
                if closes is not None:
                    if p.symbol not in closes:
                        raise LedgerError(f"no close to re-mark {p.symbol}")
                    if p.mv != q2(p.qty * closes[p.symbol]):
                        raise LedgerError(f"I1: {s.sleeve_id}/{p.symbol} mv != qty * close")
                if p.unrealized != p.mv - p.cost_basis:
                    raise LedgerError(f"I6: {s.sleeve_id}/{p.symbol} unrealized")
                unreal += p.unrealized
            if len(held) != len(s.positions):
                raise LedgerError(f"I5: sleeve {s.sleeve_id} positions != fold")
        if snap.cash + total_mv != snap.equity:
            raise LedgerError("I1: portfolio")
        if sum((s.equity for s in snap.sleeves), ZERO) != snap.equity:
            raise LedgerError("I2")
        if state.deposits + state.realized - state.fees + unreal != snap.equity:
            raise LedgerError("I6")


def _check_state_rules(state: LedgerState) -> None:
    """I4 and I7 on a folded state (O(positions + open transfers))."""
    for sid, book in state.positions.items():
        for sym, p in book.items():
            if p.qty <= 0:
                raise LedgerError(f"I4: zero/negative qty retained for {sid}/{sym}")
            check_qty_grid(sym, p.qty, f"I4 {sid}/{sym}", state.equity_qty_quantum)
    bad = state.unpaired_transfers()
    if bad:
        raise LedgerError(f"I7: unpaired transfer legs {bad}")
    for tid, legs in state.transfers.items():
        if legs["OUT"][1] != legs["IN"][1]:
            raise LedgerError(f"I7: transfer {tid} does not net to zero")
