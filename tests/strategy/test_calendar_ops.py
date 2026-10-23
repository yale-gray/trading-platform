"""Calendar, FOMC event and session operands (ADR-0001 A29): values and causality."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from support.intraday_synth import frame_from_closes, multi_session_index

from tradebot.data.calendar import sessions
from tradebot.data.events import fomc_dates
from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.schema import RulesSpec

pytestmark = pytest.mark.unit


def spec(entry: dict, tf: str) -> RulesSpec:
    return RulesSpec.model_validate(
        {
            "id": "c",
            "name": "c",
            "asset_class": "equity",
            "timeframe": tf,
            "universe": ["AAA"],
            "schedule": {"freq": "bar" if tf == "15m" else "daily"},
            "entry": entry,
        }
    )


def daily_bars(start: date, end: date) -> pd.DataFrame:
    idx = sessions("SPY", start, end)
    c = np.linspace(100, 110, len(idx))
    return pd.DataFrame(
        {"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0}, index=idx
    )


def feature(s: RulesSpec, bars: pd.DataFrame, operand: dict) -> pd.Series:
    cs = compile_strategy(spec({"gt": [operand, -1e9]}, s.timeframe))
    feats = cs.prepare({"AAA": bars})
    (arr,) = [v for k, v in feats.arrays.items()]
    return pd.Series(arr, index=feats.calendar)


# --------------------------------------------------------------------------- the fomc file


def test_fomc_file_is_complete_scheduled_only_and_on_trading_days() -> None:
    dates = fomc_dates()
    assert list(dates) == sorted(set(dates))
    per_year = pd.Series([d.year for d in dates]).value_counts().sort_index()
    assert list(per_year.index) == list(range(2015, 2028))
    assert (per_year == 8).sum() == 12 and per_year[2020] == 7
    for unscheduled in (date(2019, 10, 4), date(2020, 3, 3), date(2020, 3, 15), date(2020, 3, 18)):
        assert unscheduled not in dates
    ses = set(sessions("SPY", date(2015, 1, 1), date(2027, 12, 31)).date)
    assert all(d in ses for d in dates)
    assert date(2024, 1, 31) in dates and date(2024, 3, 20) in dates


# --------------------------------------------------------------------------- calendar


def test_calendar_operands_on_daily_bars() -> None:
    bars = daily_bars(date(2024, 1, 2), date(2024, 3, 28))
    s = spec({"always": True}, "1d")
    dom = feature(s, bars, {"calendar": "tday_of_month"})
    end = feature(s, bars, {"calendar": "tdays_to_month_end"})
    at = lambda ser, d: ser.loc[pd.Timestamp(d, tz="UTC")]  # noqa: E731
    assert at(dom, "2024-01-02") == 1  # Jan 1 is a holiday
    assert at(dom, "2024-02-01") == 1 and at(dom, "2024-02-02") == 2
    assert at(end, "2024-01-31") == 0 and at(end, "2024-01-30") == 1
    assert at(end, "2024-02-29") == 0
    assert at(dom, "2024-02-29") == 20  # Feb 2024 has 20 sessions (Presidents Day)
    assert at(end, "2024-02-01") == 19


def test_calendar_operands_on_15m_bars_use_the_new_york_day() -> None:
    idx = multi_session_index(date(2024, 1, 30), date(2024, 2, 2))
    bars = frame_from_closes(idx, [100.0] * len(idx))
    s = spec({"always": True}, "15m")
    end = feature(s, bars, {"calendar": "tdays_to_month_end"})
    ny = end.index.tz_convert("America/New_York").date
    assert set(end[ny == date(2024, 1, 30)]) == {1.0}
    assert set(end[ny == date(2024, 1, 31)]) == {0.0}
    assert set(end[ny == date(2024, 2, 1)]) == {19.0}


def test_fomc_tdays_until() -> None:
    bars = daily_bars(date(2024, 1, 29), date(2024, 2, 2))
    s = spec({"always": True}, "1d")
    f = feature(s, bars, {"event": "fomc", "field": "tdays_until"})
    got = {d.date().isoformat(): v for d, v in f.items()}
    assert got["2024-01-29"] == 2 and got["2024-01-30"] == 1 and got["2024-01-31"] == 0
    # Feb 1 -> next statement 2024-03-20: 20 Feb sessions + 13 March sessions before it.
    assert got["2024-02-01"] == 33


def test_fomc_is_nan_after_the_last_committed_date() -> None:
    bars = daily_bars(date(2027, 12, 6), date(2028, 1, 5))
    f = feature(spec({"always": True}, "1d"), bars, {"event": "fomc", "field": "tdays_until"})
    assert f.loc[pd.Timestamp("2027-12-08", tz="UTC")] == 0
    assert f.loc[pd.Timestamp("2027-12-07", tz="UTC")] == 1
    assert (
        f.loc[pd.Timestamp("2027-12-09", tz="UTC")] != f.loc[pd.Timestamp("2027-12-09", tz="UTC")]
    )


# --------------------------------------------------------------------------- session values


def _two_day_bars() -> pd.DataFrame:
    idx = multi_session_index(date(2024, 1, 8), date(2024, 1, 9))
    n = len(idx)
    closes = [100.0 + k for k in range(26)] + [200.0 + k for k in range(n - 26)]
    # opens: Tuesday opens at 120 (a gap against Monday's last close 125)
    opens = [closes[0]] + closes[: 26 - 1] + [120.0] + closes[26 : n - 1]
    vol = [10.0] * 26 + [float(1 + k) for k in range(n - 26)]
    return frame_from_closes(idx, closes, opens=opens, volume=vol)


def test_prev_close_gap_and_vwap_values() -> None:
    bars = _two_day_bars()
    s = spec({"always": True}, "15m")
    prev = feature(s, bars, {"session": "prev_close"})
    gap = feature(s, bars, {"session": "gap"})
    vwap = feature(s, bars, {"session": "vwap"})
    assert prev.iloc[:26].isna().all() and gap.iloc[:26].isna().all()
    assert (prev.iloc[26:] == 125.0).all()
    assert gap.iloc[26:].tolist() == pytest.approx([120.0 / 125.0 - 1.0] * 26)  # -4%
    # vwap, Tuesday bar 0: typical = (h + l + c) / 3 with h = max(o, c) + .1, l = min(o, c) - .1
    b0 = bars.iloc[26]
    typ0 = (b0["high"] + b0["low"] + b0["close"]) / 3
    assert vwap.iloc[26] == pytest.approx(typ0)
    b1 = bars.iloc[27]
    typ1 = (b1["high"] + b1["low"] + b1["close"]) / 3
    assert vwap.iloc[27] == pytest.approx((typ0 * 1 + typ1 * 2) / 3)  # volumes 1 and 2
    # Monday's vwap does not leak into Tuesday's: bar 0 equals its own typical price.
    assert vwap.iloc[0] == pytest.approx((bars.iloc[0][["high", "low", "close"]].sum()) / 3)


# --------------------------------------------------------------------------- causality

OPERANDS = [
    {"session": "prev_close"},
    {"session": "gap"},
    {"session": "vwap"},
    {"ind": "noise_upper", "period": 2},
    {"ind": "noise_lower", "period": 2},
    {"calendar": "tday_of_month"},
    {"calendar": "tdays_to_month_end"},
    {"event": "fomc", "field": "tdays_until"},
]


@pytest.mark.parametrize("operand", OPERANDS, ids=lambda o: "_".join(map(str, o.values())))
def test_new_operands_are_truncation_invariant_and_ignore_future_poison(operand: dict) -> None:
    idx = multi_session_index(date(2024, 1, 29), date(2024, 2, 7))
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(size=len(idx)))
    bars = frame_from_closes(idx, closes, volume=list(rng.uniform(1, 9, len(idx))))
    s = spec({"always": True}, "15m")
    full = feature(s, bars, operand)
    for t in (30, 60, 90, 130):
        cut = bars.iloc[: t + 1]
        trunc = feature(s, cut, operand)
        np.testing.assert_allclose(trunc.to_numpy(), full.iloc[: t + 1].to_numpy(), equal_nan=True)
        poisoned = bars.copy()
        poisoned.iloc[t + 1 :, :] = 1e6
        pz = feature(s, poisoned, operand)
        np.testing.assert_allclose(
            pz.iloc[: t + 1].to_numpy(), full.iloc[: t + 1].to_numpy(), equal_nan=True
        )
