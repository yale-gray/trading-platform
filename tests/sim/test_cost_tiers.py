"""ADR-0001 A23 tiered costs: tier figures, sell-side fee, stress, flat unchanged."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tradebot.data.symbols import normalize_symbol
from tradebot.portfolio import cost_tiers as ct
from tradebot.portfolio.config import default_config
from tradebot.portfolio.ledger import Ledger
from tradebot.portfolio.models import EventType, Stage
from tradebot.portfolio.portfolio import Portfolio
from tradebot.sim.broker import Bar, SimBroker
from tradebot.sim.orders import OrderIdGenerator, OrderRequest, Side

pytestmark = pytest.mark.unit

D = Decimal
D1, D2, D3 = (date(2024, 1, 2) + timedelta(days=i) for i in range(3))
CORE = "core_momentum"


@pytest.mark.parametrize(
    ("symbol", "bps"),
    [
        ("SPY", "1"), ("QQQ", "1"), ("DIA", "1"), ("IWM", "1"),
        ("TQQQ", "3"), ("SQQQ", "3"), ("SPXL", "3"), ("SPXS", "3"), ("UPRO", "3"),
        ("SPXU", "3"), ("SSO", "3"), ("SDS", "3"), ("SH", "3"), ("QLD", "3"),
        ("PSQ", "3"), ("SOXL", "3"), ("SOXS", "3"),
        ("AAPL", "1.5"), ("BRK.B", "1.5"), ("AMGN", "1.5"),
        ("XYZQ", "3"), ("F", "3"),
        ("BTC/USD", "15"), ("ETH/USD", "15"), ("SOL/USD", "30"),
    ],
)  # fmt: skip
def test_tier_slip_figures(symbol: str, bps: str) -> None:
    assert ct.tier_slip_bps(symbol, "tiered") == D(bps)
    assert ct.tier_slip_bps(symbol, "tiered_stress") == D(bps) * 2


def test_mega_cap_list_is_fifty_normalized_unique_names() -> None:
    assert len(ct.MEGA_CAPS) == 50
    assert all(str(normalize_symbol(s)) == s for s in ct.MEGA_CAPS)
    assert not ct.MEGA_CAPS & (ct.CORE_ETFS | ct.LEVERAGED_ETFS)
    assert len(ct.LEVERAGED_ETFS) == 13 and len(ct.CORE_ETFS) == 4


def test_reg_fee_figure_and_scope() -> None:
    assert ct.SELL_REG_FEE_BPS == D("0.25")
    assert ct.tier_extra_fee_bps("SPY", True, "tiered") == D("0.25")
    assert ct.tier_extra_fee_bps("SPY", True, "tiered_stress") == D("0.25")
    assert ct.tier_extra_fee_bps("SPY", False, "tiered") == 0
    assert ct.tier_extra_fee_bps("BTC/USD", True, "tiered") == 0
    assert ct.tier_extra_fee_bps("SPY", True, "flat") == 0


def _broker(profile: str) -> SimBroker:
    led = Ledger()
    led.append(
        asof=D1, stage=Stage.CLOSE, sleeve_id=CORE, type=EventType.DEPOSIT, amount=D("1000000.00")
    )
    cfg = default_config().model_copy(update={"equity_qty_quantum": D(1), "cost_profile": profile})
    return SimBroker(cfg, Portfolio(led), id_gen=OrderIdGenerator())


def _bar(px: float) -> Bar:
    return Bar(symbol="SPY", open=px, high=px, low=px, close=px)


def _round_trip(profile: str, sym: str = "SPY"):
    b = _broker(profile)
    req = OrderRequest(sleeve_id=CORE, symbol=sym, side=Side.BUY, qty=D(1000))
    b.submit_orders([req], D1)
    buy = b.process_bar(D2, {sym: _bar(100.0)}).fills[0]
    sell_req = OrderRequest(sleeve_id=CORE, symbol=sym, side=Side.SELL, qty=D(1000))
    b.submit_orders([sell_req], D2)
    sell = b.process_bar(D3, {sym: _bar(100.0)}).fills[0]
    return buy, sell


def test_flat_unchanged() -> None:
    buy, sell = _round_trip("flat")
    assert buy.fill_price == D("100.05") and sell.fill_price == D("99.95")  # 5 bps
    assert buy.fee == 0 and sell.fee == 0


def test_tiered_core_etf_prices_and_fee_on_sell_only() -> None:
    buy, sell = _round_trip("tiered")
    assert buy.fill_price == D("100.01")  # 1 bp
    assert sell.fill_price == D("99.99")
    assert buy.fee == 0
    # notional 1000 * 99.99 = 99,990.00; 0.25 bp -> 2.49975 -> 2.50
    assert sell.fee == D("2.50")


def test_stress_doubles_slippage_not_fee() -> None:
    buy, sell = _round_trip("tiered_stress")
    assert buy.fill_price == D("100.02") and sell.fill_price == D("99.98")  # 2 bp
    assert buy.fee == 0
    assert sell.fee == D("2.50")  # 99,980.00 * 0.25bp = 2.4995 -> 2.50


def test_mega_cap_and_other_equity_fills() -> None:
    buy, sell = _round_trip("tiered", "AAPL")
    assert buy.fill_price == D("100.015") and sell.fill_price == D("99.985")
    buy, sell = _round_trip("tiered", "XYZQ")
    assert buy.fill_price == D("100.03") and sell.fill_price == D("99.97")


def test_crypto_slippage_tiered_no_reg_fee() -> None:
    buy, sell = _round_trip("tiered", "BTC/USD")
    assert buy.fill_price == D("100.15") and sell.fill_price == D("99.85")
    # the tier figure is the whole per-side cost (A30): no asset-class fee on top
    assert buy.fee == 0 and sell.fee == 0
    _, sell_s = _round_trip("tiered_stress", "BTC/USD")
    assert sell_s.fill_price == D("99.7") and sell_s.fee == 0  # 30 bp


def test_cost_profile_omitted_from_serialisation_at_flat() -> None:
    cfg = default_config()
    assert "cost_profile" not in cfg.model_dump()
    assert "cost_profile" not in cfg.model_dump_json()
    t = cfg.model_copy(update={"cost_profile": "tiered"})
    assert t.model_dump()["cost_profile"] == "tiered"


def test_cost_profile_joins_the_config_hash_only_when_not_flat() -> None:
    from tradebot.backtest.engine import BacktestConfig, config_hash

    base = BacktestConfig(
        sleeves=[], start=date(2020, 1, 1), end=date(2020, 6, 1), engine=default_config()
    )
    hashes = {
        p: config_hash(
            base.model_copy(
                update={"engine": default_config().model_copy(update={"cost_profile": p})}
            )
        )
        for p in ("flat", "tiered", "tiered_stress")
    }
    assert len(set(hashes.values())) == 3
    assert hashes["flat"] == config_hash(base)
