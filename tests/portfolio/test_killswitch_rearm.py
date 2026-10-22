"""Engine-level kill switch pause and re-arm (ADR-0001 A18), persistence and back-compat."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from scenario_bars import bar, book

from tradebot.portfolio.config import default_config
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.portfolio.models import EventType
from tradebot.portfolio.state import EngineState, StateCorruptionError
from tradebot.risk.config import RiskConfig
from tradebot.sim.orders import OrderReason

CORE, SPY = "core_momentum", "SPY"
D0 = date(2024, 1, 2)


def _cfg(pause: int):  # noqa: ANN202
    return default_config().model_copy(update={"risk": RiskConfig(kill_switch_pause_bars=pause)})


def _tripped_engine(pause: int = 3) -> tuple[PortfolioEngine, date]:
    """Trip on D0+2 (SPY 100 -> 20), so equity is 79987.50."""
    eng = PortfolioEngine(_cfg(pause))
    eng.process_bar(D0, book(bar(SPY, 100.0, 100.0)))
    eng.submit_target_weights(CORE, {SPY: 0.5}, D0)
    eng.process_bar(D0 + timedelta(days=1), book(bar(SPY, 100.0, 100.0)))
    r = eng.process_bar(D0 + timedelta(days=2), book(bar(SPY, 20.0, 20.0)))
    assert r.kill_switch_tripped
    return eng, D0 + timedelta(days=2)


def test_pause_then_rearm_books_event_and_resets_peak() -> None:
    eng, d = _tripped_engine(3)
    for i in (1, 2):
        r = eng.process_bar(d + timedelta(days=i), book(bar(SPY, 20.0, 20.0)))
        assert r.snapshot.killed and not r.kill_switch_tripped
        blocked = eng.submit_target_weights(CORE, {SPY: 0.5}, d + timedelta(days=i))
        assert [x.reason for x in blocked.rejections] == [OrderReason.KILL_SWITCH]
    r = eng.process_bar(d + timedelta(days=3), book(bar(SPY, 20.0, 20.0)))
    assert not r.snapshot.killed and not r.kill_switch_tripped
    rearm = [e for e in r.new_ledger_events if e.type is EventType.KILL_SWITCH_REARMED]
    assert len(rearm) == 1
    assert r.snapshot.peak_equity == r.snapshot.equity
    assert Decimal(rearm[0].metadata["peak_equity"]) == r.snapshot.equity
    assert eng.submit_target_weights(CORE, {SPY: 0.1}, d + timedelta(days=3)).rejections == []
    eng.verify()


def test_manual_reset_ends_pause_early_and_no_rearm_event_follows() -> None:
    eng, d = _tripped_engine(3)
    eng.reset_kill_switch("op", "early", d)
    assert not eng.snapshot().killed
    types = []
    for i in (1, 2, 3, 4):
        r = eng.process_bar(d + timedelta(days=i), book(bar(SPY, 20.0, 20.0)))
        types += [e.type for e in r.new_ledger_events]
    assert EventType.KILL_SWITCH_REARMED not in types


def test_state_round_trips_mid_pause_and_after_rearm() -> None:
    eng, d = _tripped_engine(3)
    eng.process_bar(d + timedelta(days=1), book(bar(SPY, 20.0, 20.0)))
    st = eng.export_state()
    assert st.kill_state["bars_paused"] == 1 and st.kill_state["pause_bars"] == 3
    twin = PortfolioEngine(_cfg(3), EngineState.from_json(st.to_json()))
    for e in (eng, twin):
        e.process_bar(d + timedelta(days=2), book(bar(SPY, 20.0, 20.0)))
        e.process_bar(d + timedelta(days=3), book(bar(SPY, 20.0, 20.0)))
    assert not twin.snapshot().killed
    assert twin.export_state().to_json() == eng.export_state().to_json()
    PortfolioEngine(_cfg(3), EngineState.from_json(twin.export_state().to_json()))  # restores


def test_state_saved_before_a18_restores() -> None:
    eng, d = _tripped_engine(20)
    data = json.loads(eng.export_state().to_json())
    del data["kill_state"]["pause_bars"], data["kill_state"]["bars_paused"]
    del data["config"]["risk"]["kill_switch_pause_bars"]
    old = EngineState.from_json(json.dumps(data))
    assert old.config.risk.kill_switch_pause_bars == 20
    restored = PortfolioEngine(old.config, old)
    assert restored.snapshot().killed
    r = restored.process_bar(d + timedelta(days=1), book(bar(SPY, 20.0, 20.0)))
    assert r.snapshot.killed


def test_restore_cross_checks_pause_counter() -> None:
    eng, d = _tripped_engine(3)
    data = json.loads(eng.export_state().to_json())
    data["kill_state"]["bars_paused"] = 3  # a killed switch must have bars_paused < pause_bars
    with pytest.raises(StateCorruptionError, match="bars_paused"):
        PortfolioEngine(_cfg(3), EngineState.from_json(json.dumps(data)))
    data["kill_state"]["bars_paused"] = 1
    data["kill_state"]["killed"] = False  # contradicts the last ledger event (TRIPPED)
    with pytest.raises(StateCorruptionError):
        PortfolioEngine(_cfg(3), EngineState.from_json(json.dumps(data)))


def test_restore_mid_pause_allows_equity_above_the_old_peak() -> None:
    """Equity can rise past the pre-trip peak during a pause; the restore must accept it (F4)."""
    eng, d = _tripped_engine(5)
    eng.process_bar(d + timedelta(days=1), book(bar(SPY, 110.0, 110.0)))
    st = eng.export_state()
    ks = st.kill_state
    assert ks["killed"] and Decimal(str(ks["last_equity"])) > Decimal(str(ks["peak_equity"]))
    twin = PortfolioEngine(_cfg(5), EngineState.from_json(st.to_json()))
    assert twin.snapshot().killed
