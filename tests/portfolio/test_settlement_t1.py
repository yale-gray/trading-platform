"""T+1 cash-account settlement (ADR-0001 A21): SELL proceeds fund BUYs from the next trading day.

Ledger level (``LedgerState.unsettled`` / ``spendable``), engine level (BUY sizing from settled
cash, fills, snapshots) and persistence (``EngineState`` carries and re-checks the unsettled
cash). Every expected number is worked out by hand in a comment.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import CostConfig, EngineConfig, SleeveConfig, default_config
from tradebot.portfolio.engine import PortfolioEngine, StateCorruptionError
from tradebot.portfolio.ledger import Ledger, LedgerError, replay
from tradebot.portfolio.models import EventType, FillSide, Stage
from tradebot.portfolio.state import EngineState
from tradebot.risk.config import RiskConfig
from tradebot.sim.broker import Bar
from tradebot.sim.orders import OrderReason, OrderRequest, Side

pytestmark = pytest.mark.unit

D = Decimal
S = "main"


def utc(day: int, hh: int, mm: int, month: int = 1) -> datetime:
    return datetime(2024, month, day, hh, mm, tzinfo=UTC)


def _ledger(t1: bool = True) -> Ledger:
    led = Ledger(t1_settlement=t1)
    led.append(
        asof=date.min, stage=Stage.SUBMIT, sleeve_id=S, type=EventType.DEPOSIT, amount=D("10000.00")
    )
    return led


def _fill(led: Ledger, asof, side: FillSide, sym: str, qty: str, px: str, oid: str, fee="0.00"):  # noqa: ANN001, ANN202
    return led.append_fill(
        asof=asof,
        stage=Stage.OPEN_FILL,
        sleeve_id=S,
        symbol=sym,
        side=side,
        qty=D(qty),
        price=D(px),
        order_id=oid,
        fee=D(fee),
    )


# ------------------------------------------------------------------ ledger fold


def test_sale_proceeds_are_unsettled_until_the_next_trading_day() -> None:
    led = _ledger()
    mon = utc(8, 15, 0)  # Mon 2024-01-08 10:00 New York
    _fill(led, mon, FillSide.BUY, "AAA", "10", "100", "O1")  # cash 10000 - 1000 = 9000
    assert led.state_view.unsettled(S, mon) == D("0.00")
    _fill(led, utc(8, 18, 0), FillSide.SELL, "AAA", "10", "110", "O2")  # +1100 -> cash 10100
    st = led.state_view
    assert st.cash[S] == D("10100.00")
    assert st.unsettled(S, utc(8, 18, 15)) == D("1100.00")  # the same trading day
    assert st.spendable(S, utc(8, 18, 15)) == D("9000.00")  # 10100 - 1100
    # Next trading day (Tue): settled.
    assert st.unsettled(S, utc(9, 14, 30)) == D("0.00")
    assert st.spendable(S, utc(9, 14, 30)) == D("10100.00")


def test_buy_above_settled_cash_is_refused_and_within_it_is_booked() -> None:
    led = _ledger()
    _fill(led, utc(8, 15, 0), FillSide.BUY, "AAA", "10", "100", "O1")
    _fill(led, utc(8, 18, 0), FillSide.SELL, "AAA", "10", "110", "O2")
    # settled = 10100 - 1100 = 9000. 91 x 100 = 9100 > 9000: refused, nothing booked.
    n = len(led)
    with pytest.raises(LedgerError, match="settled cash"):
        _fill(led, utc(8, 18, 15), FillSide.BUY, "BBB", "91", "100", "O3")
    assert len(led) == n
    # 90 x 100 = 9000 uses exactly the settled cash.
    _fill(led, utc(8, 18, 15), FillSide.BUY, "BBB", "90", "100", "O4")
    assert led.state_view.cash[S] == D("1100.00")
    assert led.state_view.spendable(S, utc(8, 18, 30)) == D("0.00")
    # Tuesday the 1100 has settled and can be spent: 11 x 100.
    _fill(led, utc(9, 14, 30), FillSide.BUY, "CCC", "11", "100", "O5")
    assert led.state_view.cash[S] == D("0.00")


def test_without_t1_the_same_buy_is_fine() -> None:
    led = _ledger(t1=False)
    _fill(led, utc(8, 15, 0), FillSide.BUY, "AAA", "10", "100", "O1")
    _fill(led, utc(8, 18, 0), FillSide.SELL, "AAA", "10", "110", "O2")
    _fill(led, utc(8, 18, 15), FillSide.BUY, "BBB", "101", "100", "O3")  # 10100 - 10100 = 0
    assert led.state_view.cash[S] == D("0.00")
    assert led.state_view.unsettled(S, utc(8, 18, 15)) == D("0.00")


def test_a_sell_fee_comes_out_of_its_own_unsettled_lot() -> None:
    led = _ledger()
    _fill(led, utc(8, 15, 0), FillSide.BUY, "AAA", "10", "100", "O1")
    _fill(led, utc(8, 18, 0), FillSide.SELL, "AAA", "10", "110", "O2", fee="2.00")
    # cash 9000 + 1100 - 2 = 10098; unsettled lot = 1100 - 2 = 1098; settled 9000.
    st = led.state_view
    assert st.cash[S] == D("10098.00")
    assert st.unsettled(S, utc(8, 19, 0)) == D("1098.00")
    assert st.spendable(S, utc(8, 19, 0)) == D("9000.00")


def test_friday_sale_settles_on_the_next_session_across_weekend_and_holiday() -> None:
    led = _ledger()
    fri = utc(12, 19, 45)  # Fri 2024-01-12, 14:45 New York
    _fill(led, utc(12, 15, 0), FillSide.BUY, "AAA", "10", "100", "O1")
    _fill(led, fri, FillSide.SELL, "AAA", "10", "100", "O2")
    st = led.state_view
    assert st.unsettled(S, fri) == D("1000.00")
    # Mon 2024-01-15 is a holiday (no bars), Tue the 16th is the next session: settled.
    assert st.unsettled(S, utc(16, 14, 30)) == D("0.00")
    # Any later instant still reads settled, and an earlier one does not.
    assert st.unsettled(S, utc(12, 20, 0)) == D("1000.00")


def test_trade_day_is_the_new_york_day_not_the_utc_day() -> None:
    led = _ledger()
    _fill(led, utc(8, 15, 0), FillSide.BUY, "AAA", "10", "100", "O1")
    # 2024-01-09 01:00 UTC is still Mon 20:00 in New York: the sale belongs to Monday.
    _fill(led, utc(9, 1, 0), FillSide.SELL, "AAA", "10", "100", "O2")
    assert led.state_view.unsettled(S, utc(9, 1, 5)) == D("1000.00")
    assert led.state_view.unsettled(S, utc(9, 14, 30)) == D("0.00")  # Tue 09:30 New York


def test_daily_dates_settle_one_day_later_too() -> None:
    led = _ledger()
    _fill(led, date(2024, 1, 8), FillSide.BUY, "AAA", "10", "100", "O1")
    _fill(led, date(2024, 1, 9), FillSide.SELL, "AAA", "10", "100", "O2")
    assert led.state_view.unsettled(S, date(2024, 1, 9)) == D("1000.00")
    assert led.state_view.unsettled(S, date(2024, 1, 10)) == D("0.00")


def test_transfers_out_may_only_move_settled_cash() -> None:
    led = Ledger(t1_settlement=True)
    for sid in (S, "other"):
        led.append(
            asof=date.min,
            stage=Stage.SUBMIT,
            sleeve_id=sid,
            type=EventType.DEPOSIT,
            amount=D("5000.00"),
        )
    _fill(led, utc(8, 15, 0), FillSide.BUY, "AAA", "10", "100", "O1")  # cash 4000
    _fill(led, utc(8, 18, 0), FillSide.SELL, "AAA", "10", "100", "O2")  # cash 5000, 1000 unsettled
    with pytest.raises(LedgerError, match="settled cash"):
        led.append_transfer(utc(8, 18, 15), Stage.CLOSE, S, "other", D("4500.00"), "T000001")
    led.append_transfer(utc(8, 18, 15), Stage.CLOSE, S, "other", D("4000.00"), "T000001")
    assert led.state_view.cash[S] == D("1000.00")


def test_replay_rebuilds_the_same_unsettled_cash() -> None:
    led = _ledger()
    _fill(led, utc(8, 15, 0), FillSide.BUY, "AAA", "10", "100", "O1")
    _fill(led, utc(8, 18, 0), FillSide.SELL, "AAA", "10", "110", "O2")
    again = replay(led.events, None, True)
    assert again.unsettled(S, utc(8, 19, 0)) == D("1100.00")
    assert again.cash == led.state_view.cash


# ------------------------------------------------------------------ engine


def _cfg(*, flatten: bool = False) -> EngineConfig:
    """$10,000, one equity sleeve, zero fees and slippage so every price is the bar price."""
    base = default_config()
    return EngineConfig(
        initial_capital=D("10000.00"),
        sleeves=(
            SleeveConfig(
                sleeve_id=S,
                name="main",
                target_weight=D(1),
                vol_scaled=False,
                allowed_asset_classes=frozenset({AssetClass.EQUITY}),
            ),
        ),
        risk=RiskConfig(
            max_asset_fraction=D(1), max_crypto_fraction=D(1), kill_switch_drawdown=D(1)
        ),
        costs={
            AssetClass.EQUITY: CostConfig(
                asset_class=AssetClass.EQUITY, fee_bps=D(0), slip_bps=D(0)
            ),
            AssetClass.CRYPTO: base.costs[AssetClass.CRYPTO],
        },
        settlement="t1",
        intraday_flatten=flatten,
    )


def _bar(o: float, c: float | None = None) -> dict[str, Bar]:
    c = o if c is None else c
    return {"AAA": Bar(symbol="AAA", open=o, high=max(o, c), low=min(o, c), close=c)}


T1, T2, T3, T4 = utc(8, 14, 30), utc(8, 14, 45), utc(8, 15, 0), utc(8, 15, 15)
NEXT_DAY = utc(9, 14, 30)


def _run_to_unsettled_state() -> PortfolioEngine:
    eng = PortfolioEngine(_cfg())
    eng.process_bar(T1, _bar(100))
    eng.submit_target_weights(S, {"AAA": 0.5}, T1)  # 0.5 x 10000 / 100 = 50 shares
    eng.process_bar(T2, _bar(100))  # BUY 50 fills at 100: cash 5000
    eng.submit_target_weights(S, {}, T2)  # exit
    eng.process_bar(T3, _bar(102))  # SELL 50 fills at 102: +5100 -> cash 10100, 5100 unsettled
    return eng


def test_buy_is_sized_from_settled_cash_only() -> None:
    eng = _run_to_unsettled_state()
    snap = eng.snapshot()
    assert snap.cash == D("10100.00") and snap.unsettled == D("5100.00")
    assert snap.sleeves[0].unsettled == D("5100.00")
    # Target 100% = 10100 / 102 = 99.0196 shares, but only 10100 - 5100 = 5000 is settled:
    # 5000 / 102 = 49.0196078 -> 49.019607 (1e-6 grid, rounded down).
    res = eng.submit_target_weights(S, {"AAA": 1.0}, T3)
    assert [o.qty_requested for o in res.orders] == [D("49.019607")]
    assert [r.reason for r in res.rejections] == [OrderReason.CASH_SHRUNK]
    r4 = eng.process_bar(T4, _bar(102))  # fills: q2(49.019607 x 102) = 5000.00 (4999.99991...)
    assert r4.fills[0].qty_filled == D("49.019607")
    assert r4.snapshot.cash == D("5100.00")  # 10100 - 5000
    assert r4.snapshot.unsettled == D("5100.00")  # still today's sale proceeds: nothing spendable
    # With nothing settled left, a new BUY target is vetoed outright.
    again = eng.submit_target_weights(S, {"AAA": 1.0, "BBB": 0.0}, T4)
    assert again.orders == []
    eng.verify()


def test_next_session_the_proceeds_are_spendable() -> None:
    eng = _run_to_unsettled_state()
    r = eng.process_bar(NEXT_DAY, _bar(102))
    assert r.snapshot.unsettled == D("0.00") and r.snapshot.cash == D("10100.00")
    # 100% target now sizes from all 10100: 10100 / 102 = 99.019607 (rounded down).
    res = eng.submit_target_weights(S, {"AAA": 1.0}, NEXT_DAY)
    assert [o.qty_requested for o in res.orders] == [D("99.019607")]
    assert res.rejections == []


def test_broker_shrinks_a_buy_to_the_settled_cash_at_fill_time() -> None:
    eng = _run_to_unsettled_state()
    # Low-level order (bypasses the risk sizing): 60 shares = 6120 at 102 > 5000 settled.
    eng.submit_orders([OrderRequest(sleeve_id=S, symbol="AAA", side=Side.BUY, qty=D(60))], T3)
    r = eng.process_bar(T4, _bar(102))
    # Largest 1e-6 quantity with q2(qty x 102) <= 5000.00 is 49.019607 (x 102 = 4999.999914).
    assert r.fills[0].qty_filled == D("49.019607")
    assert r.fills[0].reason is OrderReason.SHRUNK_CASH
    assert r.snapshot.cash == D("5100.00")
    eng.verify()


# ------------------------------------------------------------------ persistence


def test_state_round_trips_and_restores_the_unsettled_cash() -> None:
    eng = _run_to_unsettled_state()
    state = eng.export_state()
    assert state.unsettled == {S: D("5100.00")}
    text = state.to_json()
    assert '"unsettled":{"main":"5100.00"}' in text
    restored = PortfolioEngine(_cfg(), state=EngineState.from_json(text))
    assert restored.snapshot().unsettled == D("5100.00")
    assert restored.export_state().to_json() == text
    # And it behaves identically afterwards.
    a = eng.submit_target_weights(S, {"AAA": 1.0}, T3)
    b = restored.submit_target_weights(S, {"AAA": 1.0}, T3)
    assert [o.qty_requested for o in a.orders] == [o.qty_requested for o in b.orders]


def test_a_tampered_unsettled_amount_is_rejected_on_restore() -> None:
    state = _run_to_unsettled_state().export_state()
    bad = state.model_copy(update={"unsettled": {S: D("1.00")}})
    with pytest.raises(StateCorruptionError, match="unsettled"):
        PortfolioEngine(_cfg(), state=bad)


def test_daily_configs_and_states_are_unchanged_on_the_wire() -> None:
    cfg = default_config()
    dumped = cfg.model_dump(mode="json")
    assert "settlement" not in dumped and "intraday_flatten" not in dumped
    state = PortfolioEngine(cfg).export_state()
    assert state.unsettled == {}
    assert '"unsettled"' not in state.to_json()
    assert "settlement" not in state.to_json()
    # An old state (no new keys) still restores.
    PortfolioEngine(cfg, state=EngineState.from_json(state.to_json()))
