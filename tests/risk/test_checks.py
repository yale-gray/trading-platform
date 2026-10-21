"""R0-R6 pipeline examples, I10 caps, PM exit intent and ambient-context independence.

SPEC-003 section 6 (shrink vs reject, "SELLs are never vetoed"), invariant I10, AC 2, 3, 7.
Every expected number is derived by hand in a comment; none is read back from code.
"""

from collections.abc import Mapping
from datetime import date
from decimal import ROUND_UP, Context, Decimal, localcontext

import pytest

from tradebot.data.types import AssetClass
from tradebot.portfolio.config import SleeveConfig, default_config
from tradebot.portfolio.ledger import Ledger
from tradebot.portfolio.models import EventType, FillSide, PortfolioSnapshot, Stage
from tradebot.portfolio.portfolio import Portfolio
from tradebot.risk.checks import CheckResult, Rejection, check_target_weights
from tradebot.risk.limits import PendingOrder
from tradebot.risk.sizing import SymbolMarket, vol_scaled_weights
from tradebot.sim.orders import OrderOrigin, OrderReason, OrderRequest, Side

pytestmark = pytest.mark.unit

D = Decimal
D1, D2 = date(2024, 1, 2), date(2024, 1, 3)
CFG = default_config()
CORE, MR, CRYPTO, CASH = "core_momentum", "mean_reversion", "crypto_trend", "cash"
EQ, CR = AssetClass.EQUITY, AssetClass.CRYPTO
R = OrderReason


def _sleeve(sid: str) -> SleeveConfig:
    return next(s for s in CFG.sleeves if s.sleeve_id == sid)


def _mk(
    symbol: str,
    ref: str | None = "100",
    vol: str | None = "0.01",
    ac: AssetClass = EQ,
    tradable: bool = True,
) -> SymbolMarket:
    return SymbolMarket(
        symbol, ac, None if ref is None else D(ref), None if vol is None else D(vol), tradable
    )


def _book(
    deposits: Mapping[str, str], buys: tuple[tuple[str, str, str, str], ...] = ()
) -> Portfolio:
    """Book deposits, then BUY fills given as (sleeve, symbol, qty, price), with zero fees."""
    led = Ledger()
    for sid, amt in deposits.items():
        led.append(asof=D1, stage=Stage.CLOSE, sleeve_id=sid, type=EventType.DEPOSIT, amount=D(amt))
    pf = Portfolio(led)
    for sid, sym, qty, price in buys:
        pf.ledger.append_fill(
            asof=D2,
            stage=Stage.OPEN_FILL,
            sleeve_id=sid,
            symbol=sym,
            side=FillSide.BUY,
            qty=D(qty),
            price=D(price),
            order_id=f"O{pf.ledger.next_seq:08d}",
            fee=D("0"),
        )
    return pf


def _snap(
    pf: Portfolio, closes: Mapping[str, str] | None = None, killed: bool = False
) -> PortfolioSnapshot:
    return pf.snapshot(D2, {k: D(v) for k, v in (closes or {}).items()}, killed=killed)


def _check(
    snap: PortfolioSnapshot,
    sid: str,
    weights: Mapping[str, str],
    markets: Mapping[str, SymbolMarket],
    *,
    pending: tuple[PendingOrder, ...] = (),
    stopped_out: tuple[str, ...] = (),
) -> CheckResult:
    return check_target_weights(
        sleeve=_sleeve(sid),
        weights={k: D(v) for k, v in weights.items()},
        snapshot=snap,
        markets=markets,
        risk=CFG.risk,
        costs=CFG.costs,
        pending=pending,
        stopped_out=stopped_out,
    )


def _buy(sid: str, sym: str, qty: str) -> PendingOrder:
    return PendingOrder(sid, sym, Side.BUY, D(qty))


def _order(sid: str, sym: str, side: Side, qty: str) -> OrderRequest:
    return OrderRequest(
        sleeve_id=sid, symbol=sym, side=side, qty=D(qty), origin=OrderOrigin.STRATEGY
    )


# --- R3: per-asset cap across sleeves ----------------------------------------------------


