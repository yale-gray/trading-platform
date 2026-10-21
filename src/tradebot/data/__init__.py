"""tradebot.data: cached, validated daily market data.

Import from here only. Importing this package does not import yfinance or ccxt
(the adapters load them lazily on first fetch).
"""

from tradebot.data.cache import BarCache
from tradebot.data.config import DataConfig
from tradebot.data.errors import (
    CacheCorruptError,
    DataError,
    DataQualityError,
    DataRevisionError,
    ImmutabilityError,
    ProviderError,
    RevisionQuarantinedError,
    StaleDataError,
    UnknownSymbolError,
)
from tradebot.data.provider import DataProvider
from tradebot.data.providers import CcxtProvider, FixtureProvider, YFinanceProvider
from tradebot.data.quality import QualityReport
from tradebot.data.service import MarketData, rebase, refresh, verify
from tradebot.data.types import BAR_COLUMNS, AssetClass, Symbol, Timeframe
from tradebot.data.universe import DEFAULT_UNIVERSE, StaticUniverse, Universe

__all__ = [
    "BAR_COLUMNS",
    "DEFAULT_UNIVERSE",
    "AssetClass",
    "BarCache",
    "CacheCorruptError",
    "CcxtProvider",
    "DataConfig",
    "DataError",
    "DataProvider",
    "DataQualityError",
    "DataRevisionError",
    "FixtureProvider",
    "ImmutabilityError",
    "MarketData",
    "ProviderError",
    "QualityReport",
    "RevisionQuarantinedError",
    "StaleDataError",
    "StaticUniverse",
    "Symbol",
    "Timeframe",
    "UnknownSymbolError",
    "Universe",
    "YFinanceProvider",
    "rebase",
    "refresh",
    "verify",
]
