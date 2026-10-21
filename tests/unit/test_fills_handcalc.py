"""Hand-calculated fill, fee and round-trip cases through ``PortfolioEngine`` (SPEC-005 6.3-6.5).

Every expected value is worked out by hand in a comment (SPEC-003 section 2 rounding):

- buy fill   ceil8(open x (1 + slip_bps/10000))   CEILING, 1e-8
- sell fill  floor8(open x (1 - slip_bps/10000))  FLOOR, 1e-8
- notional   q2(qty x price)                       HALF_EVEN, 0.01
- fee        q2_half_up(notional x fee_bps/10000)  HALF_UP, 0.01
- equity buy sizing: whole shares, DOWN; crypto buy sizing: 1e-8, DOWN

Costs are the default config: equity fee 0 / slip 5 bps; crypto fee 10 / slip 10 bps.
Each run goes through the public API only: ``process_bar`` (fills at the open) and
``submit_target_weights`` (sizing). Orders are signalled on bar t and filled at t+1 open.
"""

from datetime import date
from decimal import Decimal

import pytest

from tradebot.portfolio.config import default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType
from tradebot.sim.broker import Bar
from tradebot.sim.orders import Side

pytestmark = pytest.mark.unit

D1, D2, D3, D4 = date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)
CORE = "core_momentum"
CRYPTO = "crypto_trend"


def _bar(symbol: str, open_: float, close: float, vol20: float | None = 0.02) -> Bar:
    return Bar(
        symbol=symbol,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        vol20=vol20,
    )


def _exp(d: Decimal) -> int:
    return d.as_tuple().exponent  # type: ignore[return-value]


def _fills(events: list, kind: EventType = EventType.FILL) -> list:
    return [e for e in events if e.type is kind]


# ---------------------------------------------------------------- 6.3 ETF round trip


def test_etf_buy_fill_price_is_ceiling_of_open_plus_slippage() -> None:
    # Signal on D1 close 100.00: target 0.2 x 50000 = 10000 -> 10000 / 100 = 100 shares.
    # Fill at D2 open 100.00: ceil8(100.00 x 1.0005) = ceil8(100.05000000) = 100.05000000.
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"SPY": _bar("SPY", 100.00, 100.00)})
    res = eng.submit_target_weights(CORE, {"SPY": 0.2}, D1)
    assert len(res.orders) == 1
    assert res.orders[0].qty_requested == Decimal("100")

    r2 = eng.process_bar(D2, {"SPY": _bar("SPY", 100.00, 105.00)})
    buy = r2.fills[0]
    assert buy.side is Side.BUY
    assert buy.qty_filled == Decimal("100")
    assert buy.fill_price == Decimal("100.05000000")
    assert _exp(buy.fill_price) == -8
    eng.verify()


def test_etf_buy_notional_fee_cash_and_bar_end_equity() -> None:
    # notional = q2(100 x 100.05) = q2(10005.00) = 10005.00 (exponent -2)
    # fee = q2_half_up(10005.00 x 0 / 10000) = 0.00
    # cash after buy = 100000.00 - 10005.00 = 89995.00
    # bar-end equity at close 105.00: mv = q2(100 x 105.00) = 10500.00
    #   equity = 89995.00 + 10500.00 = 100495.00
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"SPY": _bar("SPY", 100.00, 100.00)})
    eng.submit_target_weights(CORE, {"SPY": 0.2}, D1)
    r2 = eng.process_bar(D2, {"SPY": _bar("SPY", 100.00, 105.00)})

    (fill_ev,) = _fills(r2.new_ledger_events)
    assert fill_ev.amount == Decimal("10005.00")
    assert _exp(fill_ev.amount) == -2
    buy = r2.fills[0]
    assert buy.fee == Decimal("0.00")
    assert _exp(buy.fee) == -2
    assert r2.snapshot.cash == Decimal("89995.00")
    assert r2.snapshot.equity == Decimal("100495.00")
    eng.verify()


