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
    # 15-minute intraday bars (ADR-0001 A21)
    intraday_history_feed: str = "sip"  # cached history: full consolidated tape
    intraday_live_feed: str | None = "iex"  # in-memory live tail on read; None disables
    sip_delay_min: int = 16  # free SIP plan refuses data newer than 15 min
    intraday_overlap_bars: int = 26  # one regular session re-fetched for the restatement check
    intraday_chunk_symbols: int = 10  # symbols per multi-symbol request group