def test_r3_held_in_other_sleeve_shrinks_to_exact_room() -> None:
    # Equity = 50000 + 0 (MR cash) + 20000 (200 SPY at 100) + 20000 + 10000 = 100000.
    # Room = 0.25 * 100000 - 20000 (SPY held by MR) = 5000.
    # Core target = 0.5 * 50000 (core cash) = 25000 -> 250 shares at 100.
    # 250 * 100 = 25000 > 5000 -> shrink to floor(5000 / 100) = 50.
    # R5: 50 * 100.05 = 5002.50 <= 50000 cash. R6: 5000 >= 0.005 * 50000 = 250.
    pf = _book(
        {CORE: "50000", MR: "20000", CRYPTO: "20000", CASH: "10000"},
        ((MR, "SPY", "200", "100"),),
    )
    res = _check(_snap(pf, {"SPY": "100"}), CORE, {"SPY": "0.5"}, {"SPY": _mk("SPY")})
    assert res.orders == (_order(CORE, "SPY", Side.BUY, "50"),)
    assert res.rejections == (Rejection("SPY", R.MAX_ASSET_SHRUNK, D("250"), D("50")),)


def test_r3_pending_buy_elsewhere_shrinks_to_remaining_room() -> None:
    # Pending BUY of 200 SPY in MR = 200 * 100 = 20000 notional. Nothing held.
    # Room = 25000 - 20000 = 5000 -> shrink 250 to 50.
    pf = _book({CORE: "50000", MR: "20000", CRYPTO: "20000", CASH: "10000"})
    res = _check(
        _snap(pf),
        CORE,
        {"SPY": "0.5"},
        {"SPY": _mk("SPY")},
        pending=(_buy(MR, "SPY", "200"),),
    )
    assert res.orders == (_order(CORE, "SPY", Side.BUY, "50"),)
    assert res.rejections == (Rejection("SPY", R.MAX_ASSET_SHRUNK, D("250"), D("50")),)


def test_r3_pending_buy_elsewhere_uses_all_room_rejects_max_asset() -> None:
    # Pending BUY of 250 SPY in MR = 250 * 100 = 25000 notional = the whole 25% cap.
    # Room = 25000 - 25000 = 0 -> reject (no shrink), accepted 0.
    pf = _book({CORE: "50000", MR: "20000", CRYPTO: "20000", CASH: "10000"})
    res = _check(
        _snap(pf),
        CORE,
        {"SPY": "0.5"},
        {"SPY": _mk("SPY")},
        pending=(_buy(MR, "SPY", "250"),),
    )
    assert res.orders == ()
    assert res.rejections == (Rejection("SPY", R.MAX_ASSET, D("250"), D("0")),)


# --- R4: crypto cap ------------------------------------------------------------------------


def test_r4_crypto_shrinks_to_room() -> None:
    # Equity = 40000 + 20000 + 40000 = 100000. Crypto cap = 0.30 * 100000 = 30000.
    # Pending ETH BUY 19.6 at 1000 (CRYPTO sleeve) = 19600 crypto exposure.
    # Crypto room = 30000 - 19600 = 10400.
    # BTC target = 0.5 * 40000 = 20000 -> 20 BTC. R3 room 25000 is not binding.
    # 20 * 1000 = 20000 > 10400 -> shrink to 10.4 BTC.
    # R5: avail = 40000 - 19.6 * 1002 = 20360.80; 10.4 * 1002 = 10420.80 fits.
    pf = _book({CORE: "40000", MR: "20000", CRYPTO: "40000"})
    res = _check(
        _snap(pf),
        CRYPTO,
        {"BTC": "0.5"},
        {"BTC": _mk("BTC", "1000", "0.02", CR), "ETH": _mk("ETH", "1000", "0.02", CR)},
        pending=(_buy(CRYPTO, "ETH", "19.6"),),
    )
    assert res.orders == (_order(CRYPTO, "BTC", Side.BUY, "10.4"),)
    assert res.rejections == (Rejection("BTC", R.MAX_CRYPTO_SHRUNK, D("20"), D("10.4")),)


def test_r4_zero_crypto_room_rejects() -> None:
    # Pending ETH BUY 30 at 1000 = 30000 = the whole crypto cap. Room = 0 -> reject.
    pf = _book({CORE: "40000", MR: "20000", CRYPTO: "40000"})
    res = _check(
        _snap(pf),
        CRYPTO,
        {"BTC": "0.5"},
        {"BTC": _mk("BTC", "1000", "0.02", CR), "ETH": _mk("ETH", "1000", "0.02", CR)},
        pending=(_buy(CRYPTO, "ETH", "30"),),
    )
    assert res.orders == ()
    assert res.rejections == (Rejection("BTC", R.MAX_CRYPTO, D("20"), D("0")),)


