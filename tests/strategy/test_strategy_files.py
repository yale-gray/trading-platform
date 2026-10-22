"""Every shipped strategy file: tickers are strings (YAML 1.1 turns NO/ON/Y into bools)."""

from __future__ import annotations

import pytest
import yaml

from tradebot.data.universe import SP500_LABEL, is_sp500_spec
from tradebot.strategy.io import STRATEGIES_DIR, load_strategy

FILES = sorted(STRATEGIES_DIR.glob("*.yaml"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_universe_ticker_is_a_string(path) -> None:  # noqa: ANN001
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert all(isinstance(t, str) for t in raw["universe"]), path.name
    spec = load_strategy(path)
    assert all(isinstance(t, str) for t in spec.universe), path.name
    assert [str(t) for t in spec.universe] == [str(t) for t in raw["universe"]]


def test_sp500_detection_is_by_spec_id_prefix() -> None:
    ids = {load_strategy(p).id for p in FILES}
    assert {i for i in ids if is_sp500_spec(i)} == {"stocks_momentum", "stocks_momentum_cash"}
    assert "survivorship" in SP500_LABEL
