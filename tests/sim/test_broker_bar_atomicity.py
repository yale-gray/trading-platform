"""SimBroker.process_bar validates the whole bar before mutating; default asset class (Wave 3)."""

import subprocess
import sys
from datetime import date
from decimal import Decimal

import pytest

from tradebot.data.errors import UnknownSymbolError
from tradebot.data.types import AssetClass
from tradebot.portfolio.config import default_config
from tradebot.portfolio.ledger import Ledger
from tradebot.portfolio.models import EventType, Stage
from tradebot.portfolio.portfolio import Portfolio
from tradebot.sim.broker import Bar, SimBroker, default_asset_class
from tradebot.sim.orders import OrderRequest, Side

pytestmark = pytest.mark.unit

D = Decimal
D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


def _broker() -> SimBroker:
    led = Ledger()
    led.append(
        asof=D1,
        stage=Stage.CLOSE,
        sleeve_id="core_momentum",
        type=EventType.DEPOSIT,
        amount=D("10000.00"),
    )
    return SimBroker(default_config(), Portfolio(led))


def _bar(sym: str, px: float = 100.0, **kw: float) -> Bar:
    vals = {"open": px, "high": px, "low": px, "close": px, **kw}
    return Bar(symbol=sym, **vals)


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_bad_bar_price_changes_no_state(field: str) -> None:
    b = _broker()
    b.submit_orders(
        [OrderRequest(sleeve_id="core_momentum", symbol="SPY", side=Side.BUY, qty=D("10"))], D1
    )
    bad = _bar("QQQ", **{field: float("nan")})
    with pytest.raises(Exception):  # noqa: B017  (BadPriceError from to_price)
        b.process_bar(D2, {"SPY": _bar("SPY"), "QQQ": bad})
    assert b._last_asof is None and b._last_close == {}
    assert len(b.pending_orders) == 1 and len(b.portfolio.ledger) == 1
    assert b._waited == {b.pending_orders[0].order_id: 0}
    # the same date can still be processed with a good bar (asof was not advanced)
    res = b.process_bar(D2, {"SPY": _bar("SPY")})
    # BUY 10 @ ceil8(100 * 1.0005) = 100.05 -> notional 1000.50, fee 0 (equity fee_bps 0)
    assert len(res.fills) == 1 and b.portfolio.cash("core_momentum") == D("8999.50")


def test_default_asset_class_normalizes_via_data_layer() -> None:
    assert default_asset_class("spy") is AssetClass.EQUITY
    assert default_asset_class("btc-usd") is AssetClass.CRYPTO
    assert default_asset_class("BTC/USD") is AssetClass.CRYPTO
    with pytest.raises(UnknownSymbolError):
        default_asset_class("BTC")  # ambiguous bare base


def test_importing_broker_does_not_load_network_libs() -> None:
    code = (
        "import sys, tradebot.sim.broker;"
        "bad=[m for m in ('yfinance','ccxt') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