def test_etf_sell_fill_floor_and_round_trip_pnl() -> None:
    # Exit signal on D3 (target 0). Fill at D4 open 110.00:
    # floor8(110.00 x 0.9995) = floor8(109.94500000) = 109.94500000 (exact at 8 dp).
    # notional = q2(100 x 109.945) = q2(10994.500) = 10994.50 ; fee = 0.00
    # cash after sell = 89995.00 + 10994.50 = 100989.50
    # net P&L = 100989.50 - 100000.00 = +989.50 ; no positions left, so equity = cash.
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"SPY": _bar("SPY", 100.00, 100.00)})
    eng.submit_target_weights(CORE, {"SPY": 0.2}, D1)
    eng.process_bar(D2, {"SPY": _bar("SPY", 100.00, 105.00)})

    eng.process_bar(D3, {"SPY": _bar("SPY", 105.00, 110.00)})
    exit_res = eng.submit_target_weights(CORE, {}, D3)
    assert len(exit_res.orders) == 1
    assert exit_res.orders[0].side is Side.SELL
    assert exit_res.orders[0].qty_requested == Decimal("100")

    r4 = eng.process_bar(D4, {"SPY": _bar("SPY", 110.00, 110.00)})
    sell = r4.fills[0]
    assert sell.side is Side.SELL
    assert sell.fill_price == Decimal("109.94500000")
    assert _exp(sell.fill_price) == -8
    (fill_ev,) = _fills(r4.new_ledger_events)
    assert fill_ev.amount == Decimal("10994.50")
    assert _exp(fill_ev.amount) == -2
    assert sell.fee == Decimal("0.00")
    assert r4.snapshot.cash == Decimal("100989.50")
    assert r4.snapshot.equity == Decimal("100989.50")
    assert r4.snapshot.equity - Decimal("100000.00") == Decimal("989.50")
    eng.verify()


# ---------------------------------------------------------------- 6.4 crypto round trip


def test_crypto_buy_fill_notional_and_fee() -> None:
    # Crypto sleeve cash 20000.00; signal D1 close 20000.00; target 0.5 x 20000 = 10000
    # -> 10000 / 20000 = 0.5 BTC (exact on the 1e-8 grid).
    # Fill at D2 open 20000: ceil8(20000 x 1.001) = 20020.00000000.
    # notional = q2(0.5 x 20020) = 10010.00 ; fee = q2_half_up(10010.00 x 10/10000)
    #   = q2_half_up(10.010) = 10.01
    # cash out = 10010.00 + 10.01 = 10020.01 ; portfolio cash = 100000.00 - 10020.01 = 89979.99
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"BTC/USD": _bar("BTC/USD", 20000.0, 20000.0, vol20=0.03)})
    res = eng.submit_target_weights(CRYPTO, {"BTC/USD": 0.5}, D1)
    assert res.orders[0].qty_requested == Decimal("0.5")

    r2 = eng.process_bar(D2, {"BTC/USD": _bar("BTC/USD", 20000.0, 20000.0, vol20=0.03)})
    buy = r2.fills[0]
    assert buy.qty_filled == Decimal("0.5")
    assert buy.fill_price == Decimal("20020.00000000")
    assert _exp(buy.fill_price) == -8
    (fill_ev,) = _fills(r2.new_ledger_events)
    assert fill_ev.amount == Decimal("10010.00")
    assert _exp(fill_ev.amount) == -2
    assert buy.fee == Decimal("10.01")
    assert _exp(buy.fee) == -2
    assert r2.snapshot.cash == Decimal("89979.99")
    eng.verify()


def test_crypto_round_trip_pnl_and_fees() -> None:
    # Exit signal D3; sell fills at D4 open 22000.
    # sell fill = floor8(22000 x 0.999) = floor8(21978.000) = 21978.00000000
    # notional = q2(0.5 x 21978) = 10989.00 ; fee = q2_half_up(10989.00 x 0.001)
    #   = q2_half_up(10.989) = 10.99 (HALF_UP and HALF_EVEN agree)
    # cash in = 10989.00 - 10.99 = 10978.01 ; portfolio cash = 89979.99 + 10978.01 = 100958.00
    # net P&L = +958.00 ; total fees = 10.01 + 10.99 = 21.00
    # Gross check: (22000 - 20000) x 0.5 = 1000.00 ; less slippage 10.00 + 11.00 ; less fees 21.00
    #   = 958.00
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"BTC/USD": _bar("BTC/USD", 20000.0, 20000.0, vol20=0.03)})
    eng.submit_target_weights(CRYPTO, {"BTC/USD": 0.5}, D1)
    buy_bar = eng.process_bar(D2, {"BTC/USD": _bar("BTC/USD", 20000.0, 20000.0, vol20=0.03)})

    eng.process_bar(D3, {"BTC/USD": _bar("BTC/USD", 21000.0, 22000.0, vol20=0.03)})
    exit_res = eng.submit_target_weights(CRYPTO, {}, D3)
    assert exit_res.orders[0].side is Side.SELL
    assert exit_res.orders[0].qty_requested == Decimal("0.5")

    r4 = eng.process_bar(D4, {"BTC/USD": _bar("BTC/USD", 22000.0, 22000.0, vol20=0.03)})
    sell = r4.fills[0]
    assert sell.fill_price == Decimal("21978.00000000")
    assert _exp(sell.fill_price) == -8
    (fill_ev,) = _fills(r4.new_ledger_events)
    assert fill_ev.amount == Decimal("10989.00")
    assert sell.fee == Decimal("10.99")
    assert _exp(sell.fee) == -2

    # Both legs carry a fee > 0 and slippage in the adverse direction.
    assert buy_bar.fills[0].fee > 0 and sell.fee > 0
    assert buy_bar.fills[0].fill_price > Decimal("20000.00")
    assert sell.fill_price < Decimal("22000.00")

    assert r4.snapshot.cash == Decimal("100958.00")
    assert r4.snapshot.equity - Decimal("100000.00") == Decimal("958.00")
    assert buy_bar.fills[0].fee + sell.fee == Decimal("21.00")
    eng.verify()


