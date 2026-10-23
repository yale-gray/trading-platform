"""Runner data types: portfolio definition (the stored ``config_json``), live context, results.

``PortfolioDef`` is the single source for building a ``PortfolioEngine`` and ``BarDriver``.
Its JSON is stored in ``portfolio.config_json`` and rebuilt identically on every restart:
``EngineConfig`` is dumped with set/dict fields sorted, strategies are dumped through
``dump_strategy`` and reloaded with ``load_strategy``, and ``build_engine_config`` is the
same function ``run_backtest`` uses, so restore never fails on a config mismatch.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from tradebot.backtest.driver import TargetBook, build_engine_config
from tradebot.backtest.engine import SleeveSpec
from tradebot.data.symbols import normalize_symbol
from tradebot.portfolio.config import EngineConfig
from tradebot.portfolio.engine import PortfolioEngine
from tradebot.runner.clock import market_of
from tradebot.sim.orders import Order
from tradebot.strategy.io import dump_strategy, load_strategy

FaultHook = Callable[[str], None]
"""Test seam called with ``after_driver`` / ``before_commit`` / ``after_commit``."""

FAULT_POINTS = ("after_driver", "before_commit", "after_commit")
CONFIG_VERSION = 1

TickStatus = Literal["ok", "noop", "data_unavailable", "error", "catchup_refused", "skipped"]


class RunnerError(RuntimeError):
    """A bar could not be processed (recorded in ``runner_state``; never partially applied)."""


def dump_engine_config(cfg: EngineConfig) -> dict[str, Any]:
    """Byte-stable JSON-able dump (sets and dicts sorted, like ``EngineState``)."""
    data = cfg.model_dump(mode="json")
    for sleeve in data["sleeves"]:
        sleeve["allowed_asset_classes"] = sorted(sleeve["allowed_asset_classes"])
    data["costs"] = dict(sorted(data["costs"].items()))
    return data  # type: ignore[no-any-return]


@dataclass(frozen=True)
class PortfolioDef:
    """Everything needed to (re)build a portfolio's engine and driver deterministically."""

    name: str
    kind: str  # fund | benchmark
    engine: EngineConfig  # base config (stop multiples are filled by build_engine_config)
    sleeves: tuple[SleeveSpec, ...]
    start: date  # inception bar: first bar the portfolio processes
    data_start: date  # first bar loaded; FIXED per portfolio (Wilder ATR depends on it)
    benchmark: str | None = None  # name of the benchmark portfolio shown on the fund's curve

    def engine_config(self) -> EngineConfig:
        """The config handed to ``PortfolioEngine`` (same builder as ``run_backtest``)."""
        return build_engine_config(self.engine, list(self.sleeves))

    def required_symbols(self) -> list[str]:
        """Canonical symbols of every strategy sleeve (universe plus fallbacks), sorted."""
        syms: set[str] = set()
        for s in self.sleeves:
            if s.strategy is not None:
                syms.update(str(normalize_symbol(x)) for x in s.strategy.required_symbols())
        return sorted(syms)

    def markets(self) -> list[str]:
        """Markets (``equity`` / ``crypto``) of the required symbols, sorted."""
        return sorted({market_of(s) for s in self.required_symbols()})

    def timeframe(self) -> str:
        """``"15m"`` if any sleeve strategy runs on 15-minute bars (ADR-0001 A21), else ``"1d"``."""
        return (
            "15m"
            if any(s.strategy and s.strategy.timeframe == "15m" for s in self.sleeves)
            else "1d"
        )

    def strategy_sleeve_ids(self) -> list[str]:
        """Sleeve ids that carry a strategy (the TargetBook ids), sorted."""
        return sorted(s.sleeve_id for s in self.sleeves if s.strategy is not None)

    def to_json(self) -> str:
        """Canonical JSON stored in ``portfolio.config_json``."""
        payload = {
            "version": CONFIG_VERSION,
            "name": self.name,
            "kind": self.kind,
            "engine": dump_engine_config(self.engine),
            "sleeves": [
                {
                    "sleeve_id": s.sleeve_id,
                    "strategy": dump_strategy(s.strategy, "json") if s.strategy else None,
                }
                for s in self.sleeves
            ],
            "start": self.start.isoformat(),
            "data_start": self.data_start.isoformat(),
            "benchmark": self.benchmark,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> PortfolioDef:
        """Inverse of :meth:`to_json`."""
        d = json.loads(text)
        if d.get("version") != CONFIG_VERSION:
            raise RunnerError(f"unsupported portfolio config version {d.get('version')!r}")
        return cls(
            name=d["name"],
            kind=d["kind"],
            engine=EngineConfig.model_validate(d["engine"]),
            sleeves=tuple(
                SleeveSpec(
                    sleeve_id=s["sleeve_id"],
                    strategy=load_strategy(s["strategy"]) if s["strategy"] else None,
                )
                for s in d["sleeves"]
            ),
            start=date.fromisoformat(d["start"]),
            data_start=date.fromisoformat(d["data_start"]),
            benchmark=d.get("benchmark"),
        )


@dataclass
class PortfolioContext:
    """In-memory engine state for one portfolio, always equal to the last committed bar."""

    portfolio_id: int
    defn: PortfolioDef
    engine: PortfolioEngine
    book: TargetBook
    sleeve_ids: dict[str, int]
    n_ledger: int = 0  # ledger events already persisted
    last_bar: datetime | None = None  # bar the context is at (== runner_state marker)
    orders: dict[str, Order] = field(default_factory=dict)  # last persisted order states


@dataclass
class PortfolioTick:
    """What one ``Runner.tick`` did for one portfolio."""

    portfolio: str
    status: TickStatus
    processed: list[date | datetime] = field(default_factory=list)  # bar dates / 15m bar opens
    error: str | None = None
