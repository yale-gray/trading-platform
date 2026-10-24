"""Daily prices for every point-in-time S&P 500 member (ADR-0001 A32).

The strategy universe for a survivorship-free test is every ticker that was ever an index member,
including names later removed or delisted. This module is a data-layer *fetch helper* (not part of
``MarketData`` routing):

1. names with a yfinance cache (or that yfinance still serves) come through the normal
   ``MarketData`` path, unchanged;
2. names yfinance cannot serve (delisted, renamed) are fetched as daily bars from Alpaca market
   data (SIP history, ``adjustment=all``) and written into the **same cache** under the
   ``yfinance`` source key, so the rest of the system reads them like any other symbol. The
   provider that supplied each row stays in the cache's ``source_rev`` audit column
   (``alpaca_sip`` vs ``yfinance``), which the coverage report uses.

Adjustment basis: yfinance bars are ``Adj Close / Close``-scaled (splits + dividends, total
return); Alpaca ``adjustment=all`` is splits + dividends too. Both are scaled to the latest price
at fetch time, but their dividend arithmetic differs slightly, so the two sources are never
spliced inside one symbol: each symbol is wholly one source. ``compare_sources`` measures the gap
on names both serve.

Run: ``uv run python -m tradebot.data.pit_prices --start 2015-06-01 --end 2023-12-31``.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from tradebot.clock import Clock, SystemClock
from tradebot.data.cache import REAL_BASIS_PREFIX, BarCache
from tradebot.data.calendar import sessions
from tradebot.data.config import DataConfig
from tradebot.data.errors import DataError
from tradebot.data.provider import DataProvider
from tradebot.data.quality import validate_bars
from tradebot.data.symbols import normalize_symbol
from tradebot.data.types import Symbol, Timeframe
from tradebot.data.universe import (
    _sp500_aliases,
    sp500_ever,
    sp500_member_flags,
)

log = logging.getLogger(__name__)

CACHE_SOURCE = "yfinance"  # the normal equity daily cache key (MarketData routes equities here)
ALPACA_REV = "alpaca_sip"
HISTORY_LOOKBACK_START = date(2015, 1, 1)  # a few months before the window, for indicator warmup


def member_days(symbol: Symbol, start: date, end: date) -> list[date]:
    """XNYS sessions in ``[start, end]`` on which ``symbol`` is an S&P 500 member."""
    days = [ts.date() for ts in sessions("SPY", start, end)]
    flags = sp500_member_flags(symbol, days)
    return [d for d, f in zip(days, flags, strict=True) if f == 1.0]


FILLER_RUN_CUT = 5  # a run of this many filler bars means the ticker was reused: cut from there
MOVE_LIMIT = 0.40  # one-day |close move| above this is listed for the exclusion file
REVERSAL_BARS = 5  # a move is "reversed" if the close is back near its old level within this
CORP_ACTION_WINDOW = 5  # sessions around a membership start/end that count as a known event


def filler_mask(frame: pd.DataFrame) -> pd.Series:
    """True on filler bars: volume 0 and open = high = low = close (carry-forward prints)."""
    return (
        (frame["volume"] == 0)
        & (frame["open"] == frame["high"])
        & (frame["high"] == frame["low"])
        & (frame["low"] == frame["close"])
    )


def clean_alpaca_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Clean an Alpaca-sourced daily series (ADR-0001 A33 M1/M2).

    Everything from the first run of ``FILLER_RUN_CUT`` or more consecutive filler bars is cut
    (the ticker was reused by another company); remaining isolated filler bars are dropped.
    Returns the cleaned frame and counts ``{"filler_dropped", "truncated_rows"}``.
    """
    flags = filler_mask(frame).to_numpy()
    cut = len(frame)
    run = 0
    for i, f in enumerate(flags):
        run = run + 1 if f else 0
        if run >= FILLER_RUN_CUT:
            cut = i - run + 1
            break
    kept = frame.iloc[:cut]
    keep_flags = flags[:cut]
    out = kept.loc[~keep_flags]
    return out, {"filler_dropped": int(keep_flags.sum()), "truncated_rows": len(frame) - cut}


