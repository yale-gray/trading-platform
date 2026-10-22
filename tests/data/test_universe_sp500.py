"""The committed S&P 500 snapshot loads and normalises (offline, no network)."""

from datetime import date

import pytest

from tradebot.data.symbols import normalize_symbol
from tradebot.data.universe import SP500_UNIVERSE, DatedUniverse, load_sp500

pytestmark = pytest.mark.unit


def test_sp500_snapshot_loads():
    assert 480 <= len(SP500_UNIVERSE) <= 520
    assert len(set(SP500_UNIVERSE)) == len(SP500_UNIVERSE)
    assert "BRK.B" in SP500_UNIVERSE and "AAPL" in SP500_UNIVERSE
    assert all(normalize_symbol(s) == s for s in SP500_UNIVERSE)


def test_dated_universe_membership(tmp_path):
    p = tmp_path / "u.csv"
    p.write_text("ticker,name,sector,date_added\nAAA,a,x,2010-01-01\nBBB,b,x,\nCCC,c,x,garbage\n")
    u = load_sp500(p)
    assert u.members(date(2009, 1, 1)) == ["BBB", "CCC"]
    assert u.members(date(2011, 1, 1)) == ["AAA", "BBB", "CCC"]
    assert isinstance(u, DatedUniverse)