# ---------------------------------------------------------------- 6.5 fill direction and sizing


def test_fill_direction_is_ceiling_on_buy_and_floor_on_sell() -> None:
    # ETF open 10.00000001 (already 8 dp). Target 0.02 x 50000 = 1000.00 at ref close 10.00
    # -> 100 shares (buy sizing rounds down, exact here). Min notional 250 is met.
    # Buy: 10.00000001 x 1.0005 = 10.005000010005 -> CEILING -> 10.00500002
    #   (HALF_EVEN would give 10.00500001, a cheaper price than the exact fill).
    #   notional = q2(100 x 10.00500002) = q2(1000.500002) = 1000.50
    # Sell: 10.00000001 x 0.9995 = 9.995000009995 -> FLOOR -> 9.99500000
    #   (HALF_EVEN would give 9.99500001). notional = q2(100 x 9.995) = 999.50
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"SPY": _bar("SPY", 10.00, 10.00, vol20=0.02)})
    res = eng.submit_target_weights(CORE, {"SPY": 0.02}, D1)
    assert res.orders[0].qty_requested == Decimal("100")

    r2 = eng.process_bar(D2, {"SPY": _bar("SPY", 10.00000001, 10.00000001, vol20=0.02)})
    buy = r2.fills[0]
    assert buy.fill_price == Decimal("10.00500002")
    assert _exp(buy.fill_price) == -8
    (fill_ev,) = _fills(r2.new_ledger_events)
    assert fill_ev.amount == Decimal("1000.50")

    eng.process_bar(D3, {"SPY": _bar("SPY", 10.00000001, 10.00000001, vol20=0.02)})
    eng.submit_target_weights(CORE, {}, D3)
    r4 = eng.process_bar(D4, {"SPY": _bar("SPY", 10.00000001, 10.00000001, vol20=0.02)})
    sell = r4.fills[0]
    assert sell.fill_price == Decimal("9.99500000")
    assert _exp(sell.fill_price) == -8
    (sell_ev,) = _fills(r4.new_ledger_events)
    assert sell_ev.amount == Decimal("999.50")
    eng.verify()


def test_etf_whole_share_sizing_rounds_down_then_fills_at_open() -> None:
    # Target 0.2 x 50000 = 10000.00 at ref close 33.33.
    # 10000 / 33.33 = 300.0300... -> DOWN to whole shares = 300 (not 301, not 300.03).
    # Fill at D2 open 33.40: ceil8(33.40 x 1.0005) = ceil8(33.41670000) = 33.41670000.
    # notional = q2(300 x 33.41670000) = q2(10025.01000) = 10025.01 ; fee 0.00.
    # The fill (10025.01) exceeds the target value because sizing uses the ref close.
    eng = PortfolioEngine(default_config())
    eng.process_bar(D1, {"SPY": _bar("SPY", 33.33, 33.33)})
    res = eng.submit_target_weights(CORE, {"SPY": 0.2}, D1)
    assert res.orders[0].qty_requested == Decimal("300")

    r2 = eng.process_bar(D2, {"SPY": _bar("SPY", 33.40, 33.40)})
    buy = r2.fills[0]
    assert buy.qty_filled == Decimal("300")
    assert buy.fill_price == Decimal("33.41670000")
    (fill_ev,) = _fills(r2.new_ledger_events)
    assert fill_ev.amount == Decimal("10025.01")
    assert _exp(fill_ev.amount) == -2
    assert buy.fee == Decimal("0.00")
    eng.verify()
