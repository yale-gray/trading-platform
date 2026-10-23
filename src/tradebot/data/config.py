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
    # In-memory IEX live tail on read. OFF (None): live decisions use SIP only (ADR-0001 A22,
    # audit 007 M1: IEX volume is not comparable to SIP baselines). Set "iex" to re-enable.
    intraday_live_feed: str | None = None
    sip_delay_min: int = 16  # free SIP plan refuses data newer than 15 min
    intraday_overlap_bars: int = 26  # one regular session re-fetched for the restatement check
    intraday_chunk_symbols: int = 10  # symbols per multi-symbol request group (bulk history)
    # Short tail refreshes (a few sessions) are tiny, so they go in groups of up to 100 symbols
    # per request (the provider's cap): 500 symbols = 5 requests, not 500 (audit 007 M3).
    intraday_tail_chunk_symbols: int = 100
    intraday_tail_max_days: int = 7  # a plan spanning at most this many days is a "tail"
