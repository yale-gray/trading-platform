"""Clock-only rules have no data lag (ADR-0001 A28)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from support.intraday_synth import frame_from_closes, multi_session_index
from test_intraday_driver import _cfg, _run_driver, hold_spec

from tradebot.portfolio.models import EventType, FillSide
from tradebot.strategy.schema import RulesSpec

pytestmark = pytest.mark.unit


def spec_of(entry: dict, exit_: dict | None = None, tf: str = "15m", **over: object) -> RulesSpec:
    body: dict = {
        "id": "clk",
        "name": "clk",
        "asset_class": "equity",
        "timeframe": tf,
        "universe": ["AAA"],
        "schedule": {"freq": "bar" if tf == "15m" else "daily"},
        "entry": entry,
    }
    if exit_ is not None:
        body["exit"] = exit_
    body.update(over)
    return RulesSpec.model_validate(body)


BAR_IDX = {"session": "bar_index"}
CAL = {"calendar": "tday_of_month"}
FOMC = {"event": "fomc", "field": "tdays_until"}


@pytest.mark.parametrize(
    ("entry", "exit_", "expected"),
    [
        ({"eq": [BAR_IDX, 3]}, None, True),
        ({"eq": [BAR_IDX, 3]}, {"gt": [BAR_IDX, 10]}, True),
        ({"all": [{"eq": [CAL, 1]}, {"le": [FOMC, 2]}]}, None, True),
        ({"eq": [BAR_IDX, 3]}, {"gt": [{"price": "close"}, 1]}, False),
        ({"gt": [{"ind": "sma", "period": 5}, 1]}, None, False),
        ({"eq": [BAR_IDX, 3]}, {"gt": [{"pos": "bars_held"}, 4]}, False),
        ({"eq": [{"session": "open"}, 3]}, None, False),
        ({"all": [{"eq": [BAR_IDX, 3]}, {"gt": [{"session": "vwap"}, 1]}]}, None, False),
        ({"always": True}, None, False),  # no clock operand at all: normal lag
    ],
)
def test_clock_only_detection(entry: dict, exit_: dict | None, expected: bool) -> None:
    assert spec_of(entry, exit_).clock_only() is expected


def test_a_declared_stop_is_not_clock_only() -> None:
    stop = [
        {"type": "atr_trailing", "period": 14, "mult": 3, "anchor": "highest_close_since_entry"}
    ]
    assert spec_of({"eq": [BAR_IDX, 3]}, stops=stop).clock_only() is False


def test_clock_operands_are_equity_only_and_params_count_as_constants() -> None:
    with pytest.raises(ValueError, match="equities only"):
        RulesSpec.model_validate(
            {
                "id": "c",
                "name": "c",
                "asset_class": "crypto",
                "universe": ["BTC/USD"],
                "schedule": {"freq": "daily"},
                "entry": {"eq": [CAL, 1]},
            }
        )
    s = spec_of({"eq": [BAR_IDX, "$k"]}, parameters={"k": 3.0})
    assert s.clock_only() is True


def _bars() -> dict[str, pd.DataFrame]:
    idx = multi_session_index(date(2024, 1, 5), date(2024, 1, 9))
    return {"AAA": frame_from_closes(idx, [100.0 + 0.01 * k for k in range(len(idx))])}


def _buy_times(spec: RulesSpec, lag: int | None) -> list[str]:
    cfg = _cfg(spec, date(2024, 1, 8), date(2024, 1, 9))
    cfg = cfg.model_copy(update={"decision_lag_bars": lag})
    engine, _, _ = _run_driver(cfg, _bars())
    return [
        e.asof.isoformat()
        for e in engine.ledger
        if e.type is EventType.FILL and e.side is FillSide.BUY
    ]


def test_clock_only_rule_fills_at_t_plus_1_and_a_data_rule_keeps_the_lag() -> None:
    # Decision on bar 3 (09:30 + 45 min = 15:15Z); lag 0 -> the BUY fills at bar 4's open 15:30Z.
    clock = spec_of({"eq": [BAR_IDX, 3]}, {"always": True})
    assert _buy_times(clock, None)[0] == "2024-01-08T15:30:00+00:00"
    # An identical rule that also reads price is not clock-only: default lag 2 -> bar 6 (15:45Z).
    data = spec_of(
        {"all": [{"eq": [BAR_IDX, 3]}, {"gt": [{"price": "close"}, 0]}]}, {"always": True}
    )
    assert _buy_times(data, None)[0] == "2024-01-08T16:00:00+00:00"
    # An explicit lag overrides the clock-only shortcut (used by pinned-fill tests).
    assert _buy_times(clock, 2)[0] == "2024-01-08T16:00:00+00:00"


def test_hold_spec_with_always_keeps_default_lag_two() -> None:
    assert _buy_times(hold_spec(["AAA"]), None)[0] == "2024-01-08T15:15:00+00:00"
