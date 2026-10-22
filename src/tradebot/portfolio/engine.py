"""``PortfolioEngine``: the one execution interface (SPEC-003 sections 1, 4, 7-10, ADR-0001 D3/D8).

The facade wires the broker (fills), risk checks, stops, kill switch and monthly
rebalance into the fixed per-bar protocol. Backtester and paper runner both call
it, so the same bars and target calls give the same ledger. Nothing here reads a
wall clock; iteration is sorted and arithmetic runs in ``money_context()``.

Per bar ``process_bar(asof, bars)`` runs, in this order: A fill at the open, B
mark at the close, C rebalance transfers, D stops, E snapshot + kill switch,
F rebalance decision. Then the caller may ``submit_target_weights``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.data.symbols import normalize_symbol
from tradebot.data.types import AssetClass
from tradebot.portfolio.config import EngineConfig, SleeveConfig, sleeve_deposits
from tradebot.portfolio.ledger import Ledger, LedgerError
from tradebot.portfolio.models import (
    EventType,
    LedgerEvent,
    PortfolioSnapshot,
    Stage,
)
from tradebot.portfolio.money import (
    PRICE_Q,
    WEIGHT_Q,
    money_context,
    to_price,
    to_ratio,
    to_weight,
)
from tradebot.portfolio.portfolio import Portfolio
from tradebot.portfolio.rebalance import (
    PORTFOLIO_SLEEVE_ID,
    RebalancePlan,
    book_planned,
    execute_transfers,
    is_rebalance_day,
    month_key,
    plan_rebalance,
    sizing_equity,
)
from tradebot.portfolio.state import (
    EngineState,
    PositionState,
    StateCorruptionError,
)
from tradebot.risk.checks import CheckResult, Rejection, check_target_weights
from tradebot.risk.killswitch import KillSwitch
from tradebot.risk.limits import PendingOrder
from tradebot.risk.sizing import SymbolMarket
from tradebot.risk.stops import StopBook
from tradebot.sim.broker import (
    Bar,
    BookError,
    BrokerBook,
    FillResult,
    SimBroker,
    StaleBarError,
    default_asset_class,
)
from tradebot.sim.orders import (
    Order,
    OrderIdGenerator,
    OrderOrigin,
    OrderRequest,
    OrderStatus,
    Side,
)

__all__ = [
    "BarResult",
    "PortfolioEngine",
    "StaleBarError",
    "StateCorruptionError",
    "TargetResult",
]

_UNFUNDED_ASOF = date.min
"""``asof`` stamped on the initial DEPOSIT events (the constructor has no bar date)."""


def _check_canonical(symbols: Sequence[str], where: str) -> None:
    """Raise ``ValueError`` unless every symbol is already in canonical form."""
    for sym in symbols:
        try:
            canon = str(normalize_symbol(sym))
        except ValueError as exc:
            raise ValueError(f"{where}: unknown symbol {sym!r}") from exc
        if canon != sym:
            raise ValueError(f"{where}: symbol {sym!r} is not canonical (expected {canon!r})")


def _id_number(ident: str) -> int:
    """Numeric part of an ``O00000001`` / ``T000001`` id (raises ``ValueError``)."""
    return int(ident[1:])


class BarResult(BaseModel):
    """Everything one ``process_bar`` did (the runner persists this per bar)."""

    model_config = ConfigDict(frozen=True)

    asof: date
    fills: list[Order]
    stop_orders: list[Order]
    expired: list[Order]
    new_ledger_events: list[LedgerEvent]
    snapshot: PortfolioSnapshot
    kill_switch_tripped: bool
    rebalance_planned: bool
    rejected: list[Order] = []  # orders the broker rejected at fill time (e.g. no cash)
    cancelled: list[Order] = []  # pending BUYs cancelled by a kill-switch trip


class TargetResult(BaseModel):
    """Orders accepted for a ``submit_target_weights`` call, plus vetoes and shrinks."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    orders: list[Order]
    rejections: list[Rejection]


def _finite(x: float | None) -> float | None:
    """``None`` for a missing or non-finite float (warm-up NaN from the data layer)."""
    return x if x is not None and math.isfinite(x) else None


