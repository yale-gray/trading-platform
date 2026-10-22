"""Risk properties (SPEC-003 sections 4, 6, 7; invariants I10-I13; audit gap G9).

Kill switch, trailing stop and the engine's target-weight checks, against independent
expectations: exact rational drawdown test, running peak, stop ratchet, repeat-call
idempotency, cash reservation across pending BUYs and the crypto/asset caps measured
against *held* positions as well as pending ones.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from support.strategies import CRYPTOS, EQUITIES, order_plan

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import EngineConfig, SleeveConfig, default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.risk.killswitch import KillSwitch
from tradebot.risk.stops import StopBook, StopLevels, update_stop
from tradebot.sim.broker import Bar
from tradebot.sim.orders import OrderStatus, Side

pytestmark = pytest.mark.property

D = Decimal
CAP = D("100000.00")
START = date(2024, 3, 4)  # a Monday
cents_equity = st.integers(1_00, 400_000_00).map(lambda c: D(c) / 100)


# ------------------------------------------------------------------------ kill switch


@settings(max_examples=100)
@given(
    path=st.lists(cents_equity, min_size=1, max_size=40),
    threshold=st.sampled_from((D("0.20"), D("0.10"), D("0.35"))),
    pause=st.sampled_from((1, 3, 20)),
    reset_at=st.sets(st.integers(0, 39), max_size=4),
)
def test_killswitch_trips_iff_drawdown_and_pauses_then_rearms(
    path: list[Decimal], threshold: D, pause: int, reset_at: set[int]
) -> None:
    """I12 + A18: exact trip rule, running peak, sticky for N bars then re-armed, manual reset."""
    ks = KillSwitch(CAP, threshold, pause)
    peak, killed, paused = CAP, False, 0
    for i, eq in enumerate(path):
        asof = START + timedelta(days=i)
        if killed:
            paused += 1
            obs = ks.observe(eq, asof)
            assert not obs.tripped_now
            if paused >= pause:
                killed, peak, paused = False, eq, 0
                assert obs.rearmed_now and not obs.killed
            else:
                assert not obs.rearmed_now and obs.killed
        else:
            peak = max(peak, eq)
            obs = ks.observe(eq, asof)
            trips = Fraction(1) - Fraction(eq) / Fraction(peak) >= Fraction(threshold)
            assert obs.tripped_now == trips and not obs.rearmed_now
            killed, paused = trips, 0
        assert ks.killed == killed == obs.killed
        assert ks.peak_equity == peak == obs.peak_equity
        assert ks.allows_buy() == (not killed) and ks.rebalance_frozen == killed
        if i in reset_at:
            if killed:
                ks.reset("op", "n", asof)
                killed, paused = False, 0
                peak = eq.quantize(D("0.01"), rounding=ROUND_HALF_EVEN)
                assert ks.peak_equity == peak
            else:
                with pytest.raises(ValueError, match="not tripped"):
                    ks.reset("op", "n", asof)
        rebuilt = KillSwitch.from_state(ks.to_state())
        assert rebuilt.to_state() == ks.to_state()  # persistence round trip


# ------------------------------------------------------------------------ stops


@settings(max_examples=100)
@given(
    closes=st.lists(st.integers(1, 10**7).map(lambda c: D(c) / 1000), min_size=1, max_size=40),
    atrs=st.lists(
        st.one_of(st.none(), st.integers(0, 10**6).map(lambda c: D(c) / 1000)),
        min_size=40,
        max_size=40,
    ),
    mult=st.sampled_from((D("1.5"), D("2.5"), D("10"))),
)
def test_trailing_stop_ratchets_and_triggers_at_or_below(
    closes: list[Decimal], atrs: list[Decimal | None], mult: Decimal
) -> None:
    """I13: the level never decreases, never exceeds the high-water close, is >= 0."""
    prev: StopLevels | None = None
    book = StopBook()
    for close, atr in zip(closes, atrs, strict=False):
        upd = update_stop(prev, close, atr, mult)
        book.on_close("s", "SPY", close, atr, mult)
        lv = upd.levels
        assert lv.high_water_close >= close
        if prev is not None:
            assert lv.high_water_close >= prev.high_water_close
            if prev.stop_level is not None:
                assert lv.stop_level is not None and lv.stop_level >= prev.stop_level
        if lv.stop_level is not None:
            assert 0 <= lv.stop_level <= lv.high_water_close
        assert upd.triggered == (lv.stop_level is not None and close <= lv.stop_level)
        assert StopBook.from_state(book.to_state()).to_state() == book.to_state()
        prev = lv


# ------------------------------------------------------------------------ engine helpers


def _bar(sym: str, price: float, vol20: float = 0.02) -> Bar:
    return Bar(symbol=sym, open=price, high=price, low=price, close=price, vol20=vol20, atr14=None)


def _crypto_heavy_config() -> EngineConfig:
    """A 90% crypto sleeve, so the 30% crypto and 25% per-asset caps actually bind."""
    base = default_config()
    crypto = frozenset({AssetClass.CRYPTO})
    return base.model_copy(
        update={
            "sleeves": (
                SleeveConfig(
                    sleeve_id="crypto_trend",
                    name="Crypto",
                    target_weight=D("0.90"),
                    vol_scaled=True,
                    allowed_asset_classes=crypto,
                ),
                SleeveConfig(
                    sleeve_id="cash",
                    name="Cash",
                    target_weight=D("0.10"),
                    vol_scaled=False,
                    allowed_asset_classes=frozenset(),
                ),
            )
        }
    )


prices = st.floats(min_value=0.5, max_value=60000, allow_nan=False)


def _pending(eng: PortfolioEngine) -> list:
    return [o for o in eng.orders() if o.status is OrderStatus.PENDING]


# ------------------------------------------------------------------------ idempotency


@settings(max_examples=100)
@given(
    eq_prices=st.lists(prices, min_size=3, max_size=3),
    cr_prices=st.lists(prices, min_size=2, max_size=2),
    plans=st.fixed_dictionaries(
        {
            "core_momentum": order_plan(EQUITIES),
            "mean_reversion": order_plan(EQUITIES),
            "crypto_trend": order_plan(CRYPTOS),
        }
    ),
    second=st.lists(st.floats(0.9, 1.1), min_size=5, max_size=5),
)
def test_repeat_target_call_is_a_no_op(
    eq_prices: list[float], cr_prices: list[float], plans: dict, second: list[float]
) -> None:
    """Spec 4: submitting the same targets again, with orders still pending, adds nothing.

    Holds both on the first bar (nothing held) and after a fill bar (positions held).
    """
    eng = PortfolioEngine(default_config())
    syms = EQUITIES + CRYPTOS
    base = dict(zip(syms, eq_prices + cr_prices, strict=True))
    for day, scale in ((START, [1.0] * 5), (START + timedelta(days=1), second)):
        bars = {s: _bar(s, p * k) for (s, p), k in zip(base.items(), scale, strict=True)}
        eng.process_bar(day, bars)
        for sid, w in plans.items():
            eng.submit_target_weights(sid, w, day)
        pend_before = _pending(eng)
        ledger_before = len(eng.ledger)
        for sid, w in plans.items():
            again = eng.submit_target_weights(sid, w, day)
            assert again.orders == [], (sid, w, again.orders)
        assert _pending(eng) == pend_before and len(eng.ledger) == ledger_before


# ------------------------------------------------------------------------ cash reservation


@settings(max_examples=100)
@given(
    p_btc=st.floats(100, 60000),
    p_eth=st.floats(5, 5000),
    calls=st.lists(order_plan(CRYPTOS), min_size=2, max_size=4),
)
def test_pending_buys_never_over_commit_sleeve_cash(
    p_btc: float, p_eth: float, calls: list[dict[str, float]]
) -> None:
    """Spec 3: across several submit calls, reserved cash (qty*ref*(1+fee+slip)) <= sleeve cash."""
    cfg = default_config()
    eng = PortfolioEngine(cfg)
    eng.process_bar(START, {"BTC/USD": _bar("BTC/USD", p_btc), "ETH/USD": _bar("ETH/USD", p_eth)})
    closes = {"BTC/USD": D(repr(p_btc)), "ETH/USD": D(repr(p_eth))}
    unit = (D(10) + D(10)) / D(10000) + 1  # crypto fee 10 bps + slip 10 bps
    for w in calls:
        eng.submit_target_weights("crypto_trend", w, START)
        cash = next(s.cash for s in eng.snapshot().sleeves if s.sleeve_id == "crypto_trend")
        reserved = sum(
            (
                o.qty_requested * closes[o.symbol] * unit
                for o in _pending(eng)
                if o.side is Side.BUY and o.sleeve_id == "crypto_trend"
            ),
            D(0),
        )
        assert reserved <= cash + D("0.01"), (reserved, cash)


# ------------------------------------------------------------------------ caps vs held


@settings(max_examples=100)
@given(
    p_btc=st.floats(1000, 60000),
    p_eth=st.floats(50, 5000),
    moves=st.lists(st.floats(0.5, 1.6), min_size=4, max_size=4),
    plans=st.lists(order_plan(CRYPTOS), min_size=2, max_size=2),
)
def test_caps_hold_against_held_and_pending_positions(
    p_btc: float, p_eth: float, moves: list[float], plans: list[dict[str, float]]
) -> None:
    """I10 / R4: after any accepted BUY the asset and crypto caps hold, held positions included."""
    cfg = _crypto_heavy_config()
    eng = PortfolioEngine(cfg)
    slack = D("0.05")
    for i, plan in enumerate(plans):
        day = START + timedelta(days=i)
        btc, eth = p_btc * moves[2 * i], p_eth * moves[2 * i + 1]
        eng.process_bar(day, {"BTC/USD": _bar("BTC/USD", btc), "ETH/USD": _bar("ETH/USD", eth)})
        closes = {"BTC/USD": D(repr(btc)), "ETH/USD": D(repr(eth))}
        tr = eng.submit_target_weights("crypto_trend", plan, day)
        snap = eng.snapshot()
        pending = _pending(eng)

        def expo(sym: str, snap=snap, pending=pending, closes=closes) -> Decimal:
            held = sum((p.mv for s in snap.sleeves for p in s.positions if p.symbol == sym), D(0))
            buying = sum(
                (o.qty_requested for o in pending if o.side is Side.BUY and o.symbol == sym), D(0)
            )
            return held + buying * closes[sym]

        bought = {o.symbol for o in tr.orders if o.side is Side.BUY}
        for sym in bought:
            assert expo(sym) <= cfg.risk.max_asset_fraction * snap.equity + slack
        if bought:
            total = expo("BTC/USD") + expo("ETH/USD")
            assert total <= cfg.risk.max_crypto_fraction * snap.equity + slack


# ------------------------------------------------------------------------ exits are never vetoed


@settings(max_examples=30)
@given(
    price=st.floats(5, 500),
    weight=st.floats(0.2, 1.0),
    crash=st.booleans(),
)
def test_exit_target_sells_the_whole_position_even_when_killed(
    price: float, weight: float, crash: bool
) -> None:
    """R0/R3-R5 never veto a SELL: a zero target sells all that is held, killed or not."""
    cfg = default_config().model_copy(
        update={
            "risk": default_config().risk.model_copy(update={"kill_switch_drawdown": D("0.01")})
        }
    )
    eng = PortfolioEngine(cfg)
    eng.process_bar(START, {"SPY": _bar("SPY", price)})
    eng.submit_target_weights("core_momentum", {"SPY": weight}, START)
    day2 = START + timedelta(days=1)
    eng.process_bar(day2, {"SPY": _bar("SPY", price)})
    held = {p.symbol: p.qty for s in eng.snapshot().sleeves for p in s.positions}
    day3 = START + timedelta(days=2)
    eng.process_bar(day3, {"SPY": _bar("SPY", price * (0.5 if crash else 1.0))})
    held = {p.symbol: p.qty for s in eng.snapshot().sleeves for p in s.positions}
    tr = eng.submit_target_weights("core_momentum", {}, day3)
    if "SPY" in held:
        sells = [o for o in tr.orders if o.side is Side.SELL]
        assert sum((o.qty_requested for o in sells), D(0)) == held["SPY"]
    assert not any(o.side is Side.BUY for o in tr.orders)
