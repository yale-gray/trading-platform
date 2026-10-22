"""Symbol normalization for the market data layer.

Canonical forms: equities upper case (``SPY``, class shares ``BRK.B``), crypto
``BASE/USD`` (``BTC/USD``).
Provider-specific forms never leave the adapters.
"""

import re

from tradebot.data.errors import UnknownSymbolError
from tradebot.data.types import AssetClass, Symbol

# Crypto base assets we accept. Extend this tuple to add a new coin.
CRYPTO_BASES: tuple[str, ...] = ("BTC", "ETH", "SOL")

# Up to 5 letters plus an optional single-letter share-class suffix (BRK.B; Yahoo spells it BRK-B).
_EQUITY_RE = re.compile(r"^(?P<root>[A-Z]{1,5})(?:[.-](?P<cls>[A-Z]))?$")
_CRYPTO_RE = re.compile(r"^(?P<base>[A-Z]+)(?:/|-)?USD$")


def normalize_symbol(raw: str) -> Symbol:
    """Return the canonical form of ``raw``.

    Accepts ``spy``/``SPY`` for equities and ``btc-usd``, ``BTCUSD`` or ``BTC/USD``
    for crypto (base must be in ``CRYPTO_BASES``). A bare crypto base (``BTC``) is
    ambiguous with an equity ticker and is rejected. Raises ``UnknownSymbolError``
    for anything else.
    """
    text = raw.upper()

    if text in CRYPTO_BASES:
        raise UnknownSymbolError(f"ambiguous: did you mean {text}/USD? (got {raw!r})")

    crypto = _CRYPTO_RE.match(text)
    if crypto is not None:
        base = crypto.group("base")
        if base in CRYPTO_BASES:
            return Symbol(f"{base}/USD")
        raise UnknownSymbolError(f"Unsupported crypto base in symbol: {raw!r}")

    equity = _EQUITY_RE.match(text)
    if equity is not None:
        cls = equity.group("cls")
        return Symbol(equity.group("root") + (f".{cls}" if cls else ""))

    raise UnknownSymbolError(f"Unrecognised symbol format: {raw!r}")


def asset_class(symbol: Symbol) -> AssetClass:
    """Return EQUITY if the symbol has no ``/``, otherwise CRYPTO."""
    return AssetClass.CRYPTO if "/" in symbol else AssetClass.EQUITY


def to_yahoo(symbol: Symbol) -> str:
    """Yahoo ticker form: ``BTC/USD`` -> ``BTC-USD``, ``BRK.B`` -> ``BRK-B``."""
    return symbol.replace("/", "-").replace(".", "-")
