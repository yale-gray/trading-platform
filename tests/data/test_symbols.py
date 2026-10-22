"""Unit tests for symbol normalization (SPEC-001 AC 1, T2)."""

import pytest

from tradebot.data import symbols
from tradebot.data.errors import UnknownSymbolError
from tradebot.data.symbols import asset_class, normalize_symbol, to_yahoo
from tradebot.data.types import AssetClass

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("SPY", "SPY"),
        ("spy", "SPY"),
        ("A", "A"),
        ("ABCDE", "ABCDE"),
        ("BTC/USD", "BTC/USD"),
        ("btc/usd", "BTC/USD"),
        ("BTC-USD", "BTC/USD"),
        ("btc-usd", "BTC/USD"),
        ("BTCUSD", "BTC/USD"),
        ("btcusd", "BTC/USD"),
        ("ETHUSD", "ETH/USD"),
        ("sol-usd", "SOL/USD"),
    ],
)
def test_normalize_accepts_spec_forms_and_maps_to_canonical(raw: str, canonical: str):
    assert normalize_symbol(raw) == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "DOGE/USD",
        "DOGE-USD",
        "DOGEUSD",
        "BTC/EUR",
        "",
        "ABCDEF",  # equities are 1-5 letters
        "SPY1",
    ],
)
def test_normalize_rejects_unknown_formats(raw: str):
    with pytest.raises(UnknownSymbolError):
        normalize_symbol(raw)


@pytest.mark.parametrize("raw", ["BTC", "btc", "Eth", "ETH", "SOL", "sol"])
def test_bare_crypto_base_is_rejected_as_ambiguous(raw: str):
    with pytest.raises(UnknownSymbolError, match=r"ambiguous: did you mean .*/USD\?"):
        normalize_symbol(raw)


def test_crypto_base_set_is_extendable_by_module_constant(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(UnknownSymbolError):
        normalize_symbol("DOGE/USD")
    monkeypatch.setattr(symbols, "CRYPTO_BASES", (*symbols.CRYPTO_BASES, "DOGE"))
    assert normalize_symbol("doge-usd") == "DOGE/USD"


def test_asset_class_equity_for_plain_ticker():
    assert asset_class(normalize_symbol("spy")) is AssetClass.EQUITY


def test_asset_class_crypto_for_pair():
    assert asset_class(normalize_symbol("btcusd")) is AssetClass.CRYPTO


def test_to_yahoo_replaces_slash_with_dash():
    assert to_yahoo(normalize_symbol("BTCUSD")) == "BTC-USD"


def test_to_yahoo_leaves_equity_unchanged():
    assert to_yahoo(normalize_symbol("spy")) == "SPY"


@pytest.mark.parametrize(
    ("raw", "canonical", "yahoo"),
    [("BRK.B", "BRK.B", "BRK-B"), ("brk-b", "BRK.B", "BRK-B"), ("BF.B", "BF.B", "BF-B")],
)
def test_class_share_tickers(raw: str, canonical: str, yahoo: str):
    sym = normalize_symbol(raw)
    assert sym == canonical
    assert asset_class(sym) is AssetClass.EQUITY
    assert to_yahoo(sym) == yahoo


@pytest.mark.parametrize("raw", ["BRK.BB", "ABCDEF.B", ".B", "BRK.", "BTC", "A.1"])
def test_class_share_rejects_bad_forms(raw: str):
    with pytest.raises(UnknownSymbolError):
        normalize_symbol(raw)
