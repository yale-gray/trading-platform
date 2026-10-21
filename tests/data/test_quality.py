"""Unit tests for per-fetch validation (SPEC-001 AC 7, Section 5, T4).

Small in-test frames cover each rule. Four edge-case fixtures (ZZGAP, ZZDUP, ZZTZ, ZZBAD)
and the clean SPY fixture exercise the same rules on committed synthetic data.
Hand-derived expectations are in comments. Calendar facts: 2024-07-04 (Thursday) is an
NYSE holiday; 2024-07-01 is a Monday.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradebot.data.errors import DataQualityError
from tradebot.data.quality import QualityReport, validate_bars
from tradebot.data.types import BAR_COLUMNS

pytestmark = pytest.mark.unit

ET = ZoneInfo("America/New_York")
# Well after every frame below, so no test bar is in progress unless it says so.
NOW = datetime(2025, 1, 6, 12, 0, tzinfo=UTC)


def _row(day: str, close: float, volume: float = 1000.0) -> tuple:
    """A valid bar: open == close, high = close + 1, low = close - 1."""
    return (day, close, close + 1.0, close - 1.0, close, volume)


def _frame(rows: list[tuple]) -> pd.DataFrame:
    """Build a canonical frame: UTC index named ts, five float64 columns."""
    index = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows]).tz_localize("UTC")
    data = [r[1:] for r in rows]
    return pd.DataFrame(data, index=index, columns=list(BAR_COLUMNS), dtype="float64").rename_axis(
        "ts"
    )


def _equity_days(*days: str) -> list[tuple]:
    return [_row(d, 100.0) for d in days]


def _load(root: Path, symbol: str) -> pd.DataFrame:
    return pd.read_parquet(root / "yfinance" / symbol / "gen-0001" / "bars.parquet")


def _at(clean: pd.DataFrame, day: str) -> pd.Series:
    """Return the single bar labelled ``day`` (UTC midnight) as a Series."""
    return clean.loc[pd.Timestamp(day, tz="UTC")]


# --- clean output and metadata -------------------------------------------


def test_clean_frame_strips_metadata_and_returns_five_bar_columns():
    # 2024-07-04 is a holiday, so 07-01, 07-02, 07-03, 07-05 are the four SPY sessions.
    df = _frame(_equity_days("2024-07-01", "2024-07-02", "2024-07-03", "2024-07-05"))
    df["fetched_at"] = pd.Timestamp("2025-01-01", tz="UTC")
    df["source_rev"] = "synthetic-v1"

    clean, report = validate_bars(df, "SPY", NOW)

    assert list(clean.columns) == list(BAR_COLUMNS)
    assert all(clean[c].dtype == "float64" for c in BAR_COLUMNS)
    assert len(clean) == 4
    assert report.n_bars == 4
    assert report.gaps == []
    assert isinstance(report, QualityReport)


def test_equity_holiday_is_not_a_gap():
    # 2024-07-04 absent from a SPY series is correct, not an interior gap.
    df = _frame(_equity_days("2024-07-03", "2024-07-05", "2024-07-08"))
    clean, report = validate_bars(df, "SPY", NOW)
    assert len(clean) == 3
    assert report.gaps == []


def test_crypto_contiguous_days_including_weekend_and_holiday_pass():
    # Crypto trades every UTC day: 07-04 (holiday) and the weekend are real bars.
    days = ["2024-07-05", "2024-07-06", "2024-07-07", "2024-07-08"]
    df = _frame([_row(d, 100.0) for d in days])
    clean, report = validate_bars(df, "BTC/USD", NOW)
    assert len(clean) == 4
    assert report.gaps == []


# --- schema -----------------------------------------------------------------


def test_naive_index_is_rejected():
    df = _frame(_equity_days("2024-07-01", "2024-07-02"))
    df.index = df.index.tz_localize(None)
    with pytest.raises(DataQualityError, match="tz-aware"):
        validate_bars(df, "SPY", NOW)


def test_non_utc_index_is_rejected():
    df = _frame(_equity_days("2024-07-01", "2024-07-02"))
    df.index = df.index.tz_convert("America/New_York")
    with pytest.raises(DataQualityError, match="must be UTC"):
        validate_bars(df, "SPY", NOW)


def test_extra_column_is_rejected():
    df = _frame(_equity_days("2024-07-01", "2024-07-02"))
    df["adj_close"] = 100.0
    with pytest.raises(DataQualityError, match="columns must be exactly"):
        validate_bars(df, "SPY", NOW)


def test_missing_column_is_rejected():
    df = _frame(_equity_days("2024-07-01", "2024-07-02")).drop(columns=["volume"])
    with pytest.raises(DataQualityError, match="columns must be exactly"):
        validate_bars(df, "SPY", NOW)


def test_non_float64_column_is_rejected():
    df = _frame(_equity_days("2024-07-01", "2024-07-02"))
    df["volume"] = df["volume"].astype("int64")
    with pytest.raises(DataQualityError, match="not float64"):
        validate_bars(df, "SPY", NOW)


def test_unsorted_index_is_rejected():
    df = _frame(_equity_days("2024-07-01", "2024-07-02", "2024-07-03"))
    with pytest.raises(DataQualityError, match="not sorted"):
        validate_bars(df.iloc[::-1], "SPY", NOW)


# --- hard failures ----------------------------------------------------------


def test_zero_close_is_non_positive_price_error():
    df = _frame([_row("2024-07-01", 100.0), ("2024-07-02", 100.0, 101.0, 99.0, 0.0, 1000.0)])
    with pytest.raises(DataQualityError, match="non-positive or NaN price"):
        validate_bars(df, "SPY", NOW)


def test_negative_price_is_non_positive_price_error():
    df = _frame([_row("2024-07-01", 100.0), ("2024-07-02", -1.0, 0.0, -2.0, -1.0, 1000.0)])
    with pytest.raises(DataQualityError, match="non-positive or NaN price"):
        validate_bars(df, "SPY", NOW)


def test_nan_price_is_error():
    df = _frame([_row("2024-07-01", 100.0), ("2024-07-02", 100.0, 101.0, 99.0, float("nan"), 1.0)])
    with pytest.raises(DataQualityError, match="NaN price"):
        validate_bars(df, "SPY", NOW)


def test_low_above_open_close_is_ohlc_error():
    # open = close = 100, so low must be <= 100. Here low = 100.5.
    df = _frame([_row("2024-07-01", 100.0), ("2024-07-02", 100.0, 101.0, 100.5, 100.0, 1000.0)])
    with pytest.raises(DataQualityError, match="low<=min"):
        validate_bars(df, "SPY", NOW)


def test_high_below_open_close_is_ohlc_error():
    # open = close = 100, so high must be >= 100. Here high = 99.5 (low 99 is fine).
    df = _frame([_row("2024-07-01", 100.0), ("2024-07-02", 100.0, 99.5, 99.0, 100.0, 1000.0)])
    with pytest.raises(DataQualityError, match="high>=max"):
        validate_bars(df, "SPY", NOW)


def test_negative_volume_is_error():
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 100.0, volume=-1.0)])
    with pytest.raises(DataQualityError, match="negative or NaN volume"):
        validate_bars(df, "SPY", NOW)


def test_nan_volume_is_error():
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 100.0, volume=float("nan"))])
    with pytest.raises(DataQualityError, match="negative or NaN volume"):
        validate_bars(df, "SPY", NOW)


def test_error_message_lists_every_violation():
    df = _frame(
        [
            ("2024-07-01", 100.0, 101.0, 99.0, 0.0, 1000.0),  # non-positive close
            _row("2024-07-02", 100.0, volume=-5.0),  # negative volume
        ]
    )
    with pytest.raises(DataQualityError) as excinfo:
        validate_bars(df, "SPY", NOW)
    message = str(excinfo.value)
    assert "non-positive or NaN price" in message
    assert "negative or NaN volume" in message


def test_equity_interior_gap_is_error_listing_the_missing_session():
    # SPY sessions 07-08..07-12 are Mon..Fri; 07-09 (Tue) is missing.
    df = _frame(_equity_days("2024-07-08", "2024-07-10", "2024-07-11", "2024-07-12"))
    with pytest.raises(DataQualityError, match="1 interior gap") as excinfo:
        validate_bars(df, "SPY", NOW)
    assert "2024-07-09" in str(excinfo.value)


def test_crypto_interior_gap_is_error_listing_the_missing_day():
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 100.0), _row("2024-07-04", 100.0)])
    with pytest.raises(DataQualityError, match="interior gap") as excinfo:
        validate_bars(df, "BTC/USD", NOW)
    assert "2024-07-03" in str(excinfo.value)


def test_equity_leading_and_trailing_gaps_are_ignored():
    # Series starts 07-08 (07-01..07-05 not present) and ends 07-10 (07-11, 07-12 absent).
    # Only interior gaps count, so this passes.
    df = _frame(_equity_days("2024-07-08", "2024-07-09", "2024-07-10"))
    clean, report = validate_bars(df, "SPY", NOW)
    assert len(clean) == 3
    assert report.gaps == []


# --- duplicates -------------------------------------------------------------


def test_duplicate_timestamps_dropped_keep_last_with_warning(caplog: pytest.LogCaptureFixture):
    df = _frame(
        [
            _row("2024-07-01", 100.0),
            ("2024-07-01", 105.0, 106.0, 104.0, 105.0, 2000.0),  # later row, should survive
            _row("2024-07-02", 100.0),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="tradebot.data.quality"):
        clean, report = validate_bars(df, "SPY", NOW)

    assert len(clean) == 2
    assert _at(clean, "2024-07-01")["close"] == 105.0
    assert _at(clean, "2024-07-01")["volume"] == 2000.0
    assert report.duplicates_dropped == 1
    assert any("duplicate" in w for w in report.warnings)
    assert any("duplicate" in r.getMessage() for r in caplog.records)


# --- in-progress bar --------------------------------------------------------


def test_equity_bar_inside_close_buffer_is_dropped():
    # 2024-07-02 closes 16:00 ET; buffer 60 min => closed at 17:00 ET. At 15:00 ET it is
    # still in progress, so the 07-02 bar goes and 07-01 stays.
    now = datetime(2024, 7, 2, 15, 0, tzinfo=ET)
    df = _frame(_equity_days("2024-07-01", "2024-07-02"))
    clean, report = validate_bars(df, "SPY", now)
    assert list(clean.index.date) == [date(2024, 7, 1)]
    assert report.n_bars == 1
    assert any("in-progress" in w for w in report.warnings)


def test_equity_bar_kept_exactly_at_close_plus_buffer():
    # 17:00 ET is exactly close (16:00) + 60 min, so the 07-02 bar is closed and kept.
    now = datetime(2024, 7, 2, 17, 0, tzinfo=ET)
    df = _frame(_equity_days("2024-07-01", "2024-07-02"))
    clean, report = validate_bars(df, "SPY", now)
    assert list(clean.index.date) == [date(2024, 7, 1), date(2024, 7, 2)]
    assert not any("in-progress" in w for w in report.warnings)


def test_crypto_todays_utc_bar_is_dropped_with_warning(caplog: pytest.LogCaptureFixture):
    now = datetime(2024, 7, 2, 12, 0, tzinfo=UTC)
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 100.0)])
    with caplog.at_level(logging.WARNING, logger="tradebot.data.quality"):
        clean, report = validate_bars(df, "BTC/USD", now)
    assert list(clean.index.date) == [date(2024, 7, 1)]
    assert any("in-progress" in w for w in report.warnings)
    assert any("in-progress" in r.getMessage() for r in caplog.records)


def test_crypto_previous_day_present_just_after_utc_midnight():
    # At 00:00:01 UTC on 07-02, the 07-01 bar has just closed; 07-02 is still open.
    now = datetime(2024, 7, 2, 0, 0, 1, tzinfo=UTC)
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 100.0)])
    clean, _ = validate_bars(df, "BTC/USD", now)
    assert list(clean.index.date) == [date(2024, 7, 1)]


# --- outliers (flag, never fail) --------------------------------------------


def test_equity_move_of_exactly_50_pct_is_not_flagged():
    # 150 / 100 - 1 = 0.5, and the rule is abs(move) > 0.5.
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 150.0)])
    _, report = validate_bars(df, "SPY", NOW)
    assert report.outliers == []


def test_equity_move_over_50_pct_is_flagged_not_raised():
    # 151 / 100 - 1 = 0.51 > 0.5.
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 151.0)])
    clean, report = validate_bars(df, "SPY", NOW)
    assert len(report.outliers) == 1
    day, move = report.outliers[0]
    assert day == date(2024, 7, 2)
    assert move == pytest.approx(0.51)
    assert len(clean) == 2  # the flagged bar is still returned
    assert any("outlier" in w for w in report.warnings)


def test_crypto_move_of_55_pct_is_not_flagged():
    # Crypto threshold is 0.6, so 155 / 100 - 1 = 0.55 passes.
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 155.0)])
    _, report = validate_bars(df, "BTC/USD", NOW)
    assert report.outliers == []


def test_crypto_move_of_61_pct_is_flagged():
    # 161 / 100 - 1 = 0.61 > 0.6.
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 161.0)])
    clean, report = validate_bars(df, "BTC/USD", NOW)
    assert [day for day, _ in report.outliers] == [date(2024, 7, 2)]
    assert len(clean) == 2


# --- zero volume ------------------------------------------------------------


def test_equity_zero_volume_session_warns_without_raising():
    df = _frame([_row("2024-07-01", 100.0, volume=0.0), _row("2024-07-02", 100.0)])
    clean, report = validate_bars(df, "SPY", NOW)
    assert len(clean) == 2
    assert any("zero-volume" in w for w in report.warnings)


def test_crypto_zero_volume_does_not_warn():
    df = _frame([_row("2024-07-01", 100.0, volume=0.0), _row("2024-07-02", 100.0)])
    _, report = validate_bars(df, "BTC/USD", NOW)
    assert not any("zero-volume" in w for w in report.warnings)


# --- edge-case fixtures (committed synthetic data) ---------------------------


def test_fixture_zzgap_raises_on_six_missing_interior_sessions(edge_cache: Path):
    # ZZGAP is 503 NYSE sessions 2018-01-02..2019-12-31 with 497 present. The missing
    # sessions are 2019-07-08 (Mon) .. 2019-07-12 (Fri) and 2019-07-15 (Mon): six in all.
    df = _load(edge_cache, "ZZGAP")
    with pytest.raises(DataQualityError, match="6 interior gap") as excinfo:
        validate_bars(df, "ZZGAP", NOW)
    assert "2019-07-08" in str(excinfo.value)


def test_fixture_zzdup_drops_duplicate_keeping_last(edge_cache: Path):
    # ZZDUP has 504 rows; 2019-03-12 appears twice. The second row (high = close = 119.0287)
    # is kept, so the result has 503 sessions and the kept close is 119.0287.
    df = _load(edge_cache, "ZZDUP")
    clean, report = validate_bars(df, "ZZDUP", NOW)
    assert len(clean) == 503
    assert report.duplicates_dropped == 1
    kept = _at(clean, "2019-03-12")
    assert kept["close"] == pytest.approx(119.0287)
    assert kept["high"] == pytest.approx(119.0287)
    assert list(clean.columns) == list(BAR_COLUMNS)


def test_fixture_zztz_rejects_non_utc_index(edge_cache: Path):
    df = _load(edge_cache, "ZZTZ")
    assert str(df.index.tz) == "America/New_York"
    with pytest.raises(DataQualityError, match="must be UTC"):
        validate_bars(df, "ZZTZ", NOW)


def test_fixture_zzbad_rejects_zero_close(edge_cache: Path):
    # ZZBAD has one bar, 2019-05-16, with close = 0.0.
    df = _load(edge_cache, "ZZBAD")
    with pytest.raises(DataQualityError, match="non-positive or NaN price"):
        validate_bars(df, "ZZBAD", NOW)


def test_fixture_spy_validates_clean(market_cache: Path):
    # Manifest: yfinance/SPY/gen-0001/bars.parquet has 1761 rows, 2018-01-02..2024-12-31.
    df = _load(market_cache, "SPY")
    clean, report = validate_bars(df, "SPY", NOW)
    assert len(clean) == 1761
    assert report.n_bars == 1761
    assert report.gaps == []
    assert report.duplicates_dropped == 0
    assert list(clean.columns) == list(BAR_COLUMNS)
    assert str(clean.index.tz) == "UTC"


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_infinite_price_is_error(bad: float):
    df = _frame([_row("2024-07-01", 100.0), ("2024-07-02", 100.0, bad, 99.0, 100.0, 1.0)])
    with pytest.raises(DataQualityError, match="infinite price"):
        validate_bars(df, "SPY", NOW)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_infinite_volume_is_error(bad: float):
    df = _frame([_row("2024-07-01", 100.0), _row("2024-07-02", 100.0, volume=bad)])
    with pytest.raises(DataQualityError, match="infinite volume"):
        validate_bars(df, "SPY", NOW)


def test_equity_bar_on_non_session_day_is_dropped_with_warning():
    # 2024-07-04 is a holiday (Thu) and 2024-07-06 a Saturday: neither may reach the cache.
    rows = [
        _row("2024-07-02", 100.0),
        _row("2024-07-03", 101.0),
        _row("2024-07-04", 102.0),
        _row("2024-07-05", 103.0),
        _row("2024-07-06", 104.0),
    ]
    clean, report = validate_bars(_frame(rows), "SPY", NOW)
    assert [d.isoformat() for d in clean.index.date] == ["2024-07-02", "2024-07-03", "2024-07-05"]
    assert report.gaps == []
    assert any("non-session" in w and "2024-07-04" in w for w in report.warnings)


def test_crypto_weekend_bars_are_not_dropped():
    rows = [_row("2024-07-05", 100.0), _row("2024-07-06", 101.0), _row("2024-07-07", 102.0)]
    clean, report = validate_bars(_frame(rows), "BTC/USD", NOW)
    assert len(clean) == 3
    assert not any("non-session" in w for w in report.warnings)


def test_close_buffer_parameter_controls_in_progress_cutoff():
    # 17:00 ET is close + 60 min: kept with the default, dropped with a 120 min buffer.
    now = datetime(2024, 7, 2, 17, 0, tzinfo=ET)
    df = _frame(_equity_days("2024-07-01", "2024-07-02"))
    kept, _ = validate_bars(df, "SPY", now)
    dropped, _ = validate_bars(df, "SPY", now, equity_close_buffer_min=120)
    assert len(kept) == 2
    assert len(dropped) == 1


def test_report_status_defaults_to_ok():
    _, report = validate_bars(_frame([_row("2024-07-01", 100.0)]), "SPY", NOW)
    assert report.status == "ok"
