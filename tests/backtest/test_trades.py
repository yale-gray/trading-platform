"""Trades, equity and exposure derived from the ledger (SPEC-002 section 6b; AC 16).

Ledgers and snapshots are hand-built. Realised P&L on each SELL is written as the value the
engine would record, and the expected trade fields are worked out by hand in the comments.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from tradebot.backtest.trades import (
    closed_trades,
    equity_series,
    exit_reason_for,
    exposure_series,
    open_positions,
)
from tradebot.portfolio.models import (
    EventType,
    FillSide,
    LedgerEvent,
    PortfolioSnapshot,
    PositionSnapshot,
    SleeveSnapshot,
    Stage,
)
from tradebot.sim.orders import Order, OrderOrigin, OrderStatus, Side

SLEEVE = "core_momentum"


def _order(
    oid: str,
    symbol: str,
    side: Side,
    origin: OrderOrigin,
    qty: str,
    price: str,
    fee: str,
    fill: date,
) -> Order:
    return Order(
        order_id=oid,
        sleeve_id=SLEEVE,
        symbol=symbol,
        side=side,
        origin=origin,
        qty_requested=Decimal(qty),
        qty_accepted=Decimal(qty),
        qty_filled=Decimal(qty),
        status=OrderStatus.FILLED,
        created_asof=date.fromordinal(fill.toordinal() - 1),
        ref_price=Decimal(price),
        fill_asof=fill,
        fill_price=Decimal(price),
        fee=Decimal(fee),
    )


class _Ledger:
    """Assigns sequence numbers as events are added."""

    def __init__(self) -> None:
        self.events: list[LedgerEvent] = []

    def _add(self, **kw: object) -> None:
        self.events.append(LedgerEvent(seq=len(self.events) + 1, **kw))  # type: ignore[arg-type]

    def fill(
        self,
        oid: str,
        symbol: str,
        side: FillSide,
        qty: str,
        price: str,
        amount: str,
        asof: date,
        realized: str | None = None,
    ) -> None:
        self._add(
            asof=asof,
            stage=Stage.OPEN_FILL,
            sleeve_id=SLEEVE,
            type=EventType.FILL,
            amount=Decimal(amount),
            symbol=symbol,
            side=side,
            qty=Decimal(qty),
            price=Decimal(price),
            order_id=oid,
            realized_pnl=Decimal(realized) if realized is not None else None,
        )

    def fee(self, oid: str, amount: str, asof: date) -> None:
        self._add(
            asof=asof,
            stage=Stage.OPEN_FILL,
            sleeve_id=SLEEVE,
            type=EventType.FEE,
            amount=Decimal(amount),
            order_id=oid,
        )


def _scale_in_and_stop_ledger() -> tuple[list[LedgerEvent], dict[str, Order]]:
    """Two buys (scale-in), a partial sell (trim), then a stop-out sell that closes the trip.

    Buy 10 SPY @ 100.00, fee 1.00, cash 1000.00      (order o1, 2024-01-02)
    Buy 10 SPY @ 110.00, fee 2.00, cash 1100.00      (order o2, 2024-01-05)
    Cost basis = 2100.00 over 20 shares; average cost = 105.00.
    Sell  5 SPY @ 120.00, fee 0.50, realised = (120 - 105) * 5 = +75.00
        (order o3, STRATEGY, 2024-01-09)
    Sell 15 SPY @  90.00, fee 1.50, realised = (90 - 105) * 15 = -225.00
        (order o4, STOP, 2024-01-12)
    """
    led = _Ledger()
    led.fill("o1", "SPY", FillSide.BUY, "10", "100.00", "1000.00", date(2024, 1, 2))
    led.fee("o1", "1.00", date(2024, 1, 2))
    led.fill("o2", "SPY", FillSide.BUY, "10", "110.00", "1100.00", date(2024, 1, 5))
    led.fee("o2", "2.00", date(2024, 1, 5))
    led.fill(
        "o3", "SPY", FillSide.SELL, "5", "120.00", "600.00", date(2024, 1, 9), realized="75.00"
    )
    led.fee("o3", "0.50", date(2024, 1, 9))
    led.fill(
        "o4", "SPY", FillSide.SELL, "15", "90.00", "1350.00", date(2024, 1, 12), realized="-225.00"
    )
    led.fee("o4", "1.50", date(2024, 1, 12))
    orders = {
        "o1": _order(
            "o1", "SPY", Side.BUY, OrderOrigin.STRATEGY, "10", "100.00", "1.00", date(2024, 1, 2)
        ),
        "o2": _order(
            "o2", "SPY", Side.BUY, OrderOrigin.STRATEGY, "10", "110.00", "2.00", date(2024, 1, 5)
        ),
        "o3": _order(
            "o3", "SPY", Side.SELL, OrderOrigin.STRATEGY, "5", "120.00", "0.50", date(2024, 1, 9)
        ),
        "o4": _order(
            "o4", "SPY", Side.SELL, OrderOrigin.STOP, "15", "90.00", "1.50", date(2024, 1, 12)
        ),
    }
    return led.events, orders


def test_scale_in_and_trim_stay_in_one_round_trip() -> None:
    events, orders = _scale_in_and_stop_ledger()
    trades = closed_trades(events, orders)
    assert len(trades) == 1  # the trim at 2024-01-09 does not close the trip; the stop does
    t = trades[0]
    assert t.sleeve_id == SLEEVE and t.symbol == "SPY"
    assert t.entry_asof == date(2024, 1, 2)  # first BUY fill
    assert t.exit_asof == date(2024, 1, 12)  # closing SELL fill
    assert t.qty == Decimal("20")  # total bought: 10 + 10
    # Average buy price = (1000 + 1100) / 20 = 105.00.
    assert t.entry_price == Decimal("105.00")
    # Average sell price = (5 x 120 + 15 x 90) / 20 = 1950 / 20 = 97.50.
    assert t.exit_price == Decimal("97.50")
    # Fees = 1.00 + 2.00 + 0.50 + 1.50 = 5.00.
    assert t.fees == Decimal("5.00")
    # Realised = +75.00 - 225.00 = -150.00; net = -150.00 - 5.00 = -155.00.
    assert t.net_pnl == Decimal("-155.00")
    assert t.exit_reason == "stop"


def test_closing_order_origin_maps_to_exit_reason() -> None:
    # The closing SELL's origin decides exit_reason (table in SPEC-002 section 6b).
    assert exit_reason_for(OrderOrigin.STRATEGY) == "signal"
    assert exit_reason_for(OrderOrigin.STOP) == "stop"
    assert exit_reason_for(OrderOrigin.REBALANCE) == "rebalance"
    assert exit_reason_for(OrderOrigin.MANUAL) == "manual"


@pytest.mark.parametrize(
    ("origin", "reason"),
    [
        (OrderOrigin.STRATEGY, "signal"),
        (OrderOrigin.STOP, "stop"),
        (OrderOrigin.REBALANCE, "rebalance"),
        (OrderOrigin.MANUAL, "manual"),
    ],
)
def test_round_trip_exit_reason_comes_from_closing_order(origin: OrderOrigin, reason: str) -> None:
    # Buy 1 @ 10.00 then sell 1 @ 12.00 with realised +2.00 and no fees: net = +2.00.
    led = _Ledger()
    led.fill("b", "QQQ", FillSide.BUY, "1", "10.00", "10.00", date(2024, 3, 1))
    led.fill("s", "QQQ", FillSide.SELL, "1", "12.00", "12.00", date(2024, 3, 4), realized="2.00")
    orders = {
        "b": _order(
            "b", "QQQ", Side.BUY, OrderOrigin.STRATEGY, "1", "10.00", "0.00", date(2024, 3, 1)
        ),
        "s": _order("s", "QQQ", Side.SELL, origin, "1", "12.00", "0.00", date(2024, 3, 4)),
    }
    (t,) = closed_trades(led.events, orders)
    assert t.exit_reason == reason
    assert t.net_pnl == Decimal("2.00")


def test_going_flat_then_buying_again_starts_a_new_round_trip() -> None:
    # Trip 1: buy 4 @ 25.00, sell 4 @ 30.00 -> realised +20.00, no fees, origin STRATEGY.
    # Trip 2: buy 2 @ 40.00, sell 2 @ 35.00 -> realised (35 - 40) * 2 = -10.00, origin STRATEGY.
    led = _Ledger()
    led.fill("a", "SPY", FillSide.BUY, "4", "25.00", "100.00", date(2024, 4, 1))
    led.fill("b", "SPY", FillSide.SELL, "4", "30.00", "120.00", date(2024, 4, 3), realized="20.00")
    led.fill("c", "SPY", FillSide.BUY, "2", "40.00", "80.00", date(2024, 4, 10))
    led.fill("d", "SPY", FillSide.SELL, "2", "35.00", "70.00", date(2024, 4, 12), realized="-10.00")
    orders = {
        "a": _order(
            "a", "SPY", Side.BUY, OrderOrigin.STRATEGY, "4", "25.00", "0.00", date(2024, 4, 1)
        ),
        "b": _order(
            "b", "SPY", Side.SELL, OrderOrigin.STRATEGY, "4", "30.00", "0.00", date(2024, 4, 3)
        ),
        "c": _order(
            "c", "SPY", Side.BUY, OrderOrigin.STRATEGY, "2", "40.00", "0.00", date(2024, 4, 10)
        ),
        "d": _order(
            "d", "SPY", Side.SELL, OrderOrigin.STRATEGY, "2", "35.00", "0.00", date(2024, 4, 12)
        ),
    }
    first, second = closed_trades(led.events, orders)
    assert (first.entry_asof, first.exit_asof) == (date(2024, 4, 1), date(2024, 4, 3))
    assert first.net_pnl == Decimal("20.00")
    assert (second.entry_asof, second.exit_asof) == (date(2024, 4, 10), date(2024, 4, 12))
    assert second.entry_price == Decimal("40.00")
    assert second.exit_price == Decimal("35.00")
    assert second.net_pnl == Decimal("-10.00")
    assert second.exit_reason == "signal"


def test_open_position_at_end_is_listed_not_counted_as_a_trade() -> None:
    # Buy 3 QQQ @ 70.00 and never sell: no closed trade, but one open position of qty 3.
    led = _Ledger()
    led.fill("q", "QQQ", FillSide.BUY, "3", "70.00", "210.00", date(2024, 5, 1))
    orders = {
        "q": _order(
            "q", "QQQ", Side.BUY, OrderOrigin.STRATEGY, "3", "70.00", "0.00", date(2024, 5, 1)
        ),
    }
    assert closed_trades(led.events, orders) == []
    snap = PortfolioSnapshot(
        asof=date(2024, 5, 2),
        equity=Decimal("100000.00"),
        cash=Decimal("99790.00"),
        drawdown=Decimal("0"),
        peak_equity=Decimal("100000.00"),
        killed=False,
        last_seq=1,
        sleeves=[
            SleeveSnapshot(
                sleeve_id=SLEEVE,
                cash=Decimal("99790.00"),
                equity=Decimal("100000.00"),
                weight=Decimal("1"),
                positions=[
                    PositionSnapshot(
                        symbol="QQQ",
                        qty=Decimal("3"),
                        mv=Decimal("210.00"),
                        cost_basis=Decimal("210.00"),
                        unrealized=Decimal("0.00"),
                        entry_asof=date(2024, 5, 1),
                    )
                ],
            )
        ],
        exposures={},
        crypto_fraction=Decimal("0"),
    )
    (op,) = open_positions(snap)
    assert (op.symbol, op.qty, op.entry_asof, op.mv) == (
        "QQQ",
        Decimal("3"),
        date(2024, 5, 1),
        Decimal("210.00"),
    )


def _snap(asof: date, sleeves: list[SleeveSnapshot], equity: str) -> PortfolioSnapshot:
    cash = sum((s.cash for s in sleeves), Decimal(0))
    return PortfolioSnapshot(
        asof=asof,
        equity=Decimal(equity),
        cash=cash,
        drawdown=Decimal("0"),
        peak_equity=Decimal(equity),
        killed=False,
        last_seq=0,
        sleeves=sleeves,
        exposures={},
        crypto_fraction=Decimal("0"),
    )


def _pos(symbol: str, mv: str) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal("1"),
        mv=Decimal(mv),
        cost_basis=Decimal(mv),
        unrealized=Decimal("0.00"),
        entry_asof=date(2024, 1, 2),
    )


def test_equity_and_exposure_series_match_hand_calc() -> None:
    # Day 1 (2024-01-02): cash 100000.00, no positions. Equity 100000.00, exposure 0.
    # Day 2 (2024-01-03): cash 80000.00, SPY mv 20000.00. Equity 100000.00,
    #   exposure = 20000 / 100000 = 0.2.
    # Day 3 (2024-01-04): two sleeves. core: cash 30000 + SPY 30000 (equity 60000);
    #   mean_reversion: cash 30000 + QQQ 20000 (equity 50000). Equity 110000,
    #   gross mv = 50000, exposure = 50000 / 110000 = 5/11 = 0.4545...
    day1 = _snap(
        date(2024, 1, 2),
        [
            SleeveSnapshot(
                sleeve_id=SLEEVE,
                cash=Decimal("100000.00"),
                equity=Decimal("100000.00"),
                weight=Decimal("1"),
                positions=[],
            )
        ],
        "100000.00",
    )
    day2 = _snap(
        date(2024, 1, 3),
        [
            SleeveSnapshot(
                sleeve_id=SLEEVE,
                cash=Decimal("80000.00"),
                equity=Decimal("100000.00"),
                weight=Decimal("1"),
                positions=[_pos("SPY", "20000.00")],
            )
        ],
        "100000.00",
    )
    day3 = _snap(
        date(2024, 1, 4),
        [
            SleeveSnapshot(
                sleeve_id=SLEEVE,
                cash=Decimal("30000.00"),
                equity=Decimal("60000.00"),
                weight=Decimal("0.6"),
                positions=[_pos("SPY", "30000.00")],
            ),
            SleeveSnapshot(
                sleeve_id="mean_reversion",
                cash=Decimal("30000.00"),
                equity=Decimal("50000.00"),
                weight=Decimal("0.4"),
                positions=[_pos("QQQ", "20000.00")],
            ),
        ],
        "110000.00",
    )
    snaps = [day1, day2, day3]
    eq = equity_series(snaps)
    assert list(eq.values) == [100000.0, 100000.0, 110000.0]
    assert eq.index[0] == pd.Timestamp("2024-01-02", tz="UTC")
    ex = exposure_series(snaps)
    assert ex.iloc[0] == 0.0
    assert ex.iloc[1] == pytest.approx(0.2, rel=1e-12)
    assert ex.iloc[2] == pytest.approx(5 / 11, rel=1e-12)
    open_rows = open_positions(day3)
    assert [(r.sleeve_id, r.symbol) for r in open_rows] == [
        (SLEEVE, "SPY"),
        ("mean_reversion", "QQQ"),
    ]
