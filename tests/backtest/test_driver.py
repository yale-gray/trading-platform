"""BarDriver and TargetBook (SPEC-002 section 6; AC 14 and 15).

Most driver tests use a stub engine that records calls, so the gating rules are checked
without fills. The stub returns real ``BarResult``/``TargetResult`` models with no orders.
The monthly sleeve is ``core_momentum`` (decision bars = first trading day of each month).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from support.backtest_fixtures import RUN_END, RUN_START, load_fixture_bars

from tradebot.backtest.driver import BarDriver, TargetBook, build_engine_config
from tradebot.backtest.engine import SleeveSpec
from tradebot.portfolio.config import default_config
from tradebot.portfolio.engine import BarResult, TargetResult
from tradebot.portfolio.models import PortfolioSnapshot, SleeveSnapshot
from tradebot.strategy import indicators
from tradebot.strategy.io import STRATEGIES_DIR, load_strategy
from tradebot.strategy.schema import STRATEGIES
from tradebot.strategy.signals import Intent, SignalSet

MONTHLY = "core_momentum"
CASH = "cash"


def _snapshot(asof: date) -> PortfolioSnapshot:
    eq = Decimal("100000.00")
    return PortfolioSnapshot(
        asof=asof,
        equity=eq,
        cash=eq,
        drawdown=Decimal("0"),
        peak_equity=eq,
        killed=False,
        last_seq=0,
        sleeves=[
            SleeveSnapshot(
                sleeve_id=MONTHLY, cash=eq, equity=eq, weight=Decimal("0.5"), positions=[]
            ),
            SleeveSnapshot(
                sleeve_id=CASH,
                cash=Decimal("0"),
                equity=Decimal("0"),
                weight=Decimal("0.5"),
                positions=[],
            ),
        ],
        exposures={},
        crypto_fraction=Decimal("0"),
    )


class StubEngine:
    """Records every call. No fills, no positions, no costs."""

    def __init__(self, sleeve_ids: list[str], rebalance_on: set[date] | None = None) -> None:
        self.sleeve_ids = list(sleeve_ids)
        self.last_processed_asof: date | None = None
        self._rebalance_on = rebalance_on or set()
        self.bar_calls: list[tuple[date, dict]] = []
        self.submits: list[tuple[str, date, dict[str, float]]] = []
        self._last: PortfolioSnapshot | None = None

    def process_bar(self, asof: date, bars: dict) -> BarResult:
        self.bar_calls.append((asof, bars))
        self.last_processed_asof = asof
        snap = _snapshot(asof)
        self._last = snap
        return BarResult(
            asof=asof,
            fills=[],
            stop_orders=[],
            expired=[],
            new_ledger_events=[],
            snapshot=snap,
            kill_switch_tripped=False,
            rebalance_planned=asof in self._rebalance_on,
        )

    def snapshot(self) -> PortfolioSnapshot:
        assert self._last is not None
        return self._last

    def submit_target_weights(self, sleeve_id: str, weights: dict, asof: date) -> TargetResult:
        self.submits.append((sleeve_id, asof, dict(weights)))
        return TargetResult(orders=[], rejections=[])


def _sleeves() -> list[SleeveSpec]:
    return [
        SleeveSpec(sleeve_id=MONTHLY, strategy=STRATEGIES[MONTHLY]),
        SleeveSpec(sleeve_id=CASH, strategy=None),
    ]


def _calendar(driver: BarDriver) -> list[pd.Timestamp]:
    return list(driver.master_calendar(end=RUN_END))


def _month_starts(cal: list[pd.Timestamp]) -> set[pd.Timestamp]:
    """First bar of each calendar month within the run window."""
    out = set()
    for k, ts in enumerate(cal):
        if k == 0 or (ts.year, ts.month) != (cal[k - 1].year, cal[k - 1].month):
            out.add(ts)
    return out


# ---------------------------------------------------------------- TargetBook (pure)


def _signals(
    sleeve: str, asof: str, intents: list[tuple[str, str, float]], decision: bool = True
) -> SignalSet:
    return SignalSet(
        sleeve_id=sleeve,
        asof=pd.Timestamp(asof),
        decision=decision,
        intents=[Intent(symbol=s, target=t, weight=w, reason="test") for s, t, w in intents],  # type: ignore[arg-type]
    )


def test_absent_symbols_have_target_zero_and_flat_intents_are_not_in_book() -> None:
    # Decision: A long 0.5, B long 0.3, C flat (weight given but target flat).
    # Book holds only the long ones; C is absent, so its target is 0.
    book = TargetBook(["s1"])
    book.update(
        _signals("s1", "2024-01-31", [("A", "long", 0.5), ("B", "long", 0.3), ("C", "flat", 0.2)])
    )
    assert book.weights("s1") == {"A": 0.5, "B": 0.3}
    assert "C" not in book.weights("s1")
    assert book.weights("s1").get("C", 0.0) == 0.0


def test_update_replaces_the_whole_book_and_never_merges() -> None:
    # Decision 1 holds A and B. Decision 2 selects only A at 0.6, so B drops out of the book
    # (the engine will then treat B as 0 and sell it).
    book = TargetBook(["s1"])
    book.update(_signals("s1", "2024-01-31", [("A", "long", 0.5), ("B", "long", 0.3)]))
    book.update(_signals("s1", "2024-02-29", [("A", "long", 0.6)]))
    assert book.weights("s1") == {"A": 0.6}
    assert book.last_decision_ts("s1") == pd.Timestamp("2024-02-29")


def test_non_decision_signal_set_is_ignored() -> None:
    book = TargetBook(["s1"])
    book.update(_signals("s1", "2024-01-31", [("A", "long", 0.5)]))
    book.update(_signals("s1", "2024-02-01", [("Z", "long", 0.9)], decision=False))
    assert book.weights("s1") == {"A": 0.5}
    assert book.last_decision_ts("s1") == pd.Timestamp("2024-01-31")


def test_zero_weight_long_intent_is_not_a_target() -> None:
    # "weights() returns only positive weights": a long intent at weight 0 is absent (target 0).
    book = TargetBook(["s1"])
    book.update(_signals("s1", "2024-01-31", [("A", "long", 0.0), ("B", "long", 0.4)]))
    assert book.weights("s1") == {"B": 0.4}


def test_fresh_book_has_no_decision_and_empty_weights() -> None:
    book = TargetBook(["s1"])
    assert not book.has_decision("s1")
    assert book.weights("s1") == {}
    assert book.state() == {"sleeves": {"s1": {"weights": {}, "decision_ts": None}}}


def test_state_round_trips_through_json_exactly() -> None:
    book = TargetBook(["s1", "s2"])
    book.update(_signals("s1", "2024-01-31", [("SPY", "long", 0.25), ("QQQ", "long", 0.125)]))
    restored = TargetBook.from_state(["s1", "s2"], json.loads(json.dumps(book.state())))
    assert restored.weights("s1") == book.weights("s1") == {"QQQ": 0.125, "SPY": 0.25}
    assert restored.last_decision_ts("s1") == book.last_decision_ts("s1")
    assert restored.has_decision("s1") and not restored.has_decision("s2")
    assert restored.state() == book.state()


def test_unknown_sleeve_in_update_or_state_is_rejected() -> None:
    book = TargetBook(["s1"])
    with pytest.raises(KeyError):
        book.update(_signals("nope", "2024-01-31", [("A", "long", 0.5)]))
    with pytest.raises(ValueError):
        TargetBook.from_state(["s1"], {"sleeves": {"other": {"weights": {}, "decision_ts": None}}})


# ---------------------------------------------------------------- BarDriver (stub engine)


def test_driver_rejects_sleeve_ids_that_differ_from_the_engine() -> None:
    with pytest.raises(ValueError):
        BarDriver(StubEngine(["something_else"]), _sleeves(), {}, start=RUN_START)


def test_step_rejects_naive_timestamp() -> None:
    bars = _load_for_stub()
    driver = BarDriver(StubEngine([MONTHLY, CASH]), _sleeves(), bars, start=RUN_START)
    with pytest.raises(ValueError):
        driver.step(pd.Timestamp("2024-01-02"))


def _load_for_stub() -> dict[str, pd.DataFrame]:
    return load_fixture_bars({str(s) for s in STRATEGIES[MONTHLY].required_symbols()} | {"SPY"})


@pytest.fixture(scope="module")
def monthly_bars() -> dict[str, pd.DataFrame]:
    return _load_for_stub()


def test_asof_is_the_timestamp_date_and_bars_carry_causal_vol20_atr14(monthly_bars) -> None:
    # AC 14 (a) and (b). The expected vol20/atr14 are the spec's causal indicator functions
    # applied to the same bars (SPEC-002 section 6, step 1). Early bars are None (not NaN).
    engine = StubEngine([MONTHLY, CASH])
    driver = BarDriver(engine, _sleeves(), monthly_bars, start=RUN_START)
    cal = _calendar(driver)
    for ts in cal[:3]:
        driver.step(ts)
    asof_seen = [a for a, _ in engine.bar_calls]
    assert asof_seen == [ts.date() for ts in cal[:3]]
    assert all(isinstance(a, date) and not isinstance(a, pd.Timestamp) for a in asof_seen)

    spy = monthly_bars["SPY"]
    later = cal[25]
    driver.step(later)
    bars_at = engine.bar_calls[-1][1]
    spy_bar = bars_at["SPY"]
    vol = indicators.vol(spy["close"], 20).loc[later]
    atr = indicators.atr(spy["high"], spy["low"], spy["close"], 14).loc[later]
    assert spy_bar.vol20 == pytest.approx(float(vol), rel=1e-12)
    assert spy_bar.atr14 == pytest.approx(float(atr), rel=1e-12)


def test_submissions_happen_only_on_decision_and_rebalance_bars(monthly_bars) -> None:
    # Monthly sleeve: decisions are the first bar of each month (run window), since the
    # warmup is already covered before RUN_START. Plus one injected rebalance_planned bar in
    # the middle of a month. The cash sleeve must never be submitted.
    probe = BarDriver(StubEngine([MONTHLY, CASH]), _sleeves(), monthly_bars, start=RUN_START)
    cal = _calendar(probe)
    month_starts = _month_starts(cal)
    rebalance_day = next(ts for ts in cal[60:] if ts not in month_starts)

    engine = StubEngine([MONTHLY, CASH], rebalance_on={rebalance_day.date()})
    driver = BarDriver(engine, _sleeves(), monthly_bars, start=RUN_START)
    results = [driver.step(ts) for ts in cal]

    submitted_dates = {asof for sid, asof, _ in engine.submits if sid == MONTHLY}
    assert submitted_dates == {ts.date() for ts in month_starts} | {rebalance_day.date()}
    assert all(sid != CASH for sid, _, _ in engine.submits)
    # Decision bars reported by the driver are exactly the month starts (signals are only
    # produced on decision bars).
    decided = {r.ts for r in results if MONTHLY in r.signals}
    assert decided == month_starts
    # On the injected rebalance bar the submitted weights are the book as of the latest decision
    # (the long intents of that decision bar, weight > 0). Not the book at the end of the run.
    last_decision = [r for r in results if r.ts <= rebalance_day and MONTHLY in r.signals][-1]
    expected = {
        i.symbol: float(i.weight)
        for i in last_decision.signals[MONTHLY].intents
        if i.target == "long" and i.weight > 0
    }
    assert expected, "the monthly sleeve should hold at least one long target"
    rb_weights = next(w for sid, asof, w in engine.submits if asof == rebalance_day.date())
    assert rb_weights == expected


def test_restored_driver_behaves_like_an_uninterrupted_one(monthly_bars) -> None:
    # Restart after bar k with the book state persisted as JSON. The next rebalance bar falls
    # after the restart and must submit the same weights as the uninterrupted run.
    probe = BarDriver(StubEngine([MONTHLY, CASH]), _sleeves(), monthly_bars, start=RUN_START)
    cal = _calendar(probe)
    month_starts = _month_starts(cal)
    k = next(i for i in range(40, len(cal)) if cal[i] not in month_starts)  # mid-month restart
    rebalance_day = next(ts for ts in cal[k + 1 :] if ts not in month_starts)

    rb = {rebalance_day.date()}
    full_engine = StubEngine([MONTHLY, CASH], rebalance_on=rb)
    full = BarDriver(full_engine, _sleeves(), monthly_bars, start=RUN_START)
    for ts in cal:
        full.step(ts)

    first_engine = StubEngine([MONTHLY, CASH], rebalance_on=rb)
    first = BarDriver(first_engine, _sleeves(), monthly_bars, start=RUN_START)
    for ts in cal[:k]:
        first.step(ts)
    state = json.loads(json.dumps(first.book.state()))

    second_engine = StubEngine([MONTHLY, CASH], rebalance_on=rb)
    restored_book = TargetBook.from_state([MONTHLY], state)
    second = BarDriver(second_engine, _sleeves(), monthly_bars, start=RUN_START, book=restored_book)
    for ts in cal[k:]:
        second.step(ts)

    assert second.book.state() == full.book.state()
    after = {(sid, asof): w for sid, asof, w in full_engine.submits if asof >= cal[k].date()}
    restored = {(sid, asof): w for sid, asof, w in second_engine.submits}
    assert restored == after
    assert (MONTHLY, rebalance_day.date()) in restored


def test_first_decision_is_exactly_at_warmup_bars_minus_one() -> None:
    # Start the run at the first fetched bar, before the strategy has its warmup history.
    # No decision before index warmup_bars - 1 of the sleeve calendar; the first (forced)
    # decision is exactly at that index (SPEC-002 section 6).
    bars = _load_for_stub()
    driver = BarDriver(StubEngine([MONTHLY, CASH]), _sleeves(), bars, start=date(2018, 1, 2))
    slot = next(s for s in driver._slots if s.sleeve_id == MONTHLY)
    warmup = slot.compiled.warmup_bars
    assert warmup > 5
    decided = [
        int(slot.feats.calendar.get_indexer([ts])[0])
        for ts in driver.master_calendar(end=RUN_END)[: warmup + 10]
        if MONTHLY in driver.step(ts).signals
    ]
    assert decided and decided[0] == warmup - 1


# ---------------------------------------------------------------- build_engine_config


def test_build_engine_config_copies_the_stop_multiple_into_the_sleeve() -> None:
    # mean_reversion has atr_trailing with mult 2.5 (strategies/mean_reversion.yaml).
    # core_momentum has no stop, so its trailing multiple stays None.
    sleeves = [
        SleeveSpec(sleeve_id=s, strategy=STRATEGIES[s])
        for s in ("core_momentum", "mean_reversion", "crypto_trend")
    ]
    sleeves.append(SleeveSpec(sleeve_id="cash", strategy=None))
    cfg = build_engine_config(default_config(), sleeves)
    by_id = {s.sleeve_id: s for s in cfg.sleeves}
    assert by_id["mean_reversion"].trailing_stop_atr_mult == Decimal("2.5")
    assert by_id["crypto_trend"].trailing_stop_atr_mult == Decimal("2.5")
    assert by_id["core_momentum"].trailing_stop_atr_mult is None


def test_atr_period_other_than_14_is_rejected_when_the_spec_loads(tmp_path) -> None:
    # The mean_reversion YAML with atr_len 14 changed to 20: the engine supports only 14 (ADR D5).
    # The schema itself rejects this at load time, before build_engine_config runs.
    text = (STRATEGIES_DIR / "mean_reversion.yaml").read_text()
    assert "atr_len: 14" in text
    path = tmp_path / "mean_reversion_atr20.yaml"
    path.write_text(text.replace("atr_len: 14", "atr_len: 20"))
    with pytest.raises(ValueError, match="period must be 14"):
        load_strategy(path)