def is_alpaca_sourced(frame: pd.DataFrame) -> bool:
    """True if every row's audit ``source_rev`` is an Alpaca revision."""
    return bool(len(frame)) and all(
        str(r).startswith("alpaca") for r in frame["source_rev"].unique()
    )


@dataclass
class RecleanResult:
    cleaned: dict[Symbol, dict[str, int]] = field(default_factory=dict)  # changed series
    unchanged: list[Symbol] = field(default_factory=list)
    skipped_yfinance: int = 0


def reclean_alpaca_cache(cache: BarCache, symbols: Iterable[Symbol]) -> RecleanResult:
    """Re-clean cached Alpaca-sourced series in place (new cache generation); yfinance untouched."""
    res = RecleanResult()
    for sym in symbols:
        if not cache.exists(CACHE_SOURCE, sym):
            continue
        frame = cache.read(CACHE_SOURCE, sym, include_audit=True)
        if not is_alpaca_sourced(frame):
            res.skipped_yfinance += 1
            continue
        clean, stats = clean_alpaca_frame(frame)
        if stats["filler_dropped"] == 0 and stats["truncated_rows"] == 0:
            res.unchanged.append(sym)
            continue
        basis = str(cache.gen_info(CACHE_SOURCE, sym)["basis"])
        if clean.empty:
            raise DataError(f"{sym}: nothing left after cleaning; refusing to write")
        cache.rebase(CACHE_SOURCE, sym, clean, basis, ALPACA_REV)
        res.cleaned[sym] = stats
    return res


@dataclass(frozen=True)
class BigMove:
    symbol: Symbol
    day: date
    move: float
    reason: str


def _membership_events(symbol: Symbol) -> list[date]:
    """Dates on which ``symbol``'s S&P 500 membership starts or ends (known corporate events)."""
    days = [ts.date() for ts in sessions("SPY", date(2014, 6, 1), date(2023, 12, 31))]
    flags = sp500_member_flags(symbol, days)
    return [days[i] for i in range(1, len(days)) if flags[i] != flags[i - 1]]


def big_moves(symbol: Symbol, frame: pd.DataFrame) -> list[BigMove]:
    """One-day close moves above ``MOVE_LIMIT`` that touch a membership event or stay unreversed.

    "Known corporate-action date" is proxied by the symbol's own index start/end dates (within
    ``CORP_ACTION_WINDOW`` sessions): no offline corporate-action feed exists. "Not reversed":
    the close ``REVERSAL_BARS`` bars later is still more than 20% away from the pre-move close.
    """
    close = frame["close"].astype(float)
    ret = close / close.shift(1) - 1.0
    events = _membership_events(symbol)
    out: list[BigMove] = []
    idx = list(frame.index)
    for i in range(1, len(idx)):
        move = float(ret.iloc[i])
        if abs(move) <= MOVE_LIMIT:
            continue
        d = idx[i].date()
        reasons: list[str] = []
        near = [e for e in events if abs(sessions_between(e, d)) <= CORP_ACTION_WINDOW]
        if near:
            reasons.append(f"index change {near[0].isoformat()}")
        later = float(close.iloc[min(i + REVERSAL_BARS, len(close) - 1)])
        if abs(later / float(close.iloc[i - 1]) - 1.0) > 0.20:
            reasons.append("not reversed")
        if reasons:
            out.append(BigMove(symbol, d, move, "; ".join(reasons)))
    return out


def sessions_between(a: date, b: date) -> int:
    """Signed XNYS session count from ``a`` to ``b`` (approximate: business days)."""
    return int(pd.bdate_range(min(a, b), max(a, b)).size - 1) * (1 if b >= a else -1)


@dataclass
class SymbolCoverage:
    """Price coverage of one symbol over its member days."""

    symbol: Symbol
    member_days: int
    covered_days: int
    source: str  # "yfinance", "alpaca_sip", "mixed" or "none"
    first_bar: date | None = None
    last_bar: date | None = None
    note: str = ""

    @property
    def share(self) -> float:
        return self.covered_days / self.member_days if self.member_days else 1.0