# --- R5: sleeve cash ----------------------------------------------------------------------


def test_r5_cash_shrinks_to_affordable_shares() -> None:
    # Core cash 6000 = sizing equity. Target = 1.0 * 6000 -> 60 shares at 100.
    # Unit cost = 100 * (1 + 5 bps) = 100.05 (equity fee 0, slip 5 bps).
    # 60 * 100.05 = 6003 > 6000 -> shrink to floor(6000 / 100.05) = floor(59.97) = 59.
    # R3: 60 * 100 = 6000 << 25% of 100000.
    pf = _book({CORE: "6000", MR: "94000"})
    res = _check(_snap(pf), CORE, {"SPY": "1.0"}, {"SPY": _mk("SPY")})
    assert res.orders == (_order(CORE, "SPY", Side.BUY, "59"),)
    assert res.rejections == (Rejection("SPY", R.CASH_SHRUNK, D("60"), D("59")),)


# --- R6: minimum size and sub-share targets ----------------------------------------------


def test_r6_sub_share_entry_rejects_below_min_size() -> None:
    # Target = 0.0001 * 50000 = 5 -> 5 / 100 = 0.05 shares -> rounds down to 0.
    # Requested 0 with no holding and a positive target: BELOW_MIN_SIZE.
    pf = _book({CORE: "50000", MR: "50000"})
    res = _check(_snap(pf), CORE, {"SPY": "0.0001"}, {"SPY": _mk("SPY")})
    assert res.orders == ()
    assert res.rejections == (Rejection("SPY", R.BELOW_MIN_SIZE, D("0"), D("0")),)


def test_r6_entry_below_min_notional_rejects_and_above_passes() -> None:
    # Min notional = 0.005 * 50000 = 250.
    # w = 0.004 -> target 200 -> 2 shares = 200 < 250 -> reject (requested 2).
    pf = _book({CORE: "50000", MR: "50000"})
    res = _check(_snap(pf), CORE, {"SPY": "0.004"}, {"SPY": _mk("SPY")})
    assert res.orders == ()
    assert res.rejections == (Rejection("SPY", R.BELOW_MIN_SIZE, D("2"), D("0")),)
    # w = 0.006 -> target 300 -> 3 shares = 300 >= 250 -> BUY 3.
    res = _check(_snap(pf), CORE, {"SPY": "0.006"}, {"SPY": _mk("SPY")})
    assert res.orders == (_order(CORE, "SPY", Side.BUY, "3"),)
    assert res.rejections == ()


def test_r6_partial_sell_below_min_size_rejects() -> None:
    # Core: 60000 deposit, buy 100 SPY at 100 -> cash 50000, mv 10000, equity 60000.
    # Min notional = 0.005 * 60000 = 300.
    # Target = 0.165 * 60000 = 9900 -> 99 shares; sell 100 - 99 = 1 share.
    # Notional 1 * 100 = 100 < 300 and not a full exit -> BELOW_MIN_SIZE (requested 1).
    pf = _book({CORE: "60000"}, ((CORE, "SPY", "100", "100"),))
    res = _check(_snap(pf, {"SPY": "100"}), CORE, {"SPY": "0.165"}, {"SPY": _mk("SPY")})
    assert res.orders == ()
    assert res.rejections == (Rejection("SPY", R.BELOW_MIN_SIZE, D("1"), D("0")),)


def test_full_exit_is_exempt_from_min_size() -> None:
    # Core: 50000 deposit, buy 1 SPY at 100 -> equity 50000, mv 100.
    # Min notional = 0.005 * 50000 = 250 > 100, but a full exit is exempt: SELL 1.
    pf = _book({CORE: "50000"}, ((CORE, "SPY", "1", "100"),))
    res = _check(_snap(pf, {"SPY": "100"}), CORE, {}, {"SPY": _mk("SPY")})
    assert res.orders == (_order(CORE, "SPY", Side.SELL, "1"),)
    assert res.rejections == ()


# --- R0 kill switch, stop lock, R1 -----------------------------------------------------


