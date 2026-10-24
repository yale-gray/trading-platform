"""``{universe: sp500_member}`` (ADR-0001 A32): values, schema, clock_only and serialisation."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from support.intraday_synth import frame_from_closes, multi_session_index

from tradebot.data import universe as U
from tradebot.data.calendar import sessions
from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.schema import RulesSpec

pytestmark = pytest.mark.unit

MEMBER = {"universe": "sp500_member"}
FAKE = (
    ("AAA", date(2020, 1, 2), None),
    ("BBB", date(2020, 1, 2), date(2020, 1, 8)),
    ("CCC", date(2020, 1, 6), None),
)


@pytest.fixture(autouse=True)
def _fake_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(U, "_sp500_intervals", lambda: FAKE)


def spec(entry: dict, tf: str = "1d", universe: tuple[str, ...] = ("AAA",), **over) -> RulesSpec:
    body = {
        "id": "m",
        "name": "m",
        "asset_class": "equity",
        "timeframe": tf,
        "universe": list(universe),
        "schedule": {"freq": "bar" if tf == "15m" else "daily"},
        "entry": entry,
    }
    body.update(over)
    return RulesSpec.model_validate(body)


def bars(start: date, end: date) -> pd.DataFrame:
    idx = sessions("SPY", start, end)
    c = np.linspace(100, 110, len(idx))
    return pd.DataFrame(
        {"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0}, index=idx
    )


def member_series(sym: str, frame: pd.DataFrame, tf: str = "1d") -> pd.Series:
    s = spec({"eq": [MEMBER, 1]}, tf, (sym,))
    feats = compile_strategy(s).prepare({sym: frame})
    (arr,) = feats.arrays.values()
    return pd.Series(arr, index=feats.calendar)


def test_values_follow_membership_in_force_at_previous_close() -> None:
    frame = bars(date(2020, 1, 2), date(2020, 1, 14))
    ccc = member_series("CCC", frame)
    # CCC's interval starts with the list dated Mon 2020-01-06: first flagged bar is Tue 01-07
    assert ccc.loc["2020-01-06"] == 0.0 and ccc.loc["2020-01-07"] == 1.0
    bbb = member_series("BBB", frame)
    assert bbb.loc["2020-01-08"] == 1.0 and bbb.loc["2020-01-09"] == 0.0
    # before the history starts (previous close before the first row) it is 0, never NaN
    assert member_series("AAA", frame).loc["2020-01-02"] == 0.0
    assert set(ccc.unique()) <= {0.0, 1.0} and not ccc.isna().any()


def test_intraday_uses_the_new_york_session_date() -> None:
    idx = multi_session_index(date(2020, 1, 6), date(2020, 1, 7))
    frame = frame_from_closes(idx, [100.0] * len(idx))
    out = member_series("CCC", frame, "15m")
    ny = out.index.tz_convert("America/New_York").date
    assert (out[ny == date(2020, 1, 6)] == 0.0).all() and (out[ny == date(2020, 1, 7)] == 1.0).all()


def test_entry_gate_end_to_end_in_evaluate() -> None:
    frame = bars(date(2020, 1, 2), date(2020, 1, 14))
    cs = compile_strategy(spec({"eq": [MEMBER, 1]}, universe=("BBB", "CCC")))
    feats = cs.prepare({"BBB": frame, "CCC": frame})
    ts = pd.Timestamp("2020-01-07", tz="UTC")
    held = cs.evaluate(feats, ts, {}, force_decision=True)
    assert sorted(i.symbol for i in held.intents) == ["BBB", "CCC"]
    late = cs.evaluate(feats, pd.Timestamp("2020-01-10", tz="UTC"), {}, force_decision=True)
    assert [i.symbol for i in late.intents] == ["CCC"]


def test_membership_is_data_not_clock() -> None:
    assert spec({"eq": [MEMBER, 1]}).clock_only() is False
    cal = {"calendar": "tday_of_month"}
    assert spec({"eq": [cal, 1]}).clock_only() is True
    assert spec({"all": [{"eq": [cal, 1]}, {"eq": [MEMBER, 1]}]}).clock_only() is False


def test_serialises_only_when_used() -> None:
    plain = spec({"always": True})
    assert "sp500_member" not in plain.model_dump_json(by_alias=True, exclude_none=True)
    used = spec({"eq": [MEMBER, 1]})
    dumped = used.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dumped["entry"] == {"eq": [{"universe": "sp500_member"}, 1.0]}
    assert RulesSpec.model_validate(dumped) == used


def test_schema_rejects_unknown_membership_and_crypto() -> None:
    with pytest.raises(ValueError):
        spec({"eq": [{"universe": "nasdaq_member"}, 1]})
    with pytest.raises(ValueError, match="equities only"):
        spec({"eq": [MEMBER, 1]}, universe=("BTC/USD",), asset_class="crypto")