def read_coverage(cache: BarCache, symbol: Symbol, start: date, end: date) -> SymbolCoverage:
    """Coverage of the cached daily bars of ``symbol`` over its member days in the window."""
    mdays = member_days(symbol, start, end)
    if not cache.exists(CACHE_SOURCE, symbol):
        return SymbolCoverage(symbol, len(mdays), 0, "none")
    frame = cache.read(CACHE_SOURCE, symbol, include_audit=True)
    have = set(frame.index.date)
    covered = sum(1 for d in mdays if d in have)
    revs = {str(r) for r in frame["source_rev"].unique()}
    kinds = {"alpaca_sip" if r.startswith("alpaca") else "yfinance" for r in revs}
    source = kinds.pop() if len(kinds) == 1 else "mixed"
    return SymbolCoverage(
        symbol,
        len(mdays),
        covered,
        source,
        frame.index[0].date() if len(frame) else None,
        frame.index[-1].date() if len(frame) else None,
    )


def alpaca_candidates(symbol: Symbol) -> list[Symbol]:
    """Tickers Alpaca may know ``symbol`` by: itself, then source tickers aliased to it."""
    older = sorted(raw for raw, price in _sp500_aliases().items() if price == symbol)
    return [symbol, *(normalize_symbol(t) for t in older if normalize_symbol(t) != symbol)]


@dataclass
class FetchResult:
    cached_before: list[Symbol] = field(default_factory=list)
    from_yfinance: list[Symbol] = field(default_factory=list)
    from_alpaca: list[Symbol] = field(default_factory=list)
    failed: dict[Symbol, str] = field(default_factory=dict)


def fetch_alpaca_daily(
    symbols: Sequence[Symbol],
    start: date,
    end: date,
    *,
    cache: BarCache,
    provider: DataProvider,
    clock: Clock | None = None,
    member_start: date | None = None,
    member_end: date | None = None,
    skip_existing: bool = True,
    replace_if: Callable[[Symbol], bool] | None = None,
) -> FetchResult:
    """Fetch Alpaca daily bars for ``symbols`` and create their cache entries.

    For each symbol the candidate tickers (``alpaca_candidates``) are tried and the one covering
    most member days is validated (``validate_bars``) and written whole. Symbols with no usable
    bars are returned in ``failed``. Existing cache entries are never changed, except those for
    which ``replace_if(symbol)`` is true (the caller vouches the entry is junk it created itself,
    e.g. a reused ticker or a one-bar yfinance stub): such an entry is replaced by a new cache
    generation only if Alpaca covers strictly more of its member days.
    """
    clock = clock or SystemClock()
    now = clock.now()
    ms, me = member_start or start, member_end or end
    out = FetchResult()
    replaceable = {
        s
        for s in symbols
        if replace_if is not None and cache.exists(CACHE_SOURCE, s) and replace_if(s)
    }
    todo = [
        s
        for s in symbols
        if s in replaceable or not (skip_existing and cache.exists(CACHE_SOURCE, s))
    ]
    wanted: dict[Symbol, list[Symbol]] = {s: alpaca_candidates(s) for s in todo}
    tickers = sorted({t for ts in wanted.values() for t in ts})
    fetch_many = getattr(provider, "fetch_many", None)
    if fetch_many is None:
        raise DataError("provider has no fetch_many")
    frames: dict[Symbol, pd.DataFrame] = fetch_many(tickers, start, end, Timeframe.D1)
    for symbol, cands in wanted.items():
        mdays = member_days(symbol, ms, me)
        best: tuple[int, Symbol, pd.DataFrame] | None = None
        for cand in cands:
            frame = frames.get(cand)
            if frame is None or frame.empty:
                continue
            have = set(frame.index.date)
            score = sum(1 for d in mdays if d in have)
            if best is None or score > best[0]:
                best = (score, cand, frame)
        if best is None or best[0] == 0:
            out.failed[symbol] = "alpaca returned no bars"
            continue
        try:
            clean, _ = validate_bars(best[2], symbol, now)
        except DataError as exc:
            out.failed[symbol] = f"rejected by validate_bars: {exc}"
            continue
        clean, _ = clean_alpaca_frame(clean)  # A33 M1/M2: filler bars, reused tickers
        if clean.empty:
            out.failed[symbol] = "no bars after validation"
            continue
        basis = f"{REAL_BASIS_PREFIX}{now.date().isoformat()}"
        if symbol in replaceable:
            have = set(cache.read(CACHE_SOURCE, symbol).index.date)
            if best[0] <= sum(1 for d in mdays if d in have):
                continue  # the existing series is no worse
            cache.rebase(CACHE_SOURCE, symbol, clean, basis, ALPACA_REV)
        else:
            cache.create(CACHE_SOURCE, symbol, clean, basis, ALPACA_REV)
        out.from_alpaca.append(symbol)
    return out


