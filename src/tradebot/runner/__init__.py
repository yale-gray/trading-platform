"""tradebot.runner: restart-safe daily-bar paper runner (SPEC-004)."""

from tradebot.runner.bootstrap import (
    ResetFromResult,
    benchmark_definition,
    create_portfolio,
    fund_definition,
    reset_from,
    reset_kill_switch,
    restore_context,
)
from tradebot.runner.state import FAULT_POINTS, PortfolioDef, PortfolioTick, RunnerError
from tradebot.runner.tick import Runner

__all__ = [
    "FAULT_POINTS",
    "PortfolioDef",
    "PortfolioTick",
    "Runner",
    "RunnerError",
    "benchmark_definition",
    "create_portfolio",
    "fund_definition",
    "ResetFromResult",
    "reset_from",
    "reset_kill_switch",
    "restore_context",
]
