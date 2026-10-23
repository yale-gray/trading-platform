"""15-minute strategy schema, session operands, position caps and the two day-trading strategies.

ADR-0001 A21. Hand-built bars; every expected value is derived in a comment.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from support.intraday_synth import frame_from_closes, multi_session_index

from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.io import STRATEGIES_DIR, dump_strategy, load_strategy
from tradebot.strategy.schema import RulesSpec
from tradebot.strategy.signals import HeldPosition

pytestmark = pytest.mark.unit


def rules(**over: object) -> dict:
    base: dict = {
        "id": "t15",
        "name": "t",
        "asset_class": "equity",
        "timeframe": "15m",
        "universe": ["AAA"],
        "schedule": {"freq": "bar"},
        "entry": {"always": True},
    }
    base.update(over)
    return base


# ------------------------------------------------------------------ schema


def test_daily_strategies_serialise_exactly_as_before() -> None:
    spec = load_strategy(STRATEGIES_DIR / "mean_reversion.yaml")
    assert spec.timeframe == "1d"
    assert "timeframe" not in spec.model_dump(mode="json", by_alias=True)
    assert "timeframe" not in dump_strategy(spec, "json")
    assert "max_positions" not in dump_strategy(spec, "json")


def test_15m_strategy_round_trips_with_its_timeframe() -> None:
    spec = RulesSpec.model_validate(rules())
    assert spec.timeframe == "15m" and spec.schedule.freq == "bar"
    text = dump_strategy(spec, "yaml")
    assert "timeframe: 15m" in text
    assert load_strategy(text) == spec


@pytest.mark.parametrize(
    ("over", "msg"),
    [
        ({"schedule": {"freq": "daily"}}, "15m strategy schedules"),
        ({"timeframe": "1d"}, "needs timeframe '15m'"),  # freq 'bar' on a daily strategy
        ({"asset_class": "crypto", "universe": ["BTC/USD"]}, "equities only"),
        (
            {
                "timeframe": "1d",
                "schedule": {"freq": "daily"},
                "entry": {"gt": [{"session": "bar_index"}, 1]},
            },
            "need timeframe",
        ),
    ],
)
def test_invalid_timeframe_combinations_are_rejected(over: dict, msg: str) -> None:
    with pytest.raises(ValidationError, match=msg):
        RulesSpec.model_validate(rules(**over))


def test_session_operand_arguments_are_checked() -> None:
    ok = rules(entry={"gt": [{"price": "close"}, {"session": "range_high", "bars": 4}]})
    assert RulesSpec.model_validate(ok).warmup_bars == 1
    for bad in (
        {"session": "range_high"},
        {"session": "bar_index", "bars": 4},
        {"session": "vwap"},
    ):
        with pytest.raises(ValidationError):
            RulesSpec.model_validate(rules(entry={"gt": [{"price": "close"}, bad]}))


def test_bars_of_a_session_range_can_be_a_parameter() -> None:
    spec = RulesSpec.model_validate(
        rules(
            parameters={"rb": 3},
            entry={"gt": [{"price": "close"}, {"session": "range_high", "bars": "$rb"}]},
        )
    )
    assert spec.resolve().entry.gt[1].bars == 3  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="integer"):
        spec.resolve({"rb": 2.5})


def test_position_cap_needs_a_rank_and_vice_versa() -> None:
    rank = {"by": {"price": "close"}, "order": "desc"}
    with pytest.raises(ValidationError, match="together"):
        RulesSpec.model_validate(rules(position={"max_positions": 2}))
    with pytest.raises(ValidationError, match="together"):
        RulesSpec.model_validate(rules(position={"rank": rank}))
    ok = RulesSpec.model_validate(rules(position={"max_positions": 2, "rank": rank}))
    assert ok.position.max_positions == 2


def test_devatr_indicator_is_a_known_operand_with_its_lookback() -> None:
    spec = RulesSpec.model_validate(rules(entry={"lt": [{"ind": "devatr", "period": 30}, -2]}))
    assert spec.warmup_bars == 31  # max(30, 14) + 1


# ------------------------------------------------------------------ session operands


def _two_session_bars() -> pd.DataFrame:
    """Mon Jan 8 closes 100,101,102,103 then flat 104; Tue Jan 9 gaps down: 90,91,92,93 then 94."""
    idx = multi_session_index(date(2024, 1, 8), date(2024, 1, 9))
    day1 = [100.0, 101.0, 102.0, 103.0] + [104.0] * 22
    day2 = [90.0, 91.0, 92.0, 93.0] + [94.0] * 22
    return frame_from_closes(idx, day1 + day2, spread=0.5)


def test_session_operands_are_per_session_and_causal() -> None:
    entry = {
        "all": [
            {"gt": [{"session": "bar_index"}, -1]},
            {"gt": [{"session": "open"}, 0]},
            {"gt": [{"session": "range_high", "bars": 4}, 0]},
            {"gt": [{"session": "range_low", "bars": 4}, 0]},
        ]
    }
    cs = compile_strategy(RulesSpec.model_validate(rules(entry=entry)))
    bars = _two_session_bars()
    feats = cs.prepare({"AAA": bars})
    get = lambda *k: feats.arrays[("AAA", "session", *k)]  # noqa: E731
    bar_index = get("bar_index", None)
    assert list(bar_index[:3]) == [0, 1, 2] and bar_index[25] == 25 and bar_index[26] == 0
    open_ = get("open", None)
    # Mon opens at 100 (first open = first close); Tue's first open is Monday's last close, 104.
    assert open_[0] == open_[25] == 100.0 and open_[26] == open_[51] == 104.0
    hi, lo = get("range_high", 4), get("range_low", 4)
    # Monday: highs (open/close max + 0.5) are 100.5, 101.5, 102.5, 103.5 -> range high 103.5 once
    # bar 3 completes the range; lows (min - 0.5) are 99.5, 99.5, 100.5, 101.5 -> range low 99.5.
    assert np.isnan(hi[:3]).all() and np.isnan(lo[:3]).all()
    assert (hi[3:26] == 103.5).all() and (lo[3:26] == 99.5).all()
    # Tuesday is a new session: the 104 -> 90 gap is in bar 0 (high 104.5, low 89.5).
    assert np.isnan(hi[26:29]).all()
    assert (hi[29:52] == 104.5).all() and (lo[29:52] == 89.5).all()
    # Causality: poison everything after bar 10; nothing at or before bar 10 may change, and the
    # range (bars 0-3) does not move either.
    poisoned = bars.copy()
    poisoned.iloc[11:, :] = 1e6
    poisoned["low"] = np.minimum(poisoned["low"], poisoned[["open", "close"]].min(axis=1))
    f2 = compile_strategy(RulesSpec.model_validate(rules(entry=entry))).prepare({"AAA": poisoned})
    for key, arr in feats.arrays.items():
        np.testing.assert_array_equal(f2.arrays[key][:11], arr[:11])
    assert (f2.arrays[("AAA", "session", "range_high", 4)][3:26] == 103.5).all()


def test_a_session_with_fewer_bars_than_the_range_never_has_a_range() -> None:
    idx = multi_session_index(date(2023, 11, 24), date(2023, 11, 24))[:3]
    bars = frame_from_closes(idx, [10.0, 11.0, 12.0])
    entry = {"gt": [{"price": "close"}, {"session": "range_high", "bars": 4}]}
    feats = compile_strategy(RulesSpec.model_validate(rules(entry=entry))).prepare({"AAA": bars})
    assert np.isnan(feats.arrays[("AAA", "session", "range_high", 4)]).all()


def test_bar_schedule_decides_on_every_bar() -> None:
    cs = compile_strategy(RulesSpec.model_validate(rules()))
    feats = cs.prepare({"AAA": _two_session_bars()})
    assert feats.decision_flags.all() and len(feats.decision_flags) == 52


# ------------------------------------------------------------------ position cap


def _cap_spec(**position: object) -> RulesSpec:
    return RulesSpec.model_validate(
        rules(
            universe=["AAA", "BBB", "CCC", "DDD"],
            parameters={"slots": 2},
            position={
                "weight": "equal",
                "max_positions": "$slots",
                "rank": {"by": {"price": "close"}, "order": "desc"},
                **position,
            },
        )
    )


def _four_symbol_bars() -> dict[str, pd.DataFrame]:
    idx = multi_session_index(date(2024, 1, 8), date(2024, 1, 8))
    last = {"AAA": 10.0, "BBB": 40.0, "CCC": 30.0, "DDD": 20.0}
    return {s: frame_from_closes(idx, [px] * len(idx)) for s, px in last.items()}


def _eval(held: dict[str, HeldPosition] | None = None):  # noqa: ANN202
    cs = compile_strategy(_cap_spec())
    bars = _four_symbol_bars()
    feats = cs.prepare(bars)
    asof = bars["AAA"].index[5]
    return cs.evaluate(feats, asof, held or {})


def _long(sig) -> dict[str, float]:  # noqa: ANN001
    return {i.symbol: i.weight for i in sig.intents if i.target == "long"}


def test_cap_gives_the_free_slots_to_the_best_ranked_entries() -> None:
    # All four want in; rank = close descending: BBB 40, CCC 30 take the 2 slots, weight 1/2.
    assert _long(_eval()) == {"BBB": 0.5, "CCC": 0.5}


def test_held_positions_keep_their_slot_and_only_the_rest_is_filled() -> None:
    ts = pd.Timestamp("2024-01-08T14:30:00Z")
    held = {"AAA": HeldPosition(symbol="AAA", entry_ts=ts)}  # worst-ranked but already in
    assert _long(_eval(held)) == {"AAA": 0.5, "BBB": 0.5}  # one free slot -> best candidate BBB
    both = {s: HeldPosition(symbol=s, entry_ts=ts) for s in ("AAA", "DDD")}
    assert _long(_eval(both)) == {"AAA": 0.5, "DDD": 0.5}  # full: no new entries


def test_uncapped_equal_weight_is_one_over_the_universe() -> None:
    cs = compile_strategy(RulesSpec.model_validate(rules(universe=["AAA", "BBB"])))
    bars = _four_symbol_bars()
    feats = cs.prepare({k: bars[k] for k in ("AAA", "BBB")})
    sig = cs.evaluate(feats, bars["AAA"].index[3], {})
    assert _long(sig) == {"AAA": 0.5, "BBB": 0.5}


# ------------------------------------------------------------------ the two strategies


DIP_PARAMS = {
    "avg_bars": 20,
    "dip_z": -2.0,
    "rsi_bars": 2,
    "rsi_max": 10,
    "trend_bars": 130,
    "min_bar": 2,
    "stop_atr": 2.0,
    "max_pos": 5,
}
BREAKOUT_PARAMS = {"range_bars": 4, "avg_bars": 20, "stop_atr": 2.0, "max_pos": 5}


def test_pre_registered_parameters_are_pinned() -> None:
    """The numbers were fixed before any data was looked at (ADR-0001 A21); changing one is a
    new strategy id, so this test must fail loudly if a value drifts."""
    dip = load_strategy(STRATEGIES_DIR / "stocks_dt_dip.yaml")
    brk = load_strategy(STRATEGIES_DIR / "stocks_dt_breakout.yaml")
    assert dip.parameters == DIP_PARAMS
    assert brk.parameters == BREAKOUT_PARAMS
    for spec in (dip, brk):
        assert spec.timeframe == "15m" and spec.schedule.freq == "bar"
        assert spec.asset_class == "equity" and spec.id.startswith("stocks_")
        assert len(spec.universe) >= 495  # current S&P 500
        (stop,) = spec.stops
        assert (stop.type, stop.period, stop.mult) == ("atr_trailing", 14, "$stop_atr")
        r = spec.resolve()
        assert r.position.max_positions == 5 and r.position.rank is not None  # type: ignore[union-attr]
        assert r.stops[0].mult == 2.0
    assert (
        dip.universe
        == brk.universe
        == load_strategy(STRATEGIES_DIR / "stocks_momentum_cash.yaml").universe
    )


def _tiny(name: str, params: dict[str, float] | None = None) -> RulesSpec:
    spec = load_strategy(STRATEGIES_DIR / f"{name}.yaml")
    upd: dict = {"universe": ["AAA"]}
    if params:
        upd["parameters"] = {**spec.parameters, **params}
    return spec.model_copy(update=upd)  # type: ignore[return-value]


def _eval_at(spec: RulesSpec, bars: pd.DataFrame, i: int, held: bool = False):  # noqa: ANN202
    cs = compile_strategy(spec)
    feats = cs.prepare({"AAA": bars})
    h = {"AAA": HeldPosition(symbol="AAA", entry_ts=bars.index[i - 1])} if held else {}
    return cs.evaluate(feats, bars.index[i], h), cs


def _dip_bars(drop: float, n: int = 190) -> pd.DataFrame:
    """An up-trending stock (+0.06 a bar), then at bar 150 a one-bar drop of ``drop``."""
    idx = multi_session_index(date(2024, 1, 2), date(2024, 1, 31))[:n]
    closes = [100.0 + 0.06 * i for i in range(n)]
    for i in range(150, n):
        closes[i] = 100.0 + 0.06 * 149 - drop  # stays down after the drop
    return frame_from_closes(idx, closes, spread=0.1)


def test_dip_buys_a_sharp_drop_in_an_uptrend() -> None:
    bars = _dip_bars(drop=2.0)
    # Bar 149 close 108.94 (still rising); bar 150 close 106.94. TR(150) = 2.2, ATR14(150) =
    # (13 x 0.22 + 2.2) / 14 ~ 0.36, SMA20 ~ 108.0 -> (106.94 - 108.0) / 0.36 ~ -2.9 < -2: dip.
    # SMA130 ~ 104.6 < 106.94: the trend filter holds. Bar index in session is >= 2.
    sig, cs = _eval_at(_tiny("stocks_dt_dip"), bars, 150)
    assert [(i.symbol, i.target, i.reason) for i in sig.intents] == [("AAA", "long", "entry")]
    assert sig.intents[0].weight == pytest.approx(0.2)  # 1 / max_pos
    assert sig.intents[0].stop is not None and sig.intents[0].stop.mult == 2.0
    # One bar earlier there is no dip: nothing.
    assert _eval_at(_tiny("stocks_dt_dip"), bars, 149)[0].intents == []
    # A shallow drop is not a dip (-0.3 ATR-ish) ...
    assert _eval_at(_tiny("stocks_dt_dip"), _dip_bars(drop=0.1), 150)[0].intents == []


def test_dip_needs_the_trend_filter_and_skips_the_open() -> None:
    # Same drop but inside a downtrend (close below SMA130): no entry.
    idx = multi_session_index(date(2024, 1, 2), date(2024, 1, 31))[:190]
    down = [200.0 - 0.06 * i for i in range(190)]
    down[150] -= 2.0
    for i in range(151, 190):
        down[i] = down[150]
    sig, _ = _eval_at(_tiny("stocks_dt_dip"), frame_from_closes(idx, down, spread=0.1), 150)
    assert sig.intents == []
    # The first two bars of a session are skipped: bar 156 = session 6 (26 x 6), bar index 0.
    up = _dip_bars(drop=2.0)
    cs = compile_strategy(_tiny("stocks_dt_dip", {"min_bar": 2}))
    feats = cs.prepare({"AAA": up})
    assert feats.arrays[("AAA", "session", "bar_index", None)][156] == 0
    first_bars = [bool(cs._entry(feats, "AAA", i, None)) for i in (156, 157)]  # noqa: SLF001
    assert first_bars == [False, False]


def test_dip_rsi_leg_fires_without_the_atr_leg() -> None:
    spec = _tiny("stocks_dt_dip", {"dip_z": -100.0})  # disable the ATR-dip leg
    # Rising +0.06 a bar, then two down bars of 0.5: Wilder RSI(2) falls below 10.
    idx = multi_session_index(date(2024, 1, 2), date(2024, 1, 31))[:190]
    closes = [100.0 + 0.06 * i for i in range(190)]
    base = closes[149]
    closes[150], closes[151] = base - 0.5, base - 1.0
    for i in range(152, 190):
        closes[i] = base - 1.0
    bars = frame_from_closes(idx, closes, spread=0.1)
    sig, cs = _eval_at(spec, bars, 151)
    feats = cs.prepare({"AAA": bars})
    rsi2 = feats.arrays[("AAA", "ind", "rsi", "close", 2, None, None, None, None, None, None)][151]
    assert rsi2 < 10  # RS = avg gain / avg loss is tiny after two sharp down bars
    assert [i.target for i in sig.intents] == ["long"]
    # One bar earlier there is only the rising trend: RSI is high, no entry.
    assert _eval_at(spec, bars, 149)[0].intents == []


def test_dip_exits_on_reversion_to_the_average_and_holds_below_it() -> None:
    bars = _dip_bars(drop=2.0)
    # Still ~1 below the 20-bar average at bar 151 (flat at 106.94): hold; the held symbol keeps
    # its slot ("hold (bars_held=...)") and is not flat.
    sig, _ = _eval_at(_tiny("stocks_dt_dip"), bars, 151, held=True)
    assert [i.target for i in sig.intents] == ["long"] and sig.intents[0].reason.startswith("hold")
    # Make the price recover above SMA20 at bar 151: exit.
    idx = bars.index
    closes = bars["close"].to_list()
    closes[151] = 109.5
    rec = frame_from_closes(idx, closes, spread=0.1)
    sig2, _ = _eval_at(_tiny("stocks_dt_dip"), rec, 151, held=True)
    assert [(i.target, i.weight) for i in sig2.intents] == [("flat", 0.0)]
    assert sig2.intents[0].reason.startswith("exit condition")


def _breakout_bars(entry_close: float, entry_volume: float, n_prev: int = 1) -> pd.DataFrame:
    """Session 1 flat at 100; session 2 closes 100,101,102,103 (range high 103.5), then
    103.4 at bar 4 and ``entry_close`` at bar 5, volume 1000 except ``entry_volume`` at bar 5."""
    idx = multi_session_index(date(2024, 1, 8), date(2024, 1, 9))
    closes = [100.0] * 26 + [100.0, 101.0, 102.0, 103.0, 103.4, entry_close] + [103.0] * 20
    vol = [1000.0] * 52
    vol[26 + 5] = entry_volume
    return frame_from_closes(idx, closes, spread=0.5, volume=vol)


def test_breakout_enters_above_the_opening_range_on_volume() -> None:
    spec = _tiny("stocks_dt_breakout")
    i = 26 + 5
    # Range = first four bars of Tuesday: highs 100.5, 101.5, 102.5, 103.5 -> 103.5.
    sig, _ = _eval_at(spec, _breakout_bars(104.0, 5000.0), i)
    assert [(x.symbol, x.reason) for x in sig.intents] == [("AAA", "entry")]
    assert sig.intents[0].stop is not None
    # Same price, volume at the average (1000 vs SMA20 of mostly 1000s): not above, no entry.
    assert _eval_at(spec, _breakout_bars(104.0, 1000.0), i)[0].intents == []
    # High volume but the close does not clear the range high (103.5): no entry.
    assert _eval_at(spec, _breakout_bars(103.5, 5000.0), i)[0].intents == []
    # No entry while the range is still forming (bar 3 closes 103 but cannot exceed its own high).
    assert _eval_at(spec, _breakout_bars(104.0, 5000.0), 26 + 3)[0].intents == []


def test_breakout_exits_when_it_fades_back_below_the_range() -> None:
    spec = _tiny("stocks_dt_breakout")
    bars = _breakout_bars(104.0, 5000.0)
    # Bar 6 closes 103.0 < range high 103.5: failed breakout -> flat.
    sig, _ = _eval_at(spec, bars, 26 + 6, held=True)
    assert [(x.target, x.reason.split(" (")[0]) for x in sig.intents] == [
        ("flat", "exit condition")
    ]
    # Still above the range at bar 5: hold.
    sig2, _ = _eval_at(spec, bars, 26 + 5, held=True)
    assert sig2.intents[0].target == "long"


def test_breakout_ranks_by_strength_when_slots_are_scarce() -> None:
    spec = load_strategy(STRATEGIES_DIR / "stocks_dt_breakout.yaml")
    spec = spec.model_copy(
        update={
            "universe": ["AAA", "BBB", "CCC"],
            "parameters": {**spec.parameters, "max_pos": 1},
        }
    )
    base = _breakout_bars(104.0, 5000.0)
    # BBB breaks out harder (close 108) than AAA (104); CCC does not break out.
    strong = _breakout_bars(108.0, 5000.0)
    weak = _breakout_bars(100.0, 1000.0)
    cs = compile_strategy(spec)
    feats = cs.prepare({"AAA": base, "BBB": strong, "CCC": weak})
    sig = cs.evaluate(feats, base.index[26 + 5], {})
    assert [(i.symbol, i.weight) for i in sig.intents if i.target == "long"] == [("BBB", 1.0)]
    assert not math.isnan(
        feats.arrays[("BBB", "ind", "devatr", "close", 20, None, None, None, None, None, None)][31]
    )
