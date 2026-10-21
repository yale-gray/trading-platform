"""Per-fetch data quality validation (spec 001, Section 5).

``validate_bars`` runs on every fetch before anything is cached. It raises
``DataQualityError`` for hard failures, drops duplicates within the fetch, and
records warnings and flags in a ``QualityReport``. ``now`` is always passed in
by the caller; this module never reads the wall clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from tradebot.data.calendar import last_closed_bar_date, sessions
from tradebot.data.errors import DataQualityError
from tradebot.data.types import BAR_COLUMNS

logger = logging.getLogger(__name__)

# Non-price metadata columns that fetchers may attach. They are dropped before
# the schema check and never returned to callers.
_METADATA_COLUMNS: tuple[str, ...] = ("fetched_at", "source_rev")
_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

MAX_GAP_SESSIONS = 0  # interior gaps allowed; leading/trailing gaps are ignored
_OUTLIER_EQUITY = 0.5  # abs daily move above this is flagged (equity ETFs)
_OUTLIER_CRYPTO = 0.6  # abs daily move above this is flagged (crypto)


@dataclass
class QualityReport:
    """Result of ``validate_bars`` for one symbol's fetch."""

    symbol: str
    n_bars: int
    gaps: list[date] = field(default_factory=list)
    duplicates_dropped: int = 0
    outliers: list[tuple[date, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # "ok", or "revision_quarantined" / "corrupt" / "quality_error" / "error" when the
    # facade could not refresh or read this symbol (see MarketData.refresh / verify).
    status: str = "ok"


def _warn(report: QualityReport, message: str) -> None:
    report.warnings.append(message)
    logger.warning("%s: %s", report.symbol, message)


def _fmt_dates(dates: list[date], limit: int = 5) -> str:
    shown = ", ".join(d.isoformat() for d in dates[:limit])
    more = f" (+{len(dates) - limit} more)" if len(dates) > limit else ""
    return shown + more


def validate_bars(
    df: pd.DataFrame, symbol: str, now: datetime, equity_close_buffer_min: int = 60
) -> tuple[pd.DataFrame, QualityReport]:
    """Validate a fetched frame and return ``(clean_frame, report)``.

    Hard failures raise ``DataQualityError`` listing every violation. The returned
    frame has exactly the five ``BAR_COLUMNS``, duplicates dropped (keep last),
    and any bar not yet closed at ``now`` (equity: close plus ``equity_close_buffer_min``)
    removed. Equity rows labelled on a non-session day (weekend or holiday artefacts) are
    dropped with a warning, never cached. Infinite prices or volume are hard failures.
    """
    # Schema: tz-aware UTC index, exact columns, float64, sorted.
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise DataQualityError(f"{symbol}: index must be a tz-aware DatetimeIndex")
    if str(df.index.tz) != "UTC":
        raise DataQualityError(f"{symbol}: index must be UTC, got {df.index.tz}")

    work = df.drop(columns=[c for c in _METADATA_COLUMNS if c in df.columns])
    if list(work.columns) != list(BAR_COLUMNS):
        raise DataQualityError(
            f"{symbol}: columns must be exactly {list(BAR_COLUMNS)}, got {list(work.columns)}"
        )
    wrong_dtype = [c for c in BAR_COLUMNS if work[c].dtype != "float64"]
    if wrong_dtype:
        raise DataQualityError(f"{symbol}: columns not float64: {wrong_dtype}")
    if not work.index.is_monotonic_increasing:
        raise DataQualityError(f"{symbol}: index is not sorted ascending")

    is_crypto = "/" in symbol
    report = QualityReport(symbol=symbol, n_bars=0)

    # Duplicates: keep last, within this fetch only.
    dup_mask = work.index.duplicated(keep="last")
    if dup_mask.any():
        report.duplicates_dropped = int(dup_mask.sum())
        _warn(report, f"dropped {report.duplicates_dropped} duplicate timestamp(s)")
        work = work[~dup_mask]

    # Closed bars only: drop any bar labelled after the last closed bar at `now`.
    if len(work):
        cutoff = last_closed_bar_date(symbol, now, equity_close_buffer_min)
        label_dates = work.index.tz_convert(UTC).normalize()
        in_progress = label_dates > pd.Timestamp(cutoff, tz=UTC)
        if in_progress.any():
            dropped = [ts.date() for ts in work.index[in_progress]]
            _warn(
                report,
                f"dropped in-progress bar(s) after {cutoff.isoformat()}: {_fmt_dates(dropped)}",
            )
            work = work[~in_progress]

    # Equity rows on non-session days (phantom weekend/holiday rows): drop and warn.
    if not is_crypto and len(work):
        labels = work.index.tz_convert(UTC).normalize()
        valid_days = sessions(symbol, work.index[0].date(), work.index[-1].date())
        phantom = ~labels.isin(valid_days)
        if phantom.any():
            dropped = [ts.date() for ts in work.index[phantom]]
            _warn(
                report,
                f"dropped {len(dropped)} bar(s) on non-session days: {_fmt_dates(dropped)}",
            )
            work = work[~phantom]

    errors: list[str] = []

    # Non-positive, NaN or infinite prices (infinite reported separately).
    prices = work[list(_PRICE_COLUMNS)]
    inf_price = np.isinf(prices).any(axis=1)
    bad_price = (prices.isna() | (prices <= 0)).any(axis=1) | inf_price
    nonpos = bad_price & ~inf_price
    if nonpos.any():
        bad_dates = [ts.date() for ts in work.index[nonpos]]
        errors.append(
            f"{len(bad_dates)} row(s) with non-positive or NaN price: {_fmt_dates(bad_dates)}"
        )
    if inf_price.any():
        bad_dates = [ts.date() for ts in work.index[inf_price]]
        errors.append(f"{len(bad_dates)} row(s) with infinite price: {_fmt_dates(bad_dates)}")

    # OHLC sanity, checked only on rows whose prices are valid.
    valid = ~bad_price
    body_low = work[["open", "close"]].min(axis=1)
    body_high = work[["open", "close"]].max(axis=1)
    bad_ohlc = valid & ((work["low"] > body_low) | (work["high"] < body_high))
    if bad_ohlc.any():
        bad_dates = [ts.date() for ts in work.index[bad_ohlc]]
        errors.append(
            f"{len(bad_dates)} row(s) violating low<=min(open,close) or "
            f"high>=max(open,close): {_fmt_dates(bad_dates)}"
        )

    # Volume must be >= 0 (NaN is also a violation) and finite.
    bad_volume = work["volume"].isna() | (work["volume"] < 0)
    if bad_volume.any():
        bad_dates = [ts.date() for ts in work.index[bad_volume]]
        errors.append(
            f"{len(bad_dates)} row(s) with negative or NaN volume: {_fmt_dates(bad_dates)}"
        )
    inf_volume = np.isinf(work["volume"])
    if inf_volume.any():
        bad_dates = [ts.date() for ts in work.index[inf_volume]]
        errors.append(f"{len(bad_dates)} row(s) with infinite volume: {_fmt_dates(bad_dates)}")

    # Gaps: interior missing sessions (equity) or UTC days (crypto) are errors.
    if len(work):
        first, last = work.index[0].date(), work.index[-1].date()
        expected = sessions(symbol, first, last)
        present = work.index.tz_convert(UTC).normalize()
        missing = expected.difference(present)
        report.gaps = [ts.date() for ts in missing]
        if len(report.gaps) > MAX_GAP_SESSIONS:
            errors.append(
                f"{len(report.gaps)} interior gap(s) vs expected bars: {_fmt_dates(report.gaps)}"
            )

        # Outliers: flag (do not fail) on large close-to-close moves.
        threshold = _OUTLIER_CRYPTO if is_crypto else _OUTLIER_EQUITY
        returns = work["close"] / work["close"].shift(1) - 1.0
        big = returns.abs() > threshold
        for ts in work.index[big.to_numpy()]:
            move = float(returns.loc[ts])
            report.outliers.append((ts.date(), move))
            _warn(report, f"outlier move on {ts.date().isoformat()}: {move:+.1%}")

        # Zero volume on an equity session day: warning only.
        if not is_crypto:
            on_session = present.isin(expected)
            zero_vol = (work["volume"] == 0).to_numpy() & on_session
            if zero_vol.any():
                zero_dates = [ts.date() for ts in work.index[zero_vol]]
                _warn(
                    report,
                    f"{len(zero_dates)} zero-volume session bar(s): {_fmt_dates(zero_dates)}",
                )

    if errors:
        raise DataQualityError(f"{symbol}: " + "; ".join(errors))

    clean = work[list(BAR_COLUMNS)].copy()
    report.n_bars = len(clean)
    return clean, report