def test_r0_kill_switch_blocks_buy_but_full_exit_sells() -> None:
    # Core: 60000 deposit, buy 100 SPY at 100 -> cash 50000, mv 10000, equity 60000.
    # Killed: BUY of QQQ (target 0.5 * 60000 = 30000 -> 300 shares) is rejected KILL_SWITCH.
    # SPY is not in the target, so its full 100-share holding is sold (SELLs not vetoed).
    pf = _book({CORE: "60000"}, ((CORE, "SPY", "100", "100"),))
    snap = _snap(pf, {"SPY": "100"}, killed=True)
    res = _check(snap, CORE, {"QQQ": "0.5"}, {"SPY": _mk("SPY"), "QQQ": _mk("QQQ")})
    assert res.orders == (_order(CORE, "SPY", Side.SELL, "100"),)
    assert res.rejections == (Rejection("QQQ", R.KILL_SWITCH, D("300"), D("0")),)


def test_stop_lock_blocks_buy() -> None:
    # Target 0.5 * 50000 = 25000 -> 250 QQQ at 100; QQQ is in the stop lock -> STOP_LOCK.
    pf = _book({CORE: "50000", MR: "50000"})
    res = _check(_snap(pf), CORE, {"QQQ": "0.5"}, {"QQQ": _mk("QQQ")}, stopped_out=("QQQ",))
    assert res.orders == ()
    assert res.rejections == (Rejection("QQQ", R.STOP_LOCK, D("250"), D("0")),)


def test_stop_lock_does_not_block_sell() -> None:
    # Core: 60000 deposit, buy 100 QQQ at 100. QQQ is stop-locked and the target is 0.
    # The exit SELL of 100 goes through with no rejection.
    pf = _book({CORE: "60000"}, ((CORE, "QQQ", "100", "100"),))
    res = _check(_snap(pf, {"QQQ": "100"}), CORE, {}, {"QQQ": _mk("QQQ")}, stopped_out=("QQQ",))
    assert res.orders == (_order(CORE, "QQQ", Side.SELL, "100"),)
    assert res.rejections == ()


def test_r1_untradable_symbol_rejects_no_data() -> None:
    pf = _book({CORE: "50000", MR: "50000"})
    res = _check(_snap(pf), CORE, {"SPY": "0.5"}, {"SPY": _mk("SPY", tradable=False)})
    assert res.orders == ()
    assert res.rejections == (Rejection("SPY", R.NO_DATA, D("0"), D("0")),)


def test_no_vol_data_rejects_entry() -> None:
    # Core is vol-scaled. SPY has w > 0 but vol20 None: NO_VOL_DATA, no order.
    pf = _book({CORE: "50000", MR: "50000"})
    res = _check(_snap(pf), CORE, {"SPY": "0.5"}, {"SPY": _mk("SPY", vol=None)})
    assert res.orders == ()
    assert res.rejections == (Rejection("SPY", R.NO_VOL_DATA, D("0"), D("0")),)


# --- PM intent: SELLs (exits) are never vetoed ----------------------------------------


@pytest.mark.parametrize("weights", [{}, {"SPY": "0"}], ids=["absent", "zero-weight"])
def test_held_exit_with_vol_none_still_sells_full_position(weights: dict[str, str]) -> None:
    # Core is vol-scaled. SPY is held (100 at 100, equity 60000) with target 0 and vol20 None.
    # Exit = full holding = 100 shares; no vol is needed to exit.
    pf = _book({CORE: "60000"}, ((CORE, "SPY", "100", "100"),))
    res = _check(_snap(pf, {"SPY": "100"}), CORE, weights, {"SPY": _mk("SPY", vol=None)})
    assert res.orders == (_order(CORE, "SPY", Side.SELL, "100"),)
    assert res.rejections == ()


def test_held_exit_sells_while_other_entry_is_rejected_for_no_vol() -> None:
    # SPY is exited (target 0, vol None) and QQQ is a new entry with vol None.
    # SPY must still SELL 100; QQQ is rejected NO_VOL_DATA (an entry, not an exit).
    pf = _book({CORE: "60000"}, ((CORE, "SPY", "100", "100"),))
    res = _check(
        _snap(pf, {"SPY": "100"}),
        CORE,
        {"QQQ": "0.5"},
        {"SPY": _mk("SPY", vol=None), "QQQ": _mk("QQQ", vol=None)},
    )
    assert res.orders == (_order(CORE, "SPY", Side.SELL, "100"),)
    assert res.rejections == (Rejection("QQQ", R.NO_VOL_DATA, D("0"), D("0")),)


