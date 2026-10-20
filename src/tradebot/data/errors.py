"""Exception hierarchy for the market data layer."""


class DataError(Exception):
    """Base class for all market data errors."""


class DataQualityError(DataError):
    """Fetched bars failed validation."""


class DataRevisionError(DataError):
    """Overlap ratios vary, so the provider revised history (non-constant ratio)."""


class ImmutabilityError(DataError):
    """A write would alter existing cached rows."""


class CacheCorruptError(DataError):
    """Cached file failed its sha256 check."""


class StaleDataError(DataError):
    """Cache tail is behind the expected last closed bar."""


class ProviderError(DataError):
    """Provider failed: network error, repeated 429, or empty frame."""


class UnknownSymbolError(DataError):
    """Symbol is not in a recognised format or not supported."""