class PortfolioEngine:
    """Deterministic, persistable execution/portfolio/risk engine."""

    def __init__(self, config: EngineConfig, state: EngineState | None = None) -> None:
        self._config = config
        self._asset_class_of = default_asset_class
        if state is None:
            self._init_fresh()
        else:
            if state.config != config:
                raise ValueError("state.config does not match the config passed to the engine")
            self._init_from_state(state)

    # ------------------------------------------------------------------ construction

    def _init_fresh(self) -> None:
        cfg = self._config
        self._ledger = Ledger()
        self._portfolio = Portfolio(self._ledger)
        self._ids = OrderIdGenerator()
        self._broker = SimBroker(
            cfg, self._portfolio, id_gen=self._ids, asset_class_of=self._asset_class_of
        )
        self._stops = StopBook()
        self._ks = KillSwitch(
            cfg.initial_capital, cfg.risk.kill_switch_drawdown, cfg.risk.kill_switch_pause_bars
        )
        self._episode: dict[tuple[str, str], int] = {}
        self._next_transfer_n = 0
        self._last_asof: date | None = None
        self._last_rebalance_month: str | None = None
        self._plan: RebalancePlan | None = None
        self._last_close: dict[str, Decimal] = {}
        self._last_vol20: dict[str, Decimal] = {}
        with money_context():
            for sleeve_id, amount in sleeve_deposits(cfg).items():
                self._ledger.append(
                    asof=_UNFUNDED_ASOF,
                    stage=Stage.SUBMIT,
                    sleeve_id=sleeve_id,
                    type=EventType.DEPOSIT,
                    amount=amount,
                )

    def _init_from_state(self, state: EngineState) -> None:
        cfg = self._config
        try:
            self._ledger = Ledger(state.ledger)
        except LedgerError as exc:
            raise StateCorruptionError(f"ledger does not replay: {exc}") from exc
        if state.next_seq != len(self._ledger) + 1:
            raise StateCorruptionError("next_seq does not match the ledger length")
        self._portfolio = Portfolio(self._ledger)
        folded = self._ledger.state
        if folded.cash != state.cash:
            raise StateCorruptionError("stored cash does not match the ledger replay")
        stored = {
            sid: sorted((p.symbol, p.qty, p.cost_basis, p.entry_asof) for p in ps)
            for sid, ps in state.positions.items()
            if ps
        }
        replayed = {
            sid: sorted((p.symbol, p.qty, p.cost_basis, p.entry_asof) for p in book.values())
            for sid, book in folded.positions.items()
            if book
        }
        if stored != replayed:
            raise StateCorruptionError("stored positions do not match the ledger replay")
        self._validate_counters_and_flags(state)
        self._ids = OrderIdGenerator(state.next_order_n)
        self._broker = SimBroker(
            cfg, self._portfolio, id_gen=self._ids, asset_class_of=self._asset_class_of
        )
        self._restore_book(state)
        self._stops = StopBook.from_state(state.stop_state)
        self._ks = KillSwitch.from_state(state.kill_state, cfg.risk.kill_switch_pause_bars)
        self._episode = {}
        for sid, syms in self._stops.to_state()["levels"].items():
            for sym in syms:
                opened = folded.opened_seq.get((sid, sym))
                if opened is None:
                    raise StateCorruptionError(f"stop levels for flat position {sid}/{sym}")
                self._episode[(sid, sym)] = opened
        for sid, ps in sorted(state.positions.items()):
            for p in ps:
                lv = self._stops.levels(sid, p.symbol)
                if lv is not None:
                    self._portfolio.set_stop_fields(
                        sid, p.symbol, lv.high_water_close, lv.stop_level
                    )
        self._next_transfer_n = state.next_transfer_n
        self._last_asof = state.last_processed_asof
        self._last_rebalance_month = state.last_rebalance_month
        self._plan = state.rebalance_plan
        self._last_close = dict(state.last_close)
        self._last_vol20 = dict(state.last_vol20)

    def _validate_counters_and_flags(self, state: EngineState) -> None:
        """Cross-check counters, kill flag, peak and last asof against ledger and orders (F4)."""
        try:
            max_order = max((_id_number(o.order_id) for o in state.orders), default=0)
            max_transfer = max(
                (_id_number(e.transfer_id) for e in state.ledger if e.transfer_id), default=0
            )
        except ValueError as exc:
            raise StateCorruptionError(f"malformed order/transfer id: {exc}") from exc
        if state.next_order_n <= max_order:
            raise StateCorruptionError("next_order_n is not above the highest order id used")
        if state.next_transfer_n < max_transfer:
            raise StateCorruptionError("next_transfer_n is below the highest transfer id used")
        kill_events = [
            e
            for e in state.ledger
            if e.type
            in (
                EventType.KILL_SWITCH_TRIPPED,
                EventType.KILL_SWITCH_RESET,
                EventType.KILL_SWITCH_REARMED,
            )
        ]
        killed = bool(state.kill_state.get("killed"))
        should_be_killed = (
            bool(kill_events) and kill_events[-1].type is EventType.KILL_SWITCH_TRIPPED
        )
        if killed != should_be_killed:
            raise StateCorruptionError("kill_state.killed does not match the KILL_SWITCH events")
        try:
            paused = int(state.kill_state.get("bars_paused", 0))
            pause_len = int(
                state.kill_state.get("pause_bars", self._config.risk.kill_switch_pause_bars)
            )
        except (TypeError, ValueError) as exc:
            raise StateCorruptionError(f"kill_state pause fields unreadable: {exc}") from exc
        if paused < 0 or (killed and paused >= pause_len) or (not killed and paused != 0):
            raise StateCorruptionError("kill_state.bars_paused is inconsistent with the pause")
        try:
            peak = Decimal(str(state.kill_state["peak_equity"]))
            floor = (
                Decimal(str(kill_events[-1].metadata["peak_equity"]))
                if kill_events
                else self._config.initial_capital
            )
            last_equity = Decimal(str(state.kill_state["last_equity"]))
        except (KeyError, ArithmeticError, TypeError) as exc:
            raise StateCorruptionError(f"kill_state unreadable: {exc}") from exc
        if peak < floor or (not killed and peak < last_equity):
            raise StateCorruptionError("kill_state.peak_equity is inconsistent with history")
        last_event = max((e.asof for e in state.ledger), default=_UNFUNDED_ASOF)
        if (state.last_processed_asof or _UNFUNDED_ASOF) < last_event:
            raise StateCorruptionError("last_processed_asof is before the last ledger event")

    def _restore_book(self, state: EngineState) -> None:
        """Load all orders, wait counters and last closes into the broker (public API only)."""
        book = BrokerBook(
            orders=sorted(state.orders, key=lambda x: x.order_id),
            pending_ids=sorted(o.order_id for o in state.orders if o.status is OrderStatus.PENDING),
            waited=dict(sorted(state.pending_waited.items())),
            last_asof=state.last_processed_asof,
            last_close=dict(sorted(state.last_close.items())),
        )
        try:
            self._broker.restore_book(book)
        except BookError as exc:
            raise StateCorruptionError(f"order book inconsistent: {exc}") from exc

    # ------------------------------------------------------------------ read-only views

    @property
    def ledger(self) -> Sequence[LedgerEvent]:
        """Read-only ledger events."""
        return self._ledger.events

    @property
    def sleeve_ids(self) -> Sequence[str]:
        """Sleeve ids in config order."""
        return [s.sleeve_id for s in self._config.sleeves]

    @property
    def config(self) -> EngineConfig:
        """The engine configuration."""
        return self._config

    @property
    def last_processed_asof(self) -> date | None:
        """Date of the last processed bar (``None`` before the first)."""
        return self._last_asof

    def get_order(self, order_id: str) -> Order:
        """Latest state of an order of any status (``KeyError`` if unknown)."""
        return self._broker.get_order(order_id)

    def orders(self) -> list[Order]:
        """All orders including terminal ones, in ``order_id`` order."""
        return list(self._broker.export_book().orders)

    def snapshot(self) -> PortfolioSnapshot:
        """Latest equity snapshot (pure read of current state, marked at the last closes)."""
        with money_context():
            return self._make_snapshot(self._last_asof if self._last_asof else date.min)

    def _make_snapshot(self, asof: date) -> PortfolioSnapshot:
        crypto = [
            s for s in sorted(self._last_close) if self._asset_class_of(s) is AssetClass.CRYPTO
        ]
        return self._portfolio.snapshot(
            asof,
            self._last_close,
            peak_equity=self._ks.peak_equity,
            killed=self._ks.killed,
            crypto_symbols=crypto,
        )

    def _sleeve(self, sleeve_id: str) -> SleeveConfig:
        for sc in self._config.sleeves:
            if sc.sleeve_id == sleeve_id:
                return sc
        raise ValueError(f"unknown sleeve {sleeve_id}")

    # ------------------------------------------------------------------ the per-bar protocol

    def process_bar(self, asof: date, bars: Mapping[str, Bar]) -> BarResult:
        """Run steps A-F for bar ``asof``. Raises ``StaleBarError`` if ``asof`` does not advance."""
        with money_context():
            return self._process_bar(asof, bars)

    def _process_bar(self, asof: date, bars: Mapping[str, Bar]) -> BarResult:
        if self._last_asof is not None and asof <= self._last_asof:
            raise StaleBarError(f"asof {asof} is not after last processed {self._last_asof}")
        _check_canonical(sorted(bars), "process_bar")
        for key, bar in bars.items():
            if bar.symbol != key:
                raise ValueError(f"process_bar: bars[{key!r}] holds a bar for {bar.symbol!r}")
        # Convert/validate every float up front so bad data fails before any mutation.
        closes: dict[str, Decimal] = {}
        vols: dict[str, Decimal | None] = {}
        atrs: dict[str, Decimal | None] = {}
        for sym in sorted(bars):
            b = bars[sym]
            to_price(b.open)
            closes[sym] = to_price(b.close)
            v, a = _finite(b.vol20), _finite(b.atr14)
            vols[sym] = None if v is None else to_ratio(v)
            atrs[sym] = None if a is None else to_ratio(a, PRICE_Q)

        n_before = len(self._ledger)
        # A: fills at the open.
        fill_result: FillResult = self._broker.process_bar(asof, bars)
        # B: mark at the close (symbols without a bar keep their last close).
        for sym in sorted(bars):
            self._last_close[sym] = closes[sym]
            vol = vols[sym]
            if vol is None:
                self._last_vol20.pop(sym, None)
            else:
                self._last_vol20[sym] = vol
        self._last_asof = asof
        # C: rebalance transfers for a plan decided on an earlier bar.
        if self._plan is not None:
            tr = execute_transfers(
                self._plan, self._portfolio, asof, self._next_transfer_n, self._ks
            )
            self._plan = tr.plan
            self._next_transfer_n = tr.next_transfer_n
        # D: trailing stops.
        stop_orders = self._step_stops(asof, closes, atrs)
        # E: snapshot and kill switch.
        snap = self._make_snapshot(asof)
        obs = self._ks.observe(snap.equity, asof)
        cancelled: list[Order] = []
        if obs.tripped_now:
            self._ledger.append(
                asof=asof,
                stage=Stage.CLOSE,
                sleeve_id=PORTFOLIO_SLEEVE_ID,
                type=EventType.KILL_SWITCH_TRIPPED,
                metadata={
                    "equity": str(snap.equity),
                    "peak_equity": str(obs.peak_equity),
                    "drawdown": str(obs.drawdown),
                },
            )
            for o in self._broker.pending_orders:
                if o.side is Side.BUY:
                    cancelled.append(self._broker.cancel_order(o.order_id))
            self._plan = None
        elif obs.rearmed_now:
            self._ledger.append(
                asof=asof,
                stage=Stage.CLOSE,
                sleeve_id=PORTFOLIO_SLEEVE_ID,
                type=EventType.KILL_SWITCH_REARMED,
                metadata={
                    "equity": str(snap.equity),
                    "peak_equity": str(obs.peak_equity),
                    "pause_bars": str(self._ks.pause_bars),
                },
            )
        # F: rebalance decision.
        planned = False
        if self._last_rebalance_month is None:
            self._last_rebalance_month = month_key(asof)  # seed: day one is not a rebalance
        if self._plan is None and is_rebalance_day(asof, self._last_rebalance_month):
            self._last_rebalance_month = month_key(asof)
            if not self._ks.rebalance_frozen:
                snap = self._make_snapshot(asof)
                plan = plan_rebalance(snap, self._config, asof, self._ks)
                if plan is not None:
                    book_planned(self._ledger, plan)
                    self._plan = plan
                    planned = True
        if snap.killed != self._ks.killed or snap.last_seq != len(self._ledger):
            snap = self._make_snapshot(asof)
        return BarResult(
            asof=asof,
            fills=fill_result.fills,
            stop_orders=stop_orders,
            expired=fill_result.expired,
            new_ledger_events=list(self._ledger.events[n_before:]),
            snapshot=snap,
            kill_switch_tripped=obs.tripped_now,
            rebalance_planned=planned,
            rejected=fill_result.rejected,
            cancelled=cancelled,
        )

    def _step_stops(
        self, asof: date, closes: Mapping[str, Decimal], atrs: Mapping[str, Decimal | None]
    ) -> list[Order]:
        """Step D: update trailing stops of held positions and submit STOP exits."""
        state = self._ledger.state
        # Drop stop levels of positions that went flat (or were re-opened) since last bar.
        for sid, syms in self._stops.to_state()["levels"].items():
            for sym in syms:
                key = (sid, sym)
                if sym not in state.positions.get(sid, {}) or state.opened_seq[
                    key
                ] != self._episode.get(key):
                    self._stops.clear(sid, sym)
                    self._episode.pop(key, None)
        out: list[Order] = []
        pending = self._broker.pending_orders
        for sc in sorted(self._config.sleeves, key=lambda c: c.sleeve_id):
            mult = sc.trailing_stop_atr_mult
            if mult is None:
                continue
            sid = sc.sleeve_id
            for sym, pos in sorted(self._portfolio.positions(sid).items()):
                if sym not in closes:  # stale mark: nothing new to test
                    continue
                upd = self._stops.on_close(sid, sym, closes[sym], atrs[sym], mult)
                self._episode[(sid, sym)] = state.opened_seq[(sid, sym)]
                self._portfolio.set_stop_fields(
                    sid, sym, upd.levels.high_water_close, upd.levels.stop_level
                )
                if not upd.triggered:
                    continue
                mine = [o for o in pending if o.sleeve_id == sid and o.symbol == sym]
                mine_sells = [o for o in mine if o.side is Side.SELL]
                if any(o.origin is OrderOrigin.STOP for o in mine_sells):
                    continue  # a STOP exit is already pending: no duplicates
                qty = pos.qty - sum((o.qty_requested for o in mine_sells), Decimal(0))
                if qty <= 0:
                    continue
                order = self._broker.submit_orders(
                    [
                        OrderRequest(
                            sleeve_id=sid,
                            symbol=sym,
                            side=Side.SELL,
                            qty=qty,
                            origin=OrderOrigin.STOP,
                        )
                    ],
                    asof,
                )[0]
                pending = self._broker.pending_orders
                self._ledger.append(
                    asof=asof,
                    stage=Stage.CLOSE,
                    sleeve_id=sid,
                    type=EventType.STOP_TRIGGERED,
                    symbol=sym,
                    qty=qty,
                    price=closes[sym],
                    order_id=order.order_id,
                    metadata={
                        "high_water_close": str(upd.levels.high_water_close),
                        "stop_level": str(upd.levels.stop_level),
                    },
                )
                out.append(order)
        return out

    # ------------------------------------------------------------------ strategy intent

    def submit_target_weights(
        self, sleeve_id: str, weights: Mapping[str, float], asof: date
    ) -> TargetResult:
        """Size, risk-check and submit a sleeve's target weights (call after ``process_bar``).

        ``weights`` are fractions of sleeve equity (long-only, sum <= 1); a symbol
        absent from ``weights`` has target 0. ``asof`` must be the last processed bar.
        """
        with money_context():
            return self._submit_target_weights(sleeve_id, weights, asof)

    def _submit_target_weights(
        self, sleeve_id: str, weights: Mapping[str, float], asof: date
    ) -> TargetResult:
        sleeve = self._sleeve(sleeve_id)
        if self._last_asof is None or asof != self._last_asof:
            raise ValueError(
                f"submit_target_weights asof {asof} must equal the last processed bar "
                f"{self._last_asof}"
            )
        _check_canonical(sorted(weights), "submit_target_weights")
        w = {sym: to_weight(x) for sym, x in sorted(weights.items())}
        # Each weight is rounded to WEIGHT_Q, so the sum may drift by len(w) * WEIGHT_Q.
        if sum(w.values(), Decimal(0)) > 1 + len(w) * WEIGHT_Q:
            raise ValueError("target weights must sum to at most 1")
        # Stop locks re-arm (after the call succeeds) for every symbol held, locked or targeted
        # with a zero/omitted weight; the lock applied to this call already reflects that.
        locked = set(self._stops.to_state()["stopped_out"].get(sleeve_id, ()))
        held = set(self._portfolio.positions(sleeve_id))
        rearm = {s for s in held | locked | set(w) if w.get(s, Decimal(0)) == 0}
        stopped = {s for s in held | set(w) if s in locked and s not in rearm}

        res, pending_orders = self._run_checks(sleeve, w, stopped)
        # F2: a target at or below the holding cancels the sleeve's pending BUYs; re-check.
        mine = {o.symbol for o in pending_orders if o.sleeve_id == sleeve_id and o.side is Side.BUY}
        have = {sym: p.qty for sym, p in self._portfolio.positions(sleeve_id).items()}
        stale = {
            sym
            for sym in mine
            if sym in res.target_qty and res.target_qty[sym] <= have.get(sym, Decimal(0))
        }
        if stale:
            for o in pending_orders:
                if o.sleeve_id == sleeve_id and o.side is Side.BUY and o.symbol in stale:
                    self._broker.cancel_order(o.order_id)
            res, pending_orders = self._run_checks(sleeve, w, stopped)
        for sym in sorted(rearm):
            self._stops.note_target(sleeve_id, sym, True)
        orders = self._broker.submit_orders(res.orders, asof) if res.orders else []
        return TargetResult(orders=orders, rejections=list(res.rejections))

    def _run_checks(
        self, sleeve: SleeveConfig, w: Mapping[str, Decimal], stopped: set[str]
    ) -> tuple[CheckResult, list[Order]]:
        """Build markets from current state and run ``check_target_weights``."""
        sleeve_id = sleeve.sleeve_id
        assert self._last_asof is not None
        pending_orders = self._broker.pending_orders
        snap = self._make_snapshot(self._last_asof)
        symbols = set(w) | {o.symbol for o in pending_orders}
        symbols |= {p.symbol for s in snap.sleeves for p in s.positions}
        markets = {
            sym: SymbolMarket(
                symbol=sym,
                asset_class=self._asset_class_of(sym),
                ref_price=self._last_close.get(sym),
                vol20=self._last_vol20.get(sym),
            )
            for sym in sorted(symbols)
        }
        res = check_target_weights(
            sleeve=sleeve,
            weights=w,
            snapshot=snap,
            markets=markets,
            risk=self._config.risk,
            costs=self._config.costs,
            pending=[
                PendingOrder(o.sleeve_id, o.symbol, o.side, o.qty_requested) for o in pending_orders
            ],
            stopped_out=stopped,
            sizing_equity=sizing_equity(self._plan, sleeve_id),
        )
        return res, pending_orders

    # ------------------------------------------------------------------ low-level order API

    def submit_orders(self, requests: Sequence[OrderRequest], asof: date) -> list[Order]:
        """Low-level order entry (stops, tests). BUYs are refused while the switch is tripped."""
        if self._ks.killed and any(r.side is Side.BUY for r in requests):
            raise ValueError("kill switch tripped: BUY orders are not accepted")
        with money_context():
            return self._broker.submit_orders(requests, asof)

    def cancel_order(self, order_id: str) -> Order:
        """Cancel a PENDING order."""
        return self._broker.cancel_order(order_id)

    def reset_kill_switch(self, operator: str, note: str, asof: date) -> None:
        """Manual reset (CLI only; never called by the backtester or driver).

        Clears the flag, sets ``peak_equity`` to the last observed equity and books
        ``KILL_SWITCH_RESET``. Raises ``ValueError`` if the switch is not tripped.
        """
        if not self._ks.killed:
            raise ValueError("kill switch is not tripped")
        with money_context():
            self._ks.reset(operator, note, asof)
            self._ledger.append(
                asof=asof,
                stage=Stage.CLOSE,
                sleeve_id=PORTFOLIO_SLEEVE_ID,
                type=EventType.KILL_SWITCH_RESET,
                metadata={
                    "operator": operator,
                    "note": note,
                    "peak_equity": str(self._ks.peak_equity),
                },
            )

    # ------------------------------------------------------------------ persistence

    def export_state(self) -> EngineState:
        """Checkpoint everything needed to continue identically (see :class:`EngineState`)."""
        with money_context():
            book = self._broker.export_book()
            folded = self._ledger.state
            positions = {
                sid: [
                    PositionState(
                        symbol=sym,
                        qty=p.qty,
                        cost_basis=p.cost_basis,
                        entry_asof=p.entry_asof,  # type: ignore[arg-type]
                        high_water_close=p.high_water_close,
                        stop_level=p.stop_level,
                    )
                    for sym, p in self._portfolio.positions(sid).items()
                ]
                for sid in sorted(folded.cash)
            }
            return EngineState(
                config=self._config,
                ledger=list(self._ledger.events),
                orders=list(book.orders),
                pending_waited=dict(book.waited),
                next_order_n=self._ids.next_n,
                next_transfer_n=self._next_transfer_n,
                next_seq=self._ledger.next_seq,
                last_processed_asof=self._last_asof,
                kill_state=self._ks.to_state(),
                stop_state=self._stops.to_state(),
                last_rebalance_month=self._last_rebalance_month,
                rebalance_plan=self._plan,
                last_close=dict(sorted(self._last_close.items())),
                last_vol20=dict(sorted(self._last_vol20.items())),
                cash=dict(sorted(folded.cash.items())),
                positions=positions,
            )

    def verify(self) -> None:
        """Replay the ledger and check invariants; raises ``StateCorruptionError`` on mismatch."""
        try:
            with money_context():
                self._portfolio.verify(self._make_snapshot(self._last_asof or date.min))
                self._verify_orders_and_transfers()
        except (LedgerError, KeyError) as exc:
            raise StateCorruptionError(str(exc)) from exc

    def _verify_orders_and_transfers(self) -> None:
        """I7 (transfers pair up) and I14 (at most one FILL per order, matching its order)."""
        book_orders = {o.order_id: o for o in self._broker.export_book().orders}
        fills: dict[str, LedgerEvent] = {}
        transfers: dict[str, Decimal] = {}
        for ev in self._ledger.events:
            if ev.type is EventType.FILL:
                assert ev.order_id is not None
                if ev.order_id in fills:
                    raise LedgerError(f"I14: order {ev.order_id} filled twice")
                fills[ev.order_id] = ev
            elif ev.type is EventType.CASH_TRANSFER_OUT:
                transfers[ev.transfer_id or ""] = transfers.get(ev.transfer_id or "", 0) + ev.amount
            elif ev.type is EventType.CASH_TRANSFER_IN:
                transfers[ev.transfer_id or ""] = transfers.get(ev.transfer_id or "", 0) - ev.amount
        if any(v != 0 for v in transfers.values()):
            raise LedgerError("I7: transfer OUT/IN amounts do not net to zero")
        for oid, ev in fills.items():
            order = book_orders.get(oid)
            if (
                order is None
                or order.status is not OrderStatus.FILLED
                or order.qty_filled != ev.qty
                or order.side.value != (ev.side.value if ev.side else "")
            ):
                raise LedgerError(f"I14: FILL for {oid} does not match its order")
        for oid, order in book_orders.items():
            if order.status is OrderStatus.FILLED and oid not in fills:
                raise LedgerError(f"I14: FILLED order {oid} has no FILL event")
