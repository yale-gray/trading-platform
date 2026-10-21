"""Shared fixtures for backtest tests (helpers live in ``support.backtest_fixtures``)."""

from __future__ import annotations

import pandas as pd
import pytest
from support.backtest_fixtures import load_fixture_bars


@pytest.fixture(scope="module")
def fixture_bars() -> dict[str, pd.DataFrame]:
    """Every symbol the three built-in strategies need, plus the SPY and BTC/USD benchmarks."""
    from tradebot.strategy.schema import STRATEGIES

    syms: set[str] = {"SPY", "BTC/USD"}
    for spec in STRATEGIES.values():
        syms.update(str(s) for s in spec.required_symbols())
    return load_fixture_bars(syms)
