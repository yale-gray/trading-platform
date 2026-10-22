"""SPEC-003 invariants I1-I18 as properties over random bar/target paths (T12, T14).

Each example drives a ``PortfolioEngine`` through a random scenario (stops, kill-switch
trips and resets, monthly rebalances, save/restore in the middle) and checks, after every
bar, the engine against an independent ``RefLedger`` oracle. A second, plain run of the
same scenario (no restores, default ambient context) must give byte-identical output (I15).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from support.oracle import (
    RefLedger,
    buy_price,
    cents,
    drawdown_trips,
    fee_cents,
    price_from_float,
    sell_price,
)
from support.strategies import Scenario, scenarios

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import EngineConfig, default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.state import EngineState
from tradebot.risk.config import RiskConfig
from tradebot.sim.broker import Bar, StaleBarError
from tradebot.sim.orders import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    Order,
    OrderOrigin,
    OrderStatus,
    Side,
)

pytestmark = pytest.mark.property

D = Decimal
CORE = "core_momentum"


def config_for(scn: Scenario) -> EngineConfig:
    """Default sleeves, trailing stops on two of them, the scenario's kill threshold."""
    base = default_config()
    sleeves = tuple(
        s.model_copy(update={"trailing_stop_atr_mult": scn.stop_mult})
        if s.sleeve_id in ("mean_reversion", "crypto_trend")
        else s
        for s in base.sleeves
    )
    return base.model_copy(
        update={
            "sleeves": sleeves,
            "risk": RiskConfig(kill_switch_drawdown=scn.kill_threshold),
        }
    )


@contextmanager
def ambient(prec: int) -> Iterator[None]:
    """Run engine calls under a hostile ambient decimal context (engine must not care)."""
    with localcontext() as ctx:
        ctx.prec = prec
        yield


