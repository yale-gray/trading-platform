"""Manual live smoke test for the ccxt adapter (Coinbase Exchange, public, no keys).

NOT run by pytest (tests/manual is ignored in CI). Run once by hand:
    uv run python tests/manual/smoke_ccxt.py
"""

from __future__ import annotations

from datetime import UTC, datetime

import ccxt

EXCHANGE_ID = "coinbaseexchange"
DAY_MS = 86_400_000


def main() -> None:
    ex = getattr(ccxt, EXCHANGE_ID)({"enableRateLimit": True})

    since = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
    btc = ex.fetch_ohlcv("BTC/USD", "1d", since, 5)
    aligned = all(c[0] % DAY_MS == 0 for c in btc)
    print("BTC/USD sample:")
    for c in btc:
        print("  ", datetime.fromtimestamp(c[0] / 1000, tz=UTC).isoformat(), c[1:])
    print(f"(a) daily buckets aligned to 00:00 UTC: {aligned}")

    since = int(datetime(2017, 1, 1, tzinfo=UTC).timestamp() * 1000)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    first = None
    while since < now_ms and first is None:  # empty pre-listing windows: page forward
        sol = ex.fetch_ohlcv("SOL/USD", "1d", since, 300)
        if sol:
            first = datetime.fromtimestamp(min(c[0] for c in sol) / 1000, tz=UTC).date()
        since += 300 * DAY_MS
    print(f"(b) SOL/USD earliest candle: {first}")


if __name__ == "__main__":
    main()
