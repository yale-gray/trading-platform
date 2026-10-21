"""Portfolio snapshot worked examples, verify() negatives, stop overlay (SPEC-003 §7, §9)."""

from datetime import date
from decimal import Context, Decimal, localcontext

import pytest

from tradebot.portfolio.ledger import Ledger, LedgerError
from tradebot.portfolio.models import EventType, FillSide, PortfolioSnapshot, Stage
from tradebot.portfolio.portfolio import Portfolio

pytestmark = pytest.mark.unit

D = Decimal
D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
SLEEVES = {"core": "50000.00", "s1": "20000.00", "s2": "20000.00", "s3": "10000.00"}


def _pf() -> Portfolio:
    """$100,000 split 50/20/20/10 across four sleeves."""
    led = Ledger()
    for sid, amt in SLEEVES.items():
        led.append(asof=D1, stage=Stage.CLOSE, sleeve_id=sid, type=EventType.DEPOSIT, amount=D(amt))
    return Portfolio(led)


def _trade(
    pf: Portfolio,
    side: FillSide,
    qty: str,
    price: str,
    *,
    sleeve: str = "core",
    symbol: str = "SPY",
    fee: str = "0.00",
    asof: date = D2,
) -> None:
    pf.ledger.append_fill(
        asof=asof,
        stage=Stage.OPEN_FILL,
        sleeve_id=sleeve,
        symbol=symbol,
        side=side,
        qty=D(qty),
        price=D(price),
        order_id=f"O{pf.ledger.next_seq:08d}",
        fee=D(fee),
    )


# --- Worked example ------------------------------------------------------------------


def test_snapshot_worked_example_then_sell_forty() -> None:
    pf = _pf()
    start = pf.snapshot(D1, {})
    assert start.equity == D("100000.00") and start.drawdown == D("0")

    _trade(pf, FillSide.BUY, "100", "100.05")  # notional 100 * 100.05 = 10005.00, fee 0
    snap = pf.snapshot(D2, {"SPY": D("101")}, peak_equity=D("100000.00"))
    # core cash = 50000 - 10005 = 39995.00 ; mv = 100 * 101 = 10100.00
    # core equity = 39995.00 + 10100.00 = 50095.00 ; portfolio = 50095 + 20000 + 20000 + 10000
    core = next(s for s in snap.sleeves if s.sleeve_id == "core")
    assert core.cash == D("39995.00")
    assert core.positions[0].mv == D("10100.00")
    assert core.positions[0].unrealized == D("95.00")  # 10100 - 10005
    assert core.equity == D("50095.00")
    assert snap.equity == D("100095.00")
    assert snap.cash == D("89995.00")  # 100095 - 10100
    assert snap.peak_equity == D("100095.00") and snap.drawdown == D("0")  # new high
    # weights: 50095/100095 = 0.500474549.. -> 0.500475 ; 20000/100095 = 0.199810.. -> 0.199810
    # 10000/100095 = 0.099905.. -> 0.099905
    assert [str(s.weight) for s in snap.sleeves] == ["0.500475", "0.199810", "0.199810", "0.099905"]
    assert snap.exposures == {"SPY": D("0.100904")}  # 10100 / 100095 = 0.10090414..
    assert snap.crypto_fraction == D("0.000000")
    pf.verify(snap)

    # SELL 40 @ 101: proceeds q2(40 * 101) = 4040.00 ; reduce = q2(10005 * 40 / 100) = 4002.00
    _trade(pf, FillSide.SELL, "40", "101", asof=D3)
    st = pf.ledger.state
    pos = st.positions["core"]["SPY"]
    assert pos.cost_basis == D("10005.00") - D("4002.00") == D("6003.00")
    assert pos.qty == D("60")
    assert st.realized == D("38.00")  # 4040.00 - 4002.00
    assert pf.ledger.events[-1].realized_pnl == D("38.00")
    snap2 = pf.snapshot(D3, {"SPY": D("101")})
    # cash 39995 + 4040 = 44035.00 ; mv 60 * 101 = 6060.00 ; equity unchanged 50095.00
    assert snap2.sleeves[0].cash == D("44035.00")
    assert snap2.sleeves[0].positions[0].unrealized == D("57.00")  # 6060 - 6003
    assert snap2.equity == D("100095.00")
    pf.verify(snap2)  # I6: 100000 + 38.00 - 0 + 57.00 = 100095.00


