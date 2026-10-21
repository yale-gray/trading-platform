"""No-lookahead tests at signal and backtest level (SPEC-002 AC 3 and 6; T9).

Randomised truncation invariance of ``evaluate`` on random panels, the backtest-level rule that
no fill precedes its signal bar, and the price-shift hook: shifting all prices forward one bar
kills a deliberately-lookahead dummy rule's edge and merely delays the real (causal) rule.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradebot.strategy.engine import compile_strategy
from tradebot.strategy.schema import RulesSpec

pytestmark = pytest.mark.property

SPEC = RulesSpec.model_validate(
    {
        "id": "rsi_trend",
        "name": "RSI pullback",
        "asset_class": "equity",
        "universe": ["SPY", "QQQ"],
        "schedule": {"freq": "daily"},
        "parameters": {"rsi_len": 2, "trend": 20},
        "entry": {
            "all": [
                {"lt": [{"ind": "rsi", "period": "$rsi_len"}, 30]},
                {"gt": [{"price": "close"}, {"ind": "sma", "period": "$trend"}]},
            ]
        },
        "exit": {"gt": [{"ind": "rsi", "period": "$rsi_len"}, 70]},
        "position": {"weight": "equal"},
    }
)


def _panel(seed: int, n: int) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n, tz="UTC")
    out = {}
    for sym in ("SPY", "QQQ"):
        close = 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, n))
        open_ = close * (1 + rng.normal(0, 0.002, n))
        hi = np.maximum(open_, close) * 1.003
        lo = np.minimum(open_, close) * 0.997
        out[sym] = pd.DataFrame(
            {"open": open_, "high": hi, "low": lo, "close": close, "volume": 1e6}, index=idx
        )
    return out


def _intents(bars: dict[str, pd.DataFrame], t: int, held: dict) -> list[tuple]:
    comp = compile_strategy(SPEC, "s")
    feats = comp.prepare(bars)
    ts = bars["SPY"].index[t]
    sig = comp.evaluate(feats, ts, held, force_decision=False)
    return sorted((i.symbol, i.target, i.weight) for i in sig.intents)


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 10_000), frac=st.floats(0.2, 1.0), held=st.booleans())
def test_evaluate_at_t_ignores_everything_after_t(seed: int, frac: float, held: bool) -> None:
    n = 120
    bars = _panel(seed, n)
    t = max(int(frac * (n - 1)), 25)
    from tradebot.strategy.signals import HeldPosition

    h = {"SPY": HeldPosition(symbol="SPY", entry_ts=bars["SPY"].index[t - 3])} if held else {}
    full = _intents(bars, t, h)
    prefix = _intents({k: v.iloc[: t + 1] for k, v in bars.items()}, t, h)
    assert prefix == full
    poisoned = {k: v.copy() for k, v in bars.items()}
    for v in poisoned.values():
        v.iloc[t + 1 :] = 1e6
    assert _intents(poisoned, t, h) == full


def test_shifting_prices_forward_delays_the_real_rule_and_kills_the_lookahead_dummy() -> None:
    bars = _panel(3, 250)
    spy = bars["SPY"]
    ret = spy["close"].pct_change().fillna(0.0)
    # dummy: hold at t if the NEXT bar's return is positive (pure lookahead)
    dummy_pos = (ret.shift(-1) > 0).astype(float)
    edge = float((dummy_pos * ret.shift(-1).fillna(0.0)).sum())
    shifted = spy.shift(1).iloc[1:]  # all prices pushed forward one bar
    sret = shifted["close"].pct_change().fillna(0.0)
    sdummy = (sret.shift(-1) > 0).astype(float)
    # evaluated against the *true* returns the dummy no longer knows the future
    true_next = ret.reindex(shifted.index).shift(-1).fillna(0.0)
    shifted_edge = float((sdummy * true_next).sum())
    assert edge > 0.5 and shifted_edge < 0.25 * edge

    # the real rule at t+1 on shifted data equals the original at t (causal: it only delays)
    for t in (40, 80, 150, 200):
        orig = _intents(bars, t, {})
        shift_bars = {k: v.shift(1).iloc[1:] for k, v in bars.items()}
        # shifted row i+1 holds original row i, so position t in `shift_bars` is original t
        idx = shift_bars["SPY"].index
        comp = compile_strategy(SPEC, "s")
        feats = comp.prepare({k: v.iloc[: t + 1] for k, v in shift_bars.items()})
        got = sorted(
            (i.symbol, i.target, i.weight)
            for i in comp.evaluate(feats, idx[t], {}, force_decision=False).intents
        )
        sub = _intents({k: v.iloc[: t + 2] for k, v in bars.items()}, t, {})
        assert got == sub == orig


@pytest.mark.slow
@pytest.mark.timeout(120)
@pytest.mark.parametrize("gid", ["G1", "G3"])
def test_backtest_fills_come_after_signals_and_first_trade_after_warmup(gid: str) -> None:
    from golden import harness as h

    run = h.run_golden(gid)
    assert run.fills
    for f in run.fills:
        assert f["signal_ts"] < f["fill_ts"]
    spec = run.cfg.sleeves[0].strategy
    assert spec is not None
    warm = compile_strategy(spec, "s").warmup_bars
    bars = h.load_bars()
    cal = bars[spec.universe[0]].index
    first_fill = pd.Timestamp(min(f["fill_ts"] for f in run.fills), tz="UTC")
    # warm-up bars precede the start, so the first possible fill is the bar after the start bar
    assert int((cal < pd.Timestamp(h.START, tz="UTC")).sum()) >= warm
    assert first_fill > pd.Timestamp(h.START, tz="UTC")