def _no_floats(obj: Any, path: str = "") -> None:
    """I17: no ``float`` anywhere in a model's Python-level dump."""
    if isinstance(obj, float):
        raise AssertionError(f"float at {path}: {obj}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _no_floats(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _no_floats(v, f"{path}[{i}]")


@dataclass
class Checker:
    """Holds the oracle and cross-bar memory for the invariant checks."""

    cfg: EngineConfig
    threshold: Decimal
    ref: RefLedger = field(init=False)
    cursor: int = 0
    prefix: tuple[Any, ...] = ()
    peak: Decimal = D("0")
    last_equity: Decimal = D("0")
    killed: bool = False
    paused: int = 0
    episodes: dict[tuple[str, str], int] = field(default_factory=dict)
    entry_seen: dict[tuple[str, str], date] = field(default_factory=dict)
    ratchet: dict[tuple[str, str, int], tuple[D, D | None]] = field(default_factory=dict)
    terminal: dict[str, Order] = field(default_factory=dict)
    stop_orders: dict[str, Order] = field(default_factory=dict)
    plan: dict[str, Any] | None = None
    n_fills: int = 0
    n_trips: int = 0
    n_stops: int = 0
    n_rebalances: int = 0
    n_resets: int = 0

    def __post_init__(self) -> None:
        costs = self.cfg.costs
        self.ref = RefLedger(costs[AssetClass.EQUITY].fee_bps, costs[AssetClass.CRYPTO].fee_bps)
        self.peak = self.cfg.initial_capital
        self.last_equity = self.cfg.initial_capital
        self.slip = {
            False: costs[AssetClass.EQUITY].slip_bps,
            True: costs[AssetClass.CRYPTO].slip_bps,
        }

    # ------------------------------------------------------------------ per bar

    def after_bar(self, eng: PortfolioEngine, res: Any, bars: dict[str, Bar], asof: date) -> None:
        was_killed = self.killed
        events = list(eng.ledger)
        # I7: dense seq, append-only prefix.
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert tuple(events[: len(self.prefix)]) == self.prefix
        new = events[self.cursor :]
        self.prefix, self.cursor = tuple(events), len(events)
        assert {e.seq for e in res.new_ledger_events} <= {e.seq for e in new}
        for ev in new:
            _no_floats(ev.model_dump(), "event")
            if str(ev.type) == "FILL":
                key = (ev.sleeve_id, ev.symbol)
                if str(ev.side) == "BUY" and key not in self.ref.pos.get(ev.sleeve_id, {}):
                    self.episodes[key] = self.episodes.get(key, 0) + 1
                self.n_fills += 1
            self.ref.replay([ev])
        assert not self.ref.unmatched_fees()
        # I7: transfers pair up and net to zero per id.
        net: dict[str, Decimal] = {}
        for ev in new:
            if str(ev.type) == "CASH_TRANSFER_OUT":
                net[ev.transfer_id] = net.get(ev.transfer_id, D(0)) + ev.amount
            elif str(ev.type) == "CASH_TRANSFER_IN":
                net[ev.transfer_id] = net.get(ev.transfer_id, D(0)) - ev.amount
        assert all(v == 0 for v in net.values())
        self.ref.set_closes({s: price_from_float(b.close) for s, b in bars.items()})

        self._check_fills(res, bars, asof, was_killed)
        self._check_snapshot(res.snapshot)
        self._check_kill(res, asof)
        self._check_orders(eng, bars, asof, res)
        self._check_stops(res, res.snapshot)
        self._check_rebalance(eng, res, new)
        _no_floats(res.snapshot.model_dump(), "snapshot")

    def _check_fills(self, res: Any, bars: dict[str, Bar], asof: date, was_killed: bool) -> None:
        for o in res.fills:
            # I8: fills at this bar's open only, created strictly before.
            assert o.fill_asof == asof and o.created_asof < asof
            open_ = price_from_float(bars[o.symbol].open)
            slip = self.slip["/" in o.symbol]
            expect = buy_price(open_, slip) if o.side is Side.BUY else sell_price(open_, slip)
            assert o.fill_price == expect  # I8 + I9 (price rule, ceil/floor)
            if o.side is Side.BUY:
                assert o.fill_price >= open_
                assert not was_killed  # I11
            else:
                assert o.fill_price <= open_
            if "/" not in o.symbol:
                assert o.fee == 0
            else:
                notional = cents(o.qty_filled * o.fill_price)
                assert o.fee == fee_cents(notional * self.ref.fee_bps[True] / D(10000))
            assert o.qty_filled <= o.qty_accepted <= o.qty_requested  # I14

    def _check_snapshot(self, snap: Any) -> None:
        ref = self.ref
        total = D(0)
        mv_total = D(0)
        cash_total = D(0)
        by_id = {s.sleeve_id: s for s in snap.sleeves}
        assert set(by_id) == set(ref.cash)
        for sid, sl in by_id.items():
            assert sl.cash >= 0  # I3
            assert sl.cash == ref.cash[sid]  # I5 against the oracle
            mv = sum((p.mv for p in sl.positions), D(0))
            assert sl.cash + mv == sl.equity  # I1
            assert sl.equity == ref.sleeve_equity(sid)
            seen = {p.symbol for p in sl.positions}
            assert seen == set(ref.pos.get(sid, {}))  # I4: no zero-qty retained
            for p in sl.positions:
                q, basis = ref.pos[sid][p.symbol]
                assert p.qty == q > 0 and p.cost_basis == basis  # I4/I5/I6
                assert p.mv == ref.mv(p.symbol, q)
                assert p.unrealized == p.mv - basis
                if "/" in p.symbol:
                    assert p.qty == p.qty.quantize(D("1E-8"))
                else:
                    assert p.qty == p.qty.to_integral_value()
                # I18: entry_asof set while held; unchanged by adds and trims.
                key = (sid, p.symbol)
                assert p.entry_asof == ref.entry[key]
                assert p.entry_asof is not None
            total += sl.equity
            mv_total += mv
            cash_total += sl.cash
        assert total == snap.equity  # I2
        assert cash_total + mv_total == snap.equity  # I1 portfolio level
        assert snap.cash == cash_total
        # I6: capital + realized - fees + unrealized == equity, exactly.
        assert ref.deposits + ref.realized - ref.fees + ref.unrealized() == snap.equity
        assert snap.equity == ref.equity()

    def _check_kill(self, res: Any, asof: date) -> None:
        snap = res.snapshot
        eq = snap.equity
        self.last_equity = eq
        types = [e.type for e in res.new_ledger_events]
        if self.killed:  # A18: paused for N processed bars, then re-armed at current equity
            assert not res.kill_switch_tripped
            self.paused += 1
            if self.paused >= self.cfg.risk.kill_switch_pause_bars:
                self.killed, self.paused, self.peak = False, 0, eq
                assert "KILL_SWITCH_REARMED" in types
            else:
                assert "KILL_SWITCH_REARMED" not in types
        else:
            assert "KILL_SWITCH_REARMED" not in types
            if eq > self.peak:
                self.peak = eq
            if drawdown_trips(eq, self.peak, self.threshold):  # I12: trips iff drawdown >= t
                assert res.kill_switch_tripped
                self.killed, self.paused = True, 0
                self.n_trips += 1
            else:
                assert not res.kill_switch_tripped
        assert snap.killed == self.killed  # sticky for N bars (or until manual reset)
        assert snap.peak_equity == self.peak

    def reset(self, eng: PortfolioEngine, asof: date) -> None:
        """Operator reset: allowed only when tripped; peak becomes the last equity."""
        if not self.killed:
            before = eng.export_state().to_json()
            with pytest.raises(ValueError):
                eng.reset_kill_switch("op", "n", asof)
            assert eng.export_state().to_json() == before
            return
        eng.reset_kill_switch("op", "n", asof)
        self.n_resets += 1
        self.killed = False
        self.paused = 0
        self.peak = cents(self.last_equity)

    def _check_orders(
        self, eng: PortfolioEngine, bars: dict[str, Bar], asof: date, res: Any
    ) -> None:
        orders = eng.orders()
        by_id = {o.order_id: o for o in orders}
        for oid, was in self.terminal.items():
            assert by_id[oid] == was  # I14: terminal states are final
        for o in orders:
            if o.status in TERMINAL_STATES:
                self.terminal[o.order_id] = o
            if o.qty_accepted is not None:
                assert o.qty_filled <= o.qty_accepted <= o.qty_requested
            if o.status is OrderStatus.FILLED:
                assert 0 < o.qty_filled <= o.qty_accepted
            assert o.status in LEGAL_TRANSITIONS  # reachable status only
        # Orders created before today whose symbol had a bar today are no longer pending.
        for o in orders:
            if o.status is OrderStatus.PENDING and o.created_asof < asof and o.symbol in bars:
                raise AssertionError(f"order {o.order_id} still pending after its bar")
        # Expiry: nothing waits longer than max_pending_bars.
        _ = res

    def _check_stops(self, res: Any, snap: Any) -> None:
        held = {(s.sleeve_id, p.symbol): p for s in snap.sleeves for p in s.positions}
        for o in res.stop_orders:
            assert o.side is Side.SELL and o.origin is OrderOrigin.STOP  # I13
            assert o.qty_requested <= held[(o.sleeve_id, o.symbol)].qty
            self.stop_orders[o.order_id] = o
            self.n_stops += 1
        for (sid, sym), p in held.items():
            ep = self.episodes.get((sid, sym), 0)
            if p.stop_level is None:
                continue
            assert p.high_water_close is not None
            key = (sid, sym, ep)
            if key in self.ratchet:
                old_stop, old_hw = self.ratchet[key]
                assert p.stop_level >= old_stop  # I13: never decreases while open
                assert old_hw is None or p.high_water_close >= old_hw
            self.ratchet[key] = (p.stop_level, p.high_water_close)

    def _check_rebalance(self, eng: PortfolioEngine, res: Any, new: list[Any]) -> None:
        """I16: a completed rebalance moves each sleeve to its target (within cents)."""
        types = [str(e.type) for e in new]
        if self.plan is not None:
            active = eng.export_state().rebalance_plan
            if active is None and "REBALANCE_INCOMPLETE" not in types and not res.snapshot.killed:
                plan, moved = self.plan, self.plan["moved"]
                for e in new:
                    if str(e.type) == "CASH_TRANSFER_OUT":
                        moved[e.sleeve_id] = moved.get(e.sleeve_id, D(0)) - e.amount
                    elif str(e.type) == "CASH_TRANSFER_IN":
                        moved[e.sleeve_id] = moved.get(e.sleeve_id, D(0)) + e.amount
                for sc in eng.config.sleeves:
                    sid = sc.sleeve_id
                    after = plan["equity_by"][sid] + moved.get(sid, D(0))
                    weight = after / plan["equity"]
                    assert abs(weight - sc.target_weight) <= sc.min_trade_fraction + D("0.000001")
                self.n_rebalances += 1
                self.plan = None
            elif active is None:
                self.plan = None
            else:
                for e in new:
                    if str(e.type) == "CASH_TRANSFER_OUT":
                        self.plan["moved"][e.sleeve_id] = (
                            self.plan["moved"].get(e.sleeve_id, D(0)) - e.amount
                        )
                    elif str(e.type) == "CASH_TRANSFER_IN":
                        self.plan["moved"][e.sleeve_id] = (
                            self.plan["moved"].get(e.sleeve_id, D(0)) + e.amount
                        )
        if res.rebalance_planned:
            ev = next(e for e in reversed(new) if str(e.type) == "REBALANCE_PLANNED")
            equity = D(ev.metadata["equity"])
            target = {sc.sleeve_id: cents(sc.target_weight * equity) for sc in eng.config.sleeves}
            equity_by = {sid: target[sid] + D(ev.metadata[f"surplus.{sid}"]) for sid in target}
            self.plan = {"equity": equity, "equity_by": equity_by, "moved": {}}

    # ------------------------------------------------------------------ submissions

    def check_submission(
        self, eng: PortfolioEngine, sleeve: str, weights: dict[str, float], tr: Any
    ) -> None:
        snap = eng.snapshot()
        if self.killed:
            assert not any(o.side is Side.BUY for o in tr.orders)  # I11
        pending = [o for o in eng.orders() if o.status is OrderStatus.PENDING]
        # No duplicate STOP, no SELLs beyond holdings.
        stops = [(o.sleeve_id, o.symbol) for o in pending if o.origin is OrderOrigin.STOP]
        assert len(stops) == len(set(stops))
        held = {(s.sleeve_id, p.symbol): p.qty for s in snap.sleeves for p in s.positions}
        sells: dict[tuple[str, str], Decimal] = {}
        for o in pending:
            if o.side is Side.SELL:
                key = (o.sleeve_id, o.symbol)
                sells[key] = sells.get(key, D(0)) + o.qty_requested
        assert all(q <= held.get(k, D(0)) for k, q in sells.items())
        # I10: exposure after a BUY (positions + pending BUYs at last close) within the caps.
        eq = snap.equity

        def expo(sym: str) -> Decimal:
            mv = sum((p.mv for s in snap.sleeves for p in s.positions if p.symbol == sym), D(0))
            pend = sum(
                (o.qty_requested for o in pending if o.side is Side.BUY and o.symbol == sym), D(0)
            )
            return mv + pend * self.ref.closes[sym]

        bought = {o.symbol for o in tr.orders if o.side is Side.BUY}
        slack = D("0.05")
        for sym in bought:
            assert expo(sym) <= self.cfg.risk.max_asset_fraction * eq + slack, (sym, expo(sym), eq)
        if any("/" in s for s in bought):
            crypto = sum((expo(s) for s in self.ref.closes if "/" in s), D(0))
            assert crypto <= self.cfg.risk.max_crypto_fraction * eq + slack, (crypto, eq)


@dataclass
class Trace:
    """What a run produced, for the byte-for-byte comparison (I15)."""

    lines: list[str]
    final_state: str
    checker: Checker | None


def run_scenario(scn: Scenario, *, check: bool, restore: bool) -> Trace:
    """Drive the engine through ``scn``; ``check`` enables the oracle, ``restore`` the saves."""
    cfg = config_for(scn)
    prec = scn.ambient_prec if restore else 28
    with ambient(prec):
        eng = PortfolioEngine(cfg)
    chk = Checker(cfg, scn.kill_threshold) if check else None
    if chk is not None:
        chk.ref.replay(list(eng.ledger))
        chk.cursor = len(eng.ledger)
        chk.prefix = tuple(eng.ledger)
    lines: list[str] = []
    last: date | None = None
    for idx, step in enumerate(scn.steps):
        with ambient(prec):
            res = eng.process_bar(step.asof, step.bars)
        lines.append(res.model_dump_json())
        if chk is not None:
            chk.after_bar(eng, res, step.bars, step.asof)
            if idx % 4 == 0:
                eng.verify()  # I5: ledger replays and matches stored state
            if (
                idx == 3 and last is not None
            ):  # I15: a stale or repeated asof is refused, state untouched
                before = eng.export_state().to_json()
                with pytest.raises(StaleBarError), ambient(prec):
                    eng.process_bar(last, step.bars)
                assert eng.export_state().to_json() == before
        last = step.asof
        for sid, weights in step.targets.items():
            with ambient(prec):
                tr = eng.submit_target_weights(sid, weights, step.asof)
            lines.append(tr.model_dump_json())
            if chk is not None:
                chk.check_submission(eng, sid, weights, tr)
        if step.reset_kill:
            if chk is not None:
                with ambient(prec):
                    chk.reset(eng, step.asof)
            elif eng.snapshot().killed:
                with ambient(prec):
                    eng.reset_kill_switch("op", "n", step.asof)
            lines.append(eng.snapshot().model_dump_json())
        if restore and step.restore:
            js = eng.export_state().to_json()
            with ambient(prec):
                eng = PortfolioEngine(cfg, EngineState.from_json(js))
            assert eng.export_state().to_json() == js  # I5/I15 round trip
            eng.verify()
    return Trace(lines, eng.export_state().to_json(), chk)


# ---------------------------------------------------------------------------- properties


@settings(max_examples=40)
@given(scn=scenarios(min_bars=25, max_bars=60))
def test_invariants_i1_to_i18_and_restore_equivalence(scn: Scenario) -> None:
    """I1-I18 on every bar; save/restore mid-run and a different ambient context change nothing."""
    checked = run_scenario(scn, check=True, restore=True)
    plain = run_scenario(scn, check=False, restore=False)
    assert checked.lines == plain.lines  # I15 restore-and-continue == uninterrupted
    assert checked.final_state == plain.final_state


@settings(max_examples=10)
@given(scn=scenarios(min_bars=30, max_bars=60, with_restores=False))
def test_two_identical_runs_are_byte_identical(scn: Scenario) -> None:
    """I15: same input twice gives the same ``export_state`` bytes."""
    a = run_scenario(scn, check=False, restore=False)
    b = run_scenario(scn, check=False, restore=False)
    assert a.final_state == b.final_state and a.lines == b.lines


def test_hash_seed_independence() -> None:
    """I15 / G8: ``export_state`` is identical under different ``PYTHONHASHSEED`` values."""
    code = (
        "import hashlib, random\n"
        "from support.strategies import _make_steps, Scenario\n"
        "from datetime import date\n"
        "from decimal import Decimal\n"
        "import test_engine_invariants as t\n"
        "steps = _make_steps(random.Random(7), 70, date(2024, 1, 20), 30, 8, 0.12, 0.02,"
        " 0.05, 0.6, 0, 0.1)\n"
        "scn = Scenario(Decimal('0.20'), Decimal('2.5'), 28, steps)\n"
        "tr = t.run_scenario(scn, check=False, restore=False)\n"
        "print(hashlib.sha256(tr.final_state.encode()).hexdigest())\n"
    )
    here = Path(__file__).resolve().parent
    tests_dir = here.parent
    outs = []
    for seed in ("0", "12345"):
        env = {"PYTHONHASHSEED": seed, "PYTHONPATH": f"{tests_dir}:{here}", "PATH": "/usr/bin"}
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        outs.append(proc.stdout.strip())
    assert outs[0] == outs[1] and len(outs[0]) == 64


def test_scenarios_exercise_every_mechanism() -> None:
    """Guard against vacuous properties: trips, stops, rebalances and resets all occur."""
    import random

    from support.strategies import _make_steps

    totals = {"fills": 0, "trips": 0, "stops": 0, "rebalances": 0, "resets": 0}
    for seed in range(5):
        steps = _make_steps(
            random.Random(seed), 70, date(2024, 1, 20), 20, 15, 0.45, 0.03, 0.03, 0.8, 6, 0.1
        )
        scn = Scenario(D("0.10"), D("1.5"), 28, steps)
        chk = run_scenario(scn, check=True, restore=True).checker
        assert chk is not None
        totals["fills"] += chk.n_fills
        totals["trips"] += chk.n_trips
        totals["stops"] += chk.n_stops
        totals["rebalances"] += chk.n_rebalances
        totals["resets"] += chk.n_resets
    assert all(v > 0 for v in totals.values()), totals


# ---------------------------------------------------------------------------- oracle self-test


def test_oracle_reproduces_spec_005_section_6_3_etf_round_trip() -> None:
    """ETF: fee 0, slip 5 bps. Buy 100 at open 100, sell 100 at open 110 (SPEC-005 6.3)."""
    ref = RefLedger(D("0"), D("10"))
    ref.deposit("s", D("100000.00"))
    p_buy = buy_price(D("100.00"), D("5"))
    assert p_buy == D("100.05000000")
    assert ref.fill("s", "SPY", "BUY", D("100"), p_buy, "O1") == D("10005.00")
    ref.fee("s", "O1", D("0.00"))
    assert ref.cash["s"] == D("89995.00")
    ref.set_closes({"SPY": D("105.00")})
    assert ref.equity() == D("100495.00")
    p_sell = sell_price(D("110.00"), D("5"))
    assert p_sell == D("109.94500000")
    assert ref.fill("s", "SPY", "SELL", D("100"), p_sell, "O2") == D("10994.50")
    ref.fee("s", "O2", D("0.00"))
    assert ref.cash["s"] == D("100989.50") and ref.equity() == D("100989.50")
    assert ref.realized == D("989.50") and not ref.pos["s"]


def test_oracle_reproduces_spec_005_section_6_4_crypto_round_trip() -> None:
    """Crypto: fee 10 bps, slip 10 bps. Buy 0.5 BTC at 20,000, sell at 22,000 (SPEC-005 6.4)."""
    ref = RefLedger(D("0"), D("10"))
    ref.deposit("s", D("100000.00"))
    p_buy = buy_price(D("20000"), D("10"))
    assert p_buy == D("20020.00000000")
    assert ref.fill("s", "BTC/USD", "BUY", D("0.5"), p_buy, "O1") == D("10010.00")
    ref.fee("s", "O1", D("10.01"))
    assert ref.cash["s"] == D("89979.99")
    p_sell = sell_price(D("22000"), D("10"))
    assert p_sell == D("21978.00000000")
    assert ref.fill("s", "BTC/USD", "SELL", D("0.5"), p_sell, "O2") == D("10989.00")
    ref.fee("s", "O2", D("10.99"))
    assert ref.cash["s"] == D("100958.00")
    assert ref.fees == D("21.00")
    assert ref.cash["s"] - D("100000.00") == D("958.00")


def test_oracle_rejects_a_wrong_fee() -> None:
    """The oracle must be able to fail: a half-even fee (0.00 for 0.005) is refused."""
    ref = RefLedger(D("0"), D("10"))
    ref.deposit("s", D("1000.00"))
    ref.fill("s", "BTC/USD", "BUY", D("0.0001"), D("50000"), "O1")  # notional 5.00 -> fee 0.005
    with pytest.raises(AssertionError):
        ref.fee("s", "O1", D("0.00"))
