"""Configuration for the market data layer."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_cache_dir() -> Path:
    env = os.environ.get("TRADEBOT_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return Path("~/.tradebot/cache").expanduser()


@dataclass(frozen=True)
class DataConfig:
    cache_dir: Path = field(default_factory=_default_cache_dir)  # env TRADEBOT_CACHE_DIR
    crypto_exchange: str = "coinbaseexchange"
    overlap_bars: int = 10
    equity_close_buffer_min: int = 60
    offline: bool = False  # True => never call providers (tests, CI)