def compare_sources(
    cache: BarCache, symbol: Symbol, other: pd.DataFrame
) -> dict[str, float] | None:
    """Close-price ratio ``other / cached`` on shared dates: median and 5th/95th percentile.

    Used to measure the yfinance-vs-Alpaca adjustment gap on names both serve (``None`` if the
    series share fewer than 20 dates).
    """
    if not cache.exists(CACHE_SOURCE, symbol) or other.empty:
        return None
    base = cache.read(CACHE_SOURCE, symbol)
    shared = base.index.intersection(other.index)
    if len(shared) < 20:
        return None
    ratio = other.loc[shared, "close"] / base.loc[shared, "close"]
    return {
        "n": float(len(shared)),
        "median": float(ratio.median()),
        "p05": float(ratio.quantile(0.05)),
        "p95": float(ratio.quantile(0.95)),
    }


def build_pit_prices(
    start: date,
    end: date,
    *,
    config: DataConfig | None = None,
    alpaca: DataProvider | None = None,
    symbols: Iterable[Symbol] | None = None,
) -> FetchResult:
    """Make sure every ``sp500_ever(start, end)`` symbol has daily bars in the cache.

    yfinance first (through ``MarketData``), then Alpaca for what is still missing. Network.
    """
    from tradebot.data.providers.alpaca_provider import AlpacaProvider
    from tradebot.data.service import MarketData

    cfg = config or DataConfig()
    clock = SystemClock()
    md = MarketData(cfg, clock=clock)
    cache = BarCache(cfg.cache_dir, clock)
    universe = sorted(symbols if symbols is not None else sp500_ever(start, end))
    result = FetchResult()
    result.cached_before = [s for s in universe if cache.exists(CACHE_SOURCE, s)]
    missing = [s for s in universe if s not in set(result.cached_before)]
    if missing:
        md.refresh(missing, start=HISTORY_LOOKBACK_START)
    result.from_yfinance = [s for s in missing if cache.exists(CACHE_SOURCE, s)]
    today = clock.now().date().isoformat()

    def junk(symbol: Symbol) -> bool:
        """A cache entry made during this build whose member-day coverage is poor."""
        made = str(cache.gen_info(CACHE_SOURCE, symbol, 1).get("created", ""))
        return made.startswith(today) and read_coverage(cache, symbol, start, end).share < 0.9

    still = [s for s in universe if not cache.exists(CACHE_SOURCE, s) or junk(s)]
    if still:
        provider = alpaca or AlpacaProvider(clock=clock)
        got = fetch_alpaca_daily(
            still,
            HISTORY_LOOKBACK_START,
            end,
            cache=cache,
            provider=provider,
            clock=clock,
            member_start=start,
            member_end=end,
            replace_if=junk,
        )
        result.from_alpaca = got.from_alpaca
        result.failed = got.failed
    return result


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, default=date(2015, 6, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2023, 12, 31))
    args = ap.parse_args(argv)
    res = build_pit_prices(args.start, args.end)
    print(
        f"cached before: {len(res.cached_before)}; yfinance: {len(res.from_yfinance)}; "
        f"alpaca: {len(res.from_alpaca)}; failed: {len(res.failed)}"
    )
    for sym, why in sorted(res.failed.items()):
        print(f"  no data: {sym} ({why})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