def test_drawdown_against_a_given_peak() -> None:
    pf = _pf()
    _trade(pf, FillSide.BUY, "100", "100.05")
    snap = pf.snapshot(D2, {"SPY": D("101")}, peak_equity=D("101000.00"))
    # 1 - 100095/101000 = 905/101000 = 0.00896039603960.. -> 10 dp 0.0089603960
    assert snap.drawdown == D("0.0089603960") and snap.peak_equity == D("101000.00")


def test_crypto_fraction_fee_and_half_even_mark() -> None:
    pf = _pf()
    # 0.25 BTC @ 20000.12345678 = 5000.030864195 -> q2 = 5000.03 ; fee 5.00 (10 bps, given)
    _trade(pf, FillSide.BUY, "0.25", "20000.12345678", sleeve="s3", symbol="BTC/USD", fee="5.00")
    snap = pf.snapshot(D2, {"BTC/USD": D("21000.5")}, crypto_symbols={"BTC/USD"})
    # s3 cash = 10000 - 5000.03 - 5.00 = 4994.97 ; mv = q2(0.25 * 21000.5) = q2(5250.125) = 5250.12
    s3 = next(s for s in snap.sleeves if s.sleeve_id == "s3")
    assert s3.cash == D("4994.97")
    assert s3.positions[0].mv == D("5250.12")
    assert s3.equity == D("10245.09")
    # portfolio = 50000 + 20000 + 20000 + 10245.09
    assert snap.equity == D("100245.09")
    # crypto fraction = 5250.12 / 100245.09 = 0.05237285.. -> 0.052373
    assert snap.crypto_fraction == D("0.052373")
    pf.verify(snap)  # I6: 100000 - 5.00 + (5250.12 - 5000.03) = 100245.09


def test_missing_close_raises() -> None:
    pf = _pf()
    _trade(pf, FillSide.BUY, "1", "10")
    with pytest.raises(LedgerError, match="no close"):
        pf.snapshot(D2, {})


def test_snapshot_is_independent_of_ambient_context() -> None:
    pf = _pf()
    _trade(pf, FillSide.BUY, "100", "100.05")
    closes = {"SPY": D("101")}
    ref = pf.snapshot(D2, closes, peak_equity=D("101000.00")).model_dump_json()
    with localcontext(Context(prec=3)):
        got = pf.snapshot(D2, closes, peak_equity=D("101000.00"))
        pf.verify(got)
    assert got.model_dump_json() == ref
    assert got.equity == D("100095.00") and got.drawdown == D("0.0089603960")


# --- verify() negatives ------------------------------------------------------------------


def _good() -> tuple[Portfolio, PortfolioSnapshot]:
    pf = _pf()
    _trade(pf, FillSide.BUY, "100", "100.05")
    snap = pf.snapshot(D2, {"SPY": D("101")})
    pf.verify(snap)
    return pf, snap


def _with_core(snap: PortfolioSnapshot, **sleeve_over: object) -> PortfolioSnapshot:
    sleeves = list(snap.sleeves)
    sleeves[0] = sleeves[0].model_copy(update=sleeve_over)
    return snap.model_copy(update={"sleeves": sleeves})


def _with_pos(snap: PortfolioSnapshot, **pos_over: object) -> PortfolioSnapshot:
    pos = snap.sleeves[0].positions[0].model_copy(update=pos_over)
    return _with_core(snap, positions=[pos])


def test_verify_catches_sleeve_cash_not_matching_equity() -> None:
    pf, snap = _good()
    with pytest.raises(LedgerError, match="I1: sleeve"):
        pf.verify(_with_core(snap, cash=snap.sleeves[0].cash + D("1.00")))


def test_verify_catches_tampered_mv() -> None:
    pf, snap = _good()
    with pytest.raises(LedgerError, match="I1: sleeve"):
        pf.verify(_with_pos(snap, mv=D("10101.00")))


def test_verify_catches_portfolio_equity_mismatch() -> None:
    pf, snap = _good()
    with pytest.raises(LedgerError, match="I1: portfolio"):
        pf.verify(snap.model_copy(update={"equity": snap.equity + D("1.00")}))


