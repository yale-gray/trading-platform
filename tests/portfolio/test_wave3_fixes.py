"""Wave 3 fix pass: audit 003 findings F1, F2, F4, F5, F6, F8, F9 and the weight-sum blocker."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from scenario_bars import bar, book

from tradebot.portfolio.config import default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType
from tradebot.portfolio.state import EngineState, StateCorruptionError
from tradebot.risk.stops import update_stop
from tradebot.sim.orders import OrderOrigin, OrderRequest, OrderStatus, Side
from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.schema import STRATEGIES
from tradebot.strategy.signals import HeldPosition

CORE, MR, CRYPTO, CASH = "core_momentum", "mean_reversion", "crypto_trend", "cash"
SPY, BTC = "SPY", "BTC/USD"
JAN2, JAN3, JAN4, JAN5 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)
JAN31, FEB1, FEB2 = date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 2)


# ------------------------------------------------------------------ weight-sum tolerance


def _tickers(k: int) -> list[str]:
    letters = "ABCDEFGHIJ"
    return [f"Q{letters[i // 10]}{letters[i % 10]}" for i in range(k)]


@pytest.mark.parametrize("k", range(1, 41))
def test_equal_weights_one_over_k_are_accepted(k: int) -> None:
    eng = PortfolioEngine(default_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    eng.submit_target_weights(CORE, {s: 1 / k for s in _tickers(k)}, JAN2)  # must not raise


def test_weights_above_one_still_raise() -> None:
    eng = PortfolioEngine(default_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    with pytest.raises(ValueError, match="sum to at most 1"):
        eng.submit_target_weights(CORE, {SPY: 1.01}, JAN2)


# ------------------------------------------------------------------ F1: strategy reuse

_COLS = ["open", "high", "low", "close", "volume"]


def _fixture_bars() -> dict[str, pd.DataFrame]:
    dest = Path(__file__).resolve().parents[1] / "fixtures" / "market"
    out: dict[str, pd.DataFrame] = {}
    for path in sorted(dest.glob("*/*/gen-*/bars.parquet")):
        out[path.parents[1].name.replace("_", "/")] = pd.read_parquet(path)[_COLS]
    return out


@pytest.mark.parametrize("name", ["mean_reversion", "core_momentum", "crypto_trend"])
def test_compiled_strategy_reuse_across_windows_matches_fresh(name: str) -> None:
    spec = STRATEGIES[name]
    all_bars = _fixture_bars()
    shared = compile_strategy(spec)
    req = shared.required
    full = {s: all_bars[s] for s in req}
    short = {s: df.iloc[:-300] for s, df in full.items()}

    def run(strat, feats, cal, ts):  # type: ignore[no-untyped-def]
        held = {s: HeldPosition(symbol=s, entry_ts=cal[ts - 3]) for s in strat.universe[:2]}
        t = cal[ts]
        sig = strat.evaluate(feats, t, held, force_decision=True).model_dump()
        return sig, strat.is_decision_bar(t, feats)

    f_full = shared.prepare(full)
    f_short = shared.prepare(short)  # overwrites the instance's last-prepared state
    for feats in (f_full, f_short):
        cal = feats.calendar
        for ts in (len(cal) - 1, len(cal) // 2, 400):
            fresh = compile_strategy(spec)
            fresh_feats = fresh.prepare(full if feats is f_full else short)
            assert run(shared, feats, cal, ts) == run(fresh, fresh_feats, cal, ts)
    # The default (no feats) path still follows the last prepare, as before.
    t = f_short.calendar[-1]
    assert shared.is_decision_bar(t) == shared.is_decision_bar(t, f_short)


# ------------------------------------------------------------------ F2: pending BUY cancelled


def test_flat_target_cancels_pending_buy_for_missing_bar() -> None:
    eng = PortfolioEngine(default_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    buy = eng.submit_target_weights(CORE, {SPY: 0.2}, JAN2).orders[0]
    eng.process_bar(JAN3, book(bar(BTC, 100.0, 100.0)))  # no SPY bar: BUY keeps waiting
    assert eng.get_order(buy.order_id).status is OrderStatus.PENDING
    res = eng.submit_target_weights(CORE, {}, JAN3)
    assert res.orders == []
    assert eng.get_order(buy.order_id).status is OrderStatus.CANCELLED
    r = eng.process_bar(JAN4, book(bar(SPY, 100.0, 100.0)))
    assert r.fills == []
    eng.verify()


def test_reduced_target_cancels_pending_buy_and_sells() -> None:
    eng = PortfolioEngine(default_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    eng.submit_target_weights(CORE, {SPY: 0.2}, JAN2)  # 100 sh
    eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0)))
    buy = eng.submit_target_weights(CORE, {SPY: 0.3}, JAN3).orders[0]  # +50 pending
    eng.process_bar(JAN4, book(bar(BTC, 100.0, 100.0)))
    res = eng.submit_target_weights(CORE, {SPY: 0.1}, JAN4)  # target 50 < holding 100
    assert eng.get_order(buy.order_id).status is OrderStatus.CANCELLED
    # Sleeve equity is a hair under 50000 after slippage, so the target is ~50 sh: sell ~50.
    assert [o.side for o in res.orders] == [Side.SELL]
    assert Decimal(49) <= res.orders[0].qty_requested <= Decimal(51)


def test_target_above_holding_keeps_pending_buy() -> None:
    eng = PortfolioEngine(default_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    buy = eng.submit_target_weights(CORE, {SPY: 0.2}, JAN2).orders[0]
    eng.process_bar(JAN3, book(bar(BTC, 100.0, 100.0)))
    res = eng.submit_target_weights(CORE, {SPY: 0.2}, JAN3)
    assert res.orders == []
    assert eng.get_order(buy.order_id).status is OrderStatus.PENDING


# ------------------------------------------------------------------ F4: restore validation


def _killed_state() -> EngineState:
    eng = PortfolioEngine(default_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    eng.submit_target_weights(CORE, {SPY: 0.5}, JAN2)
    eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0)))
    eng.process_bar(JAN4, book(bar(SPY, 20.0, 20.0)))
    assert eng.snapshot().killed
    return eng.export_state()


def _transfer_state() -> EngineState:
    eng = PortfolioEngine(default_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    eng.submit_target_weights(CORE, {SPY: 0.5}, JAN2)
    eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0)))
    eng.process_bar(JAN31, book(bar(SPY, 120.0, 120.0)))
    eng.process_bar(FEB1, book(bar(SPY, 120.0, 120.0)))
    eng.submit_target_weights(CORE, {SPY: 0.5}, FEB1)
    eng.process_bar(FEB2, book(bar(SPY, 120.0, 120.0)))
    st = eng.export_state()
    assert any(e.type is EventType.CASH_TRANSFER_OUT for e in st.ledger)
    return st


def test_untampered_states_restore() -> None:
    for st in (_killed_state(), _transfer_state()):
        PortfolioEngine(st.config, st).verify()


def test_restore_rejects_lowered_next_order_n() -> None:
    st = _killed_state()
    assert st.next_order_n > 1
    with pytest.raises(StateCorruptionError, match="next_order_n"):
        PortfolioEngine(st.config, st.model_copy(update={"next_order_n": 1}))


def test_restore_rejects_lowered_next_transfer_n() -> None:
    st = _transfer_state()
    assert st.next_transfer_n > 0
    with pytest.raises(StateCorruptionError, match="next_transfer_n"):
        PortfolioEngine(st.config, st.model_copy(update={"next_transfer_n": 0}))


def test_restore_rejects_killed_flag_flipped() -> None:
    st = _killed_state()
    with pytest.raises(StateCorruptionError, match="killed"):
        PortfolioEngine(
            st.config, st.model_copy(update={"kill_state": {**st.kill_state, "killed": False}})
        )
    ok = _transfer_state()
    with pytest.raises(StateCorruptionError, match="killed"):
        PortfolioEngine(
            ok.config, ok.model_copy(update={"kill_state": {**ok.kill_state, "killed": True}})
        )


def test_restore_rejects_lowered_peak() -> None:
    st = _killed_state()
    with pytest.raises(StateCorruptionError, match="peak_equity"):
        PortfolioEngine(
            st.config, st.model_copy(update={"kill_state": {**st.kill_state, "peak_equity": "1"}})
        )


def test_restore_rejects_last_asof_before_last_event() -> None:
    st = _killed_state()
    with pytest.raises(StateCorruptionError, match="last_processed_asof"):
        PortfolioEngine(st.config, st.model_copy(update={"last_processed_asof": JAN2}))


# ------------------------------------------------------------------ F5: atr14 <= 0


@pytest.mark.parametrize("atr", [Decimal(0), Decimal("-1")])
def test_non_positive_atr_leaves_level_unchanged(atr: Decimal) -> None:
    first = update_stop(None, Decimal(100), atr, Decimal(2))
    assert first.levels.stop_level is None and not first.triggered
    prev = update_stop(None, Decimal(100), Decimal(2), Decimal(2)).levels  # stop 96
    nxt = update_stop(prev, Decimal(110), atr, Decimal(2))
    assert nxt.levels.stop_level == prev.stop_level and not nxt.triggered


def test_engine_zero_atr_on_fill_bar_does_not_stop_out() -> None:
    from test_scenarios import make_config  # type: ignore[import-not-found]

    eng = PortfolioEngine(make_config(mr_stop_mult="2.5"))
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0, atr14=0.0)))
    eng.submit_target_weights(MR, {SPY: 0.5}, JAN2)
    r = eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0, atr14=0.0)))
    assert r.fills and r.stop_orders == []


# ------------------------------------------------------------------ F6: lock re-armed on success


def test_failed_submit_does_not_rearm_stop_lock() -> None:
    from test_scenarios import make_config  # type: ignore[import-not-found]

    eng = PortfolioEngine(make_config(mr_stop_mult="2.5"))
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0, atr14=2.0)))
    eng.submit_target_weights(MR, {SPY: 0.9}, JAN2)
    eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0, atr14=2.0)))
    eng.process_bar(JAN4, book(bar(SPY, 100.0, 95.0, atr14=2.0)))  # stop triggers
    assert eng.export_state().stop_state["stopped_out"] == {MR: [SPY]}
    # BTC is outside the mean-reversion sleeve's asset classes: the call raises.
    eng.process_bar(JAN5, book(bar(SPY, 94.0, 94.0, atr14=2.0), bar(BTC, 100.0, 100.0)))
    with pytest.raises(ValueError, match="allowed asset classes"):
        eng.submit_target_weights(MR, {BTC: 0.5}, JAN5)
    assert eng.export_state().stop_state["stopped_out"] == {MR: [SPY]}
    eng.submit_target_weights(MR, {}, JAN5)  # a successful flat call re-arms
    assert eng.export_state().stop_state["stopped_out"] == {}


# ------------------------------------------------------------------ F8: low-level kill guard


def test_submit_orders_refuses_buys_while_killed_but_allows_sells() -> None:
    eng = PortfolioEngine(default_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    eng.submit_target_weights(CORE, {SPY: 0.5}, JAN2)
    eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0)))
    eng.process_bar(JAN4, book(bar(SPY, 20.0, 20.0)))
    assert eng.snapshot().killed

    def req(side: Side) -> OrderRequest:
        return OrderRequest(
            sleeve_id=CORE, symbol=SPY, side=side, qty=Decimal(10), origin=OrderOrigin.MANUAL
        )

    with pytest.raises(ValueError, match="kill switch"):
        eng.submit_orders([req(Side.BUY)], JAN4)
    assert [o.side for o in eng.submit_orders([req(Side.SELL)], JAN4)] == [Side.SELL]


# ------------------------------------------------------------------ F9: symbols at the boundary


def test_process_bar_rejects_key_symbol_mismatch_and_noncanonical() -> None:
    eng = PortfolioEngine(default_config())
    with pytest.raises(ValueError, match="holds a bar for"):
        eng.process_bar(JAN2, {SPY: bar("QQQ", 1.0, 1.0)})
    with pytest.raises(ValueError, match="not canonical"):
        eng.process_bar(JAN2, {"spy": bar("spy", 1.0, 1.0)})
    with pytest.raises(ValueError, match="not canonical"):
        eng.process_bar(JAN2, {"BTC-USD": bar("BTC-USD", 1.0, 1.0)})
    assert eng.last_processed_asof is None  # nothing was mutated


def test_submit_target_weights_rejects_noncanonical_symbol() -> None:
    eng = PortfolioEngine(default_config())
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    with pytest.raises(ValueError, match="not canonical"):
        eng.submit_target_weights(CORE, {"spy": 0.1}, JAN2)


# ------------------------------------------------------------------ Wave 4 hooks


def test_wave4_hooks() -> None:
    eng = PortfolioEngine(default_config())
    assert eng.last_processed_asof is None and eng.orders() == []
    eng.process_bar(JAN2, book(bar(SPY, 100.0, 100.0)))
    assert eng.last_processed_asof == JAN2
    o = eng.submit_target_weights(CORE, {SPY: 0.2}, JAN2).orders[0]
    eng.process_bar(JAN3, book(bar(SPY, 100.0, 100.0)))
    assert eng.get_order(o.order_id).status is OrderStatus.FILLED
    assert [x.order_id for x in eng.orders()] == [o.order_id]  # terminal orders included
    with pytest.raises(KeyError):
        eng.get_order("O99999999")
