"""Exception hierarchy for the market data layer."""

from datetime import date


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


def quarantine_warning(since: date) -> str:
    """The warning text carried by a ``QualityReport`` for a wedged symbol."""
    return f"revision_quarantined: needs rebase since {since.isoformat()}"


class RevisionQuarantinedError(DataRevisionError):
    """A symbol's tail fetch was quarantined as a revision; only ``rebase`` clears it (ADR D2).

    The Wave 5 runner maps this to ``data_unavailable`` for ``symbol``. ``since`` is the
    UTC date on which this ``MarketData`` first saw the revision.
    """

    def __init__(self, symbol: str, since: date) -> None:
        self.symbol = symbol
        self.since = since
        super().__init__(f"{symbol}: {quarantine_warning(since)}")