def test_verify_catches_sleeve_equities_not_summing_to_portfolio() -> None:
    pf, snap = _good()
    # cash and equity both lowered by 1.00 so I1 holds, but sleeves still sum to 100095.00
    bad = snap.model_copy(update={"cash": snap.cash - D("1.00"), "equity": snap.equity - D("1.00")})
    with pytest.raises(LedgerError, match="I2"):
        pf.verify(bad)


def test_verify_catches_negative_cash() -> None:
    pf, snap = _good()
    mv = snap.sleeves[0].positions[0].mv
    with pytest.raises(LedgerError, match="I3"):
        pf.verify(_with_core(snap, cash=D("-1.00"), equity=mv - D("1.00")))


def test_verify_catches_cash_not_equal_to_fold() -> None:
    pf, snap = _good()
    c = snap.sleeves[0]
    with pytest.raises(LedgerError, match="I5: sleeve"):
        pf.verify(_with_core(snap, cash=c.cash + D("1.00"), equity=c.equity + D("1.00")))


def test_verify_catches_replay_mismatch() -> None:
    pf, snap = _good()
    pf.ledger._state.cash["core"] += D("1.00")  # simulate a corrupted live fold
    with pytest.raises(LedgerError, match="I5: replay"):
        pf.verify(snap)


def test_verify_catches_missing_entry_asof() -> None:
    pf, snap = _good()
    with pytest.raises(LedgerError, match="I18"):
        pf.verify(_with_pos(snap, entry_asof=None))


def test_verify_catches_wrong_unrealized_via_i6() -> None:
    pf, snap = _good()
    with pytest.raises(LedgerError, match="I6"):
        pf.verify(_with_pos(snap, unrealized=D("96.00")))  # true 95.00


def test_verify_i6_sees_unbooked_fee_or_realized_drift() -> None:
    pf, snap = _good()
    pf.ledger._state.fees += D("1.00")  # a fee that never reduced any cash
    with pytest.raises(LedgerError, match="I6"):
        pf.verify(snap)


# --- Stop overlay is cleared at flat (F1) ------------------------------------------------------


def test_overlay_applies_while_open_and_survives_add_and_trim() -> None:
    pf = _pf()
    _trade(pf, FillSide.BUY, "10", "10")
    pf.set_stop_fields("core", "SPY", D("500"), D("400"))
    _trade(pf, FillSide.BUY, "5", "10")  # add
    _trade(pf, FillSide.SELL, "3", "10")  # trim
    p = pf.positions("core")["SPY"]
    assert (p.high_water_close, p.stop_level) == (D("500"), D("400"))
    snap = pf.snapshot(D2, {"SPY": D("10")})
    assert snap.sleeves[0].positions[0].stop_level == D("400")


def test_overlay_is_cleared_when_position_goes_flat_and_reentry_starts_fresh() -> None:
    pf = _pf()
    _trade(pf, FillSide.BUY, "10", "10")
    pf.set_stop_fields("core", "SPY", D("500"), D("400"))
    _trade(pf, FillSide.SELL, "10", "10")  # flat
    assert pf.positions("core") == {}
    _trade(pf, FillSide.BUY, "1", "10", asof=D3)  # re-entry, no read in between
    p = pf.positions("core")["SPY"]
    assert (p.high_water_close, p.stop_level) == (None, None)
    snap = pf.snapshot(D3, {"SPY": D("10")})
    assert snap.sleeves[0].positions[0].high_water_close is None
    assert snap.sleeves[0].positions[0].stop_level is None
    # and a fresh overlay can be set for the new episode
    pf.set_stop_fields("core", "SPY", D("11"), D("9"))
    assert pf.positions("core")["SPY"].stop_level == D("9")


def test_overlay_is_per_symbol_and_set_requires_open_position() -> None:
    pf = _pf()
    _trade(pf, FillSide.BUY, "10", "10")
    _trade(pf, FillSide.BUY, "10", "10", symbol="QQQ")
    pf.set_stop_fields("core", "SPY", D("12"), D("9"))
    _trade(pf, FillSide.SELL, "10", "10", symbol="QQQ")
    assert pf.positions("core")["SPY"].stop_level == D("9")
    with pytest.raises(LedgerError):
        pf.set_stop_fields("core", "QQQ", D("1"), D("1"))
