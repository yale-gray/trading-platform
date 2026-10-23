"""Noise-area bands (ADR-0001 A29): ``open_today * (1 +/- sigma)``, sigma from earlier sessions."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from support.intraday_synth import frame_from_closes, multi_session_index

from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.indicators import noise_lower, noise_upper
from tradebot.strategy.schema import RulesSpec

pytestmark = pytest.mark.unit

MOVES = [0.01, 0.03, 0.05, 0.02]  # session d closes every bar at open_d * (1 + MOVES[d])
OPENS = [100.0, 110.0, 120.0, 130.0]


def _bars() -> pd.DataFrame:
    idx = multi_session_index(date(2024, 1, 8), date(2024, 1, 11))
    o = np.repeat(OPENS, 26)
    c = o * (1 + np.repeat(MOVES, 26))
    return frame_from_closes(idx, c, opens=o)


def test_bands_match_a_hand_calculation() -> None:
    b = _bars()
    up, lo = noise_upper(b["open"], b["close"], 2), noise_lower(b["open"], b["close"], 2)
    assert up.iloc[:52].isna().all() and lo.iloc[:52].isna().all()  # needs 2 earlier sessions
    # session 2: sigma = mean(0.01, 0.03) = 0.02, open 120
    assert up.iloc[52:78].tolist() == pytest.approx([120 * 1.02] * 26)
    assert lo.iloc[52:78].tolist() == pytest.approx([120 * 0.98] * 26)
    # session 3: sigma = mean(0.03, 0.05) = 0.04, open 130
    assert up.iloc[78:].tolist() == pytest.approx([130 * 1.04] * 26)
    assert lo.iloc[78:].tolist() == pytest.approx([130 * 0.96] * 26)


def test_half_day_does_not_blank_the_following_sessions() -> None:
    b = _bars()
    # session 1 becomes a half day: its last 12 bars (bar-of-day 14..25) do not exist
    keep = np.ones(len(b), dtype=bool)
    keep[26 + 14 : 52] = False
    h = b.loc[keep]
    up = noise_upper(h["open"], h["close"], 2)
    assert up.isna().sum() == 26 + 14 + 12  # sessions 0-1 (no history) + session 2's 12 late bars
    s2 = up.iloc[40:66]  # session 2 rows: bars 0-13 have sessions 0,1; bars 14-25 only session 0
    assert s2.iloc[:14].tolist() == pytest.approx([120 * 1.02] * 14)
    assert s2.iloc[14:].isna().all()
    s3 = up.iloc[66:]  # session 3 late bars use sessions 0 and 2 (session 1 has no such bar)
    assert s3.iloc[:14].tolist() == pytest.approx([130 * 1.04] * 14)
    assert s3.iloc[14:].tolist() == pytest.approx([130 * 1.03] * 12)


def test_sigma_ignores_the_current_and_later_sessions() -> None:
    b = _bars()
    base = noise_upper(b["open"], b["close"], 2)
    poisoned = b.copy()
    poisoned.iloc[78:, 1:] = 1e6  # session 3 high/low/close/volume garbage; its open stays
    again = noise_upper(poisoned["open"], poisoned["close"], 2)
    assert again.iloc[:78].fillna(0).tolist() == base.iloc[:78].fillna(0).tolist()
    # Session 3's own closes are not in its sigma: only its open (unchanged) and sessions 1-2.
    assert again.iloc[78:].tolist() == pytest.approx(base.iloc[78:].tolist())


def test_noise_indicator_through_the_rules_schema_and_engine() -> None:
    spec = RulesSpec.model_validate(
        {
            "id": "nb",
            "name": "nb",
            "asset_class": "equity",
            "timeframe": "15m",
            "universe": ["AAA"],
            "schedule": {"freq": "bar"},
            "entry": {"gt": [{"price": "close"}, {"ind": "noise_upper", "period": 2}]},
            "exit": {"lt": [{"price": "close"}, {"ind": "noise_lower", "period": 2}]},
        }
    )
    assert spec.warmup_bars == 2 * 26 + 2
    feats = compile_strategy(spec).prepare({"AAA": _bars()})
    key = next(k for k in feats.arrays if k[2] == "noise_upper")
    assert feats.arrays[key][78] == pytest.approx(130 * 1.04)


def test_noise_needs_a_15m_strategy() -> None:
    with pytest.raises(ValueError, match="15m"):
        RulesSpec.model_validate(
            {
                "id": "nb",
                "name": "nb",
                "asset_class": "equity",
                "universe": ["AAA"],
                "schedule": {"freq": "daily"},
                "entry": {"gt": [{"price": "close"}, {"ind": "noise_upper", "period": 2}]},
            }
        )
