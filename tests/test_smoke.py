import importlib

import tradebot

SUBPACKAGES = ["data", "strategy", "backtest", "sim", "risk", "portfolio", "runner", "api"]


def test_package_imports():
    assert tradebot.__version__ == "0.1.0"
    for name in SUBPACKAGES:
        importlib.import_module(f"tradebot.{name}")