def test_held_exit_with_ref_price_none_needs_no_price() -> None:
    # SPY is held (100 shares) with target 0. The market has ref_price None and vol20 None.
    # The exit qty is the holding (100); sizing needs no price. The snapshot still marks the
    # position at close 100 (portfolio marking, not sizing).
    pf = _book({CORE: "60000"}, ((CORE, "SPY", "100", "100"),))
    res = _check(_snap(pf, {"SPY": "100"}), CORE, {}, {"SPY": _mk("SPY", ref=None, vol=None)})
    assert res.orders == (_order(CORE, "SPY", Side.SELL, "100"),)
    assert res.rejections == ()


# --- I10: post-acceptance exposure never exceeds the caps -----------------------------


@pytest.mark.parametrize("held", [0, 50, 150, 200, 249])
@pytest.mark.parametrize("target_w", ["0.1", "0.25", "0.5", "1.0"])
def test_i10_asset_exposure_never_exceeds_25_percent(held: int, target_w: str) -> None:
    # Equity stays 100000 (MR cash moves into SPY mv at 100, no fees).
    # Cap = 25000 = 250 shares at 100. Post exposure = (held + accepted) * 100 <= 25000.
    deposits = {CORE: "50000", MR: "40000", CRYPTO: "10000"}
    buys = ((MR, "SPY", str(held), "100"),) if held else ()
    pf = _book(deposits, buys)
    closes = {"SPY": "100"} if held else {}
    res = _check(_snap(pf, closes), CORE, {"SPY": target_w}, {"SPY": _mk("SPY")})
    accepted = sum((o.qty for o in res.orders if o.symbol == "SPY" and o.side is Side.BUY), D(0))
    post = (D(held) + accepted) * D("100")
    assert post <= D("0.25") * D("100000")
    assert post <= D("25000")


@pytest.mark.parametrize("pending_eth", ["5", "19.6", "29.9", "30"])
@pytest.mark.parametrize("target_btc", ["0.1", "0.5", "1.0"])
def test_i10_crypto_exposure_never_exceeds_30_percent(pending_eth: str, target_btc: str) -> None:
    # Equity 100000, crypto cap 30000. Pending ETH at 1000 is crypto exposure.
    # Post crypto exposure = pending ETH notional + accepted BTC * 1000 <= 30000.
    pf = _book({CORE: "40000", MR: "20000", CRYPTO: "40000"})
    markets = {"BTC": _mk("BTC", "1000", "0.02", CR), "ETH": _mk("ETH", "1000", "0.02", CR)}
    res = _check(
        _snap(pf),
        CRYPTO,
        {"BTC": target_btc},
        markets,
        pending=(_buy(CRYPTO, "ETH", pending_eth),),
    )
    accepted = sum((o.qty for o in res.orders if o.symbol == "BTC"), D(0))
    post = D(pending_eth) * D("1000") + accepted * D("1000")
    assert post <= D("0.30") * D("100000")


# --- Ambient decimal context independence --------------------------------------------


def _scenario() -> tuple[CheckResult, dict[str, Decimal]]:
    pf = _book({CORE: "40000", MR: "20000", CRYPTO: "40000"})
    res = _check(
        _snap(pf),
        CRYPTO,
        {"BTC": "0.5"},
        {"BTC": _mk("BTC", "1000", "0.02", CR), "ETH": _mk("ETH", "1000", "0.02", CR)},
        pending=(_buy(CRYPTO, "ETH", "19.6"),),
    )
    vols, _ = vol_scaled_weights(
        {"A": D("0.37"), "B": D("0.63")}, {"A": D("0.0123456789"), "B": D("0.0234567891")}
    )
    return res, vols


@pytest.mark.parametrize(
    "ctx",
    [Context(prec=3), Context(prec=5, rounding=ROUND_UP)],
    ids=["prec3", "prec5-round-up"],
)
def test_results_identical_under_ambient_decimal_context(ctx: Context) -> None:
    baseline = _scenario()
    with localcontext(ctx):
        under_ctx = _scenario()
    assert under_ctx == baseline
