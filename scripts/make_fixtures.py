"""Generate the committed synthetic market fixtures (SPEC-005 section 2).

Synthetic data; no network; seed 20180102.

Usage:
    uv run python scripts/make_fixtures.py --write   # rebuild tests/fixtures/market{,_edge}
    uv run python scripts/make_fixtures.py --check   # regenerate to a temp dir and compare

The output uses SPEC-001's parquet cache layout so tests exercise the real read path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

SEED = 20180102
GENERATOR_VERSION = 1
START = "2018-01-02"
END = "2024-12-31"
SOL_START = "2020-04-10"
CREATED = "2025-01-01T00:00:00Z"
SOURCE_REV = "synthetic-v1"
BUDGET_TOTAL = 2_500_000
BUDGET_FILE = 200_000

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

ETFS = [
    "SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "XLK",
    "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
]  # fmt: skip
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD"]

BULL, CHOP, CRASH, REBOUND, BEAR = "BULL", "CHOP", "CRASH", "REBOUND", "BEAR"
MU = {BULL: 0.0006, CHOP: 0.0, CRASH: -0.0175, REBOUND: 0.0040, BEAR: -0.0009}
SIGMA = {BULL: 0.0075, CHOP: 0.0110, CRASH: 0.0300, REBOUND: 0.0200, BEAR: 0.0140}
STAY = {BULL: 0.985, CHOP: 0.95}
FORCED = [
    (CRASH, "2020-02-19", "2020-03-23"),
    (REBOUND, "2020-03-24", "2020-05-29"),
    (BEAR, "2022-01-03", "2022-10-12"),
]
D_R = {CRASH: 0.0005, BEAR: -0.0007}
D_C = {BULL: 0.0001, CRASH: -0.008, REBOUND: 0.005, BEAR: -0.002, CHOP: 0.0}
CORR = np.array(
    [
        [1, 0.3, -0.2, 0.3, 0.35],
        [0.3, 1, -0.1, 0, 0.25],
        [-0.2, -0.1, 1, -0.1, 0],
        [0.3, 0, -0.1, 1, 0.1],
        [0.35, 0.25, 0, 0.1, 1],
    ],
    dtype=float,
)


@dataclass(frozen=True)
class Params:
    """Per-symbol factor loadings and level parameters."""

    beta: float
    g_g: float
    g_r: float
    g_e: float
    g_c: float
    alpha_ann: float
    idio: float
    p0: float
    vol: float


# beta, gG, gR, gE, gC, alpha_ann, idio, P0, base volume
_TABLE: dict[str, tuple[float, ...]] = {
    "SPY": (1.00, 0, 0, 0, 0, 0.03, 0.0015, 270, 8.0e7),
    "QQQ": (1.10, 0.8, 0, 0, 0, 0.04, 0.0030, 160, 4.0e7),
    "IWM": (1.15, -0.2, 0, 0, 0, 0, 0.0050, 150, 2.5e7),
    "EFA": (0.85, 0, 0, 0, 0, 0, 0.0040, 70, 2.0e7),
    "EEM": (0.95, 0, 0, 0.1, 0, 0, 0.0065, 47, 3.5e7),
    "TLT": (-0.15, 0, 1.00, 0, 0, 0, 0.0020, 125, 1.0e7),
    "IEF": (-0.05, 0, 0.45, 0, 0, 0, 0.0010, 105, 5.0e6),
    "GLD": (0.05, 0, 0.20, 0.1, 0, 0.02, 0.0050, 125, 8.0e6),
    "XLK": (1.05, 0.9, 0, 0, 0, 0.03, 0.0040, 65, 1.0e7),
    "XLF": (1.10, 0, -0.30, 0, 0, 0, 0.0045, 28, 4.0e7),
    "XLE": (0.90, 0, 0, 1.0, 0, 0, 0.0050, 70, 1.5e7),
    "XLV": (0.75, 0, 0, 0, 0, 0.02, 0.0040, 85, 8.0e6),
    "XLI": (1.00, 0, 0, 0, 0, 0.01, 0.0035, 72, 1.0e7),
    "XLY": (1.00, 0.4, 0, 0, 0, 0.02, 0.0040, 105, 5.0e6),
    "XLP": (0.55, 0, 0, 0, 0, 0.01, 0.0035, 55, 1.2e7),
    "XLU": (0.45, 0, 0.35, 0, 0, 0.01, 0.0050, 52, 9.0e6),
    "XLB": (0.95, 0, 0, 0.3, 0, 0, 0.0040, 56, 5.0e6),
    "BTC/USD": (0.60, 0, 0, 0, 1.00, 0, 0.0150, 13500, 3.0e4),
    "ETH/USD": (0.80, 0, 0, 0, 1.15, 0, 0.0200, 750, 3.0e5),
    "SOL/USD": (0.90, 0, 0, 0, 1.30, 0, 0.0300, 0.5, 5.0e6),
}
PARAMS = {k: Params(*v) for k, v in _TABLE.items()}


def clone(p0: float) -> Params:
    """Edge-symbol clone parameters (beta 1.0, idio 0.004, nothing else)."""
    return Params(1.0, 0, 0, 0, 0, 0.0, 0.004, p0, 1.0e6)


# ---------------------------------------------------------------- factors
def calendar_days() -> pd.DatetimeIndex:
    """Every UTC day in the generation range."""
    return pd.date_range(START, END, freq="D", tz="UTC")


def xnys_sessions(start: str = START, end: str = END) -> pd.DatetimeIndex:
    """Real XNYS sessions as tz-aware UTC midnight."""
    cal = xcals.get_calendar("XNYS")
    s = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return pd.DatetimeIndex(s).tz_localize("UTC") if s.tz is None else s.tz_convert("UTC")


@dataclass
class Factors:
    """Market-wide daily factor series over all calendar days."""

    days: pd.DatetimeIndex
    reg: list[str]
    M: np.ndarray
    G: np.ndarray
    R: np.ndarray
    E: np.ndarray
    C: np.ndarray


def build_factors() -> Factors:
    """Draw the master stream and build regimes and factors (fixed draw order)."""
    days = calendar_days()
    n = len(days)
    rng = np.random.Generator(np.random.PCG64(SEED))
    u = rng.random(n)
    z = rng.standard_normal((n, 5)) @ np.linalg.cholesky(CORR).T

    forced: dict[int, str] = {}
    for name, a, b in FORCED:
        for i in np.flatnonzero(
            (days >= pd.Timestamp(a, tz="UTC")) & (days <= pd.Timestamp(b, tz="UTC"))
        ):
            forced[int(i)] = name
    reg: list[str] = []
    s = BULL
    for i in range(n):
        if i in forced:
            s = BULL
            reg.append(forced[i])
            continue
        if u[i] >= STAY[s]:
            s = CHOP if s == BULL else BULL
        reg.append(s)

    m = np.array([MU[r] + SIGMA[r] * z[i, 0] for i, r in enumerate(reg)])
    g = 0.006 * z[:, 1]
    r_ = np.array([D_R.get(r, 0.0) + 0.0045 * z[i, 2] for i, r in enumerate(reg)])
    e = 0.012 * z[:, 3]
    dc = np.array([D_C[r] for r in reg])
    rally = (days >= pd.Timestamp("2020-10-01", tz="UTC")) & (
        days <= pd.Timestamp("2021-04-14", tz="UTC")
    )
    dc[rally] = 0.0035
    c = dc + 0.025 * z[:, 4]
    return Factors(days, reg, m, g, r_, e, c)


def gen_symbol(
    fac: Factors, k: int, p: Params, dates: pd.DatetimeIndex, crypto: bool
) -> pd.DataFrame:
    """Generate unrounded-then-rounded OHLCV for one symbol on `dates` (subset of fac.days)."""
    n_all = len(fac.days)
    r = np.random.Generator(np.random.PCG64(np.random.SeedSequence([SEED, k])))
    eps = r.standard_normal(n_all)
    ae = r.standard_normal(n_all)
    pos = fac.days.get_indexer(dates)
    assert (pos >= 0).all()
    n_bars = len(dates)
    u1, u2, u3, w = (r.standard_normal(n_bars) for _ in range(4))

    idio = p.idio
    sa = 6e-5 * float(np.clip(idio / 0.004, 0.3, 1.5))
    a_all = np.zeros(n_all)
    for t in range(1, n_all):
        a_all[t] = 0.99 * a_all[t - 1] + sa * ae[t]
    a = a_all[pos]
    alpha = 0.0 if crypto else p.alpha_ann / 252
    ret = (
        alpha
        + a
        + p.beta * fac.M[pos]
        + p.g_g * fac.G[pos]
        + p.g_r * fac.R[pos]
        + p.g_e * fac.E[pos]
        + p.g_c * fac.C[pos]
        + idio * eps[pos]
    )
    close = p.p0 * np.exp(np.cumsum(ret))
    prev = np.concatenate([[p.p0], close[:-1]])
    s = float(np.std(ret, ddof=1))
    open_ = prev * np.exp(0.3 * ret + 0.15 * s * u1)
    high_raw = np.maximum(open_, close) * np.exp(np.abs(0.6 * s * u2))
    low_raw = np.minimum(open_, close) * np.exp(-np.abs(0.6 * s * u3))
    o, h, lo, c = (np.round(x, 4) for x in (open_, high_raw, low_raw, close))
    h = np.maximum(h, np.maximum(o, c))
    lo = np.minimum(lo, np.minimum(o, c))
    assert (lo <= np.minimum(o, c)).all() and (h >= np.maximum(o, c)).all()
    assert min(o.min(), h.min(), lo.min(), c.min()) >= 0.01
    vol = np.maximum(1.0, np.round(p.vol * np.exp(0.2 * w + 8 * np.abs(ret))))
    df = pd.DataFrame(
        {"open": o, "high": h, "low": lo, "close": c, "volume": vol.astype("float64")},
        index=pd.DatetimeIndex(dates, name="ts"),
    )
    return df


def with_audit(df: pd.DataFrame) -> pd.DataFrame:
    """Append the cache-internal audit columns."""
    out = df.copy()
    out["fetched_at"] = pd.Timestamp(CREATED)
    out["source_rev"] = SOURCE_REV
    return out


# ---------------------------------------------------------------- universe
def fs_name(symbol: str) -> str:
    """Filesystem-safe symbol name (BTC/USD -> BTC_USD)."""
    return symbol.replace("/", "_")


def clean_universe(fac: Factors) -> dict[str, tuple[str, pd.DataFrame]]:
    """Return {symbol: (source, frame)} for the 20 clean symbols."""
    sess = xnys_sessions()
    out: dict[str, tuple[str, pd.DataFrame]] = {}
    for k, sym in enumerate(ETFS + CRYPTO):
        crypto = sym in CRYPTO
        dates = fac.days if crypto else sess
        if sym == "SOL/USD":
            dates = dates[dates >= pd.Timestamp(SOL_START, tz="UTC")]
        src = "coinbaseexchange" if crypto else "yfinance"
        out[sym] = (src, gen_symbol(fac, k, PARAMS[sym], dates, crypto))
    return out


def edge_universe(fac: Factors) -> dict[str, tuple[str, pd.DataFrame]]:
    """Return the eight edge-case symbols (defects intentionally left in)."""
    sess = xnys_sessions()

    def rng_(a: str, b: str) -> pd.DatetimeIndex:
        return sess[(sess >= pd.Timestamp(a, tz="UTC")) & (sess <= pd.Timestamp(b, tz="UTC"))]

    def ts(d: str) -> pd.Timestamp:
        return pd.Timestamp(d, tz="UTC")

    out: dict[str, pd.DataFrame] = {}
    out["ZZNEW"] = gen_symbol(fac, 100, clone(50), rng_("2021-03-01", END), False)
    out["ZZGONE"] = gen_symbol(fac, 101, clone(40), rng_(START, "2022-06-30"), False)

    gap = gen_symbol(fac, 102, clone(100), rng_(START, "2019-12-31"), False)
    drop = [
        ts(d)
        for d in (
            "2019-07-08",
            "2019-07-09",
            "2019-07-10",
            "2019-07-11",
            "2019-07-12",
            "2019-07-15",
        )
    ]
    out["ZZGAP"] = gap.drop(index=drop)

    dup = gen_symbol(fac, 103, clone(100), rng_(START, "2019-12-31"), False)
    row = dup.loc[[ts("2019-03-12")]].copy()
    row["close"] = np.round(row["close"] * 1.01, 4)
    row["high"] = np.maximum(row["high"], row["close"])
    dup = pd.concat(
        [
            dup.iloc[: dup.index.get_loc(ts("2019-03-12")) + 1],
            row,
            dup.iloc[dup.index.get_loc(ts("2019-03-12")) + 1 :],
        ]
    )
    out["ZZDUP"] = dup

    tz = gen_symbol(fac, 104, clone(100), rng_(START, "2019-12-31"), False)
    tz.index = tz.index.tz_convert("America/New_York")
    out["ZZTZ"] = tz

    bad = gen_symbol(fac, 105, clone(100), rng_(START, "2019-12-31"), False)
    hi, lo = bad.loc[ts("2019-05-15"), "high"], bad.loc[ts("2019-05-15"), "low"]
    bad.loc[ts("2019-05-15"), ["high", "low"]] = [lo, hi]
    bad.loc[ts("2019-05-16"), "close"] = 0.0
    out["ZZBAD"] = bad

    idx = sess[:300]
    flat = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1_000_000.0},
        index=pd.DatetimeIndex(idx, name="ts"),
    )
    out["ZZFLAT"] = flat
    t = np.arange(300)
    close = 100.0 + 0.5 * t
    open_ = np.concatenate([[99.75], close[:-1]])
    out["ZZUP"] = pd.DataFrame(
        {
            "open": open_,
            "high": close,
            "low": open_,
            "close": close,
            "volume": np.full(300, 1_000_000.0),
        },
        index=pd.DatetimeIndex(idx, name="ts"),
    )
    return {
        s: ("yfinance", with_audit(df) if "fetched_at" not in df else df) for s, df in out.items()
    }


# ---------------------------------------------------------------- acceptance
def max_drawdown(x: pd.Series) -> float:
    """Peak-to-trough drawdown as a positive fraction."""
    return float((1 - x / x.cummax()).max())


def check_bands(uni: dict[str, tuple[str, pd.DataFrame]]) -> dict[str, float]:
    """Assert the SPEC-005 acceptance bands and anchors; return measured values."""
    m: dict[str, float] = {}
    spy = uni["SPY"][1]["close"]

    def d(a: str, b: str) -> pd.Series:
        lo, hi = pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC")
        return spy[(spy.index >= lo) & (spy.index <= hi)]

    m["spy_crash_dd"] = max_drawdown(d("2020-02-19", "2020-03-23"))
    m["spy_bear_dd"] = max_drawdown(d("2022-01-03", "2022-10-12"))
    assert 0.25 <= m["spy_crash_dd"] <= 0.45, m
    assert 0.20 <= m["spy_bear_dd"] <= 0.35, m
    p0 = {s: PARAMS[s].p0 for s in CRYPTO}
    for s, lo_, hi_ in (("BTC/USD", 3, 15), ("ETH/USD", 2, 15), ("SOL/USD", 5, 100)):
        c = uni[s][1]["close"]
        m[f"{s}_ratio"] = float(c.iloc[-1] / p0[s])
        assert lo_ <= m[f"{s}_ratio"] <= hi_, m
    m["btc_dd"] = max_drawdown(uni["BTC/USD"][1]["close"])
    assert m["btc_dd"] >= 0.60, m
    closes = pd.DataFrame({s: uni[s][1]["close"] for s in ETFS})
    above = (closes > closes.rolling(200).mean()).sum(axis=1)[199:]
    m["sma_min"], m["sma_max"] = float(above.min()), float(above.max())
    assert m["sma_min"] <= 4 and m["sma_max"] >= 15, m
    me = closes.groupby([closes.index.year, closes.index.month]).tail(1).index
    pos = closes.index.get_indexer(me)
    tops = []
    for i in pos:
        if i >= 126:
            r6 = closes.iloc[i] / closes.iloc[i - 126] - 1
            tops.append(frozenset(r6.nlargest(3).index))
    changes = [a != b for a, b in zip(tops, tops[1:], strict=False)]
    m["top3_change"] = float(np.mean(changes))
    assert m["top3_change"] >= 0.5, m
    anchors = [
        (
            "SPY_first",
            tuple(uni["SPY"][1].iloc[0][["open", "high", "low", "close"]]),
            (271.8521, 277.9405, 268.1065, 274.3097),
        ),
        ("SPY_last", (uni["SPY"][1]["close"].iloc[-1],), (444.458,)),
        ("BTC_first", (uni["BTC/USD"][1]["close"].iloc[0],), (14476.3872,)),
        ("BTC_last", (uni["BTC/USD"][1]["close"].iloc[-1],), (91316.3118,)),
        ("SOL_first", (uni["SOL/USD"][1]["close"].iloc[0],), (0.5298,)),
        ("SOL_last", (uni["SOL/USD"][1]["close"].iloc[-1],), (14.8407,)),
        ("XLE_last", (uni["XLE"][1]["close"].iloc[-1],), (90.2088,)),
    ]
    for name, got, want in anchors:
        for g, w_ in zip(got, want, strict=True):
            m[name] = float(g)
            if abs(g - w_) > 1e-3 * abs(w_):
                raise AssertionError(f"anchor {name}: got {g}, want {w_}")
    assert uni["SOL/USD"][1].index[0] == pd.Timestamp(SOL_START, tz="UTC")
    for s in ETFS:
        assert len(uni[s][1]) == 1761, s
    assert len(uni["BTC/USD"][1]) == len(uni["ETH/USD"][1]) == 2556
    assert len(uni["SOL/USD"][1]) == 1727
    return m


# ---------------------------------------------------------------- writing
def sha256(path: Path) -> str:
    """Hex sha256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump_json(path: Path, obj: object) -> None:
    """Write deterministic JSON."""
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def iso(ts_: pd.Timestamp) -> str:
    """UTC calendar date of a timestamp as YYYY-MM-DD."""
    t = ts_.tz_convert("UTC") if ts_.tzinfo else ts_
    return t.strftime("%Y-%m-%d")


def write_tree(root: Path, uni: dict[str, tuple[str, pd.DataFrame]]) -> None:
    """Write one fixture tree (parquet, meta, gen, manifest) under root."""
    if root.exists():
        shutil.rmtree(root)
    files: dict[str, dict[str, object]] = {}
    for sym, (src, df) in uni.items():
        d = root / src / fs_name(sym)
        (d / "gen-0001").mkdir(parents=True)
        bars = d / "gen-0001" / "bars.parquet"
        df.to_parquet(bars, engine="pyarrow", compression="snappy")
        utc = df.index.tz_convert("UTC")
        dump_json(d / "meta.json", {"current_gen": 1, "tf": "1d", "schema": 1})
        dump_json(
            d / "gen-0001" / "gen.json",
            {
                "created": CREATED,
                "basis": f"synthetic_seed_{SEED}",
                "events": [],
                "first": iso(utc.min()),
                "last": iso(utc.max()),
                "sha256": sha256(bars),
            },
        )
        for p in (d / "meta.json", d / "gen-0001" / "gen.json", bars):
            rel = p.relative_to(root).as_posix()
            e: dict[str, object] = {"sha256": sha256(p), "bytes": p.stat().st_size}
            if p is bars:
                e.update(rows=len(df), first=iso(utc.min()), last=iso(utc.max()))
            files[rel] = e
    dump_json(
        root / "manifest.json",
        {
            "schema": 1,
            "generator": "scripts/make_fixtures.py",
            "generator_version": GENERATOR_VERSION,
            "seed": SEED,
            "synthetic": True,
            "files": files,
            "total_bytes": sum(int(e["bytes"]) for e in files.values()),  # type: ignore[arg-type]
        },
    )


def build(out: Path) -> dict[str, float]:
    """Generate both trees under `out`; return measured acceptance values."""
    fac = build_factors()
    clean = clean_universe(fac)
    clean = {s: (src, with_audit(df)) for s, (src, df) in clean.items()}
    measured = check_bands({s: (src, df) for s, (src, df) in clean.items()})
    write_tree(out / "market", clean)
    write_tree(out / "market_edge", edge_universe(fac))
    total = 0
    for p in out.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            if p.suffix == ".parquet":
                assert p.stat().st_size <= BUDGET_FILE, f"{p} too large"
    assert total <= BUDGET_TOTAL, f"fixtures {total} bytes exceed budget"
    measured["total_bytes"] = float(total)
    return measured


def check(committed: Path) -> bool:
    """Regenerate to a temp dir and compare with the committed trees."""
    ok = True
    with tempfile.TemporaryDirectory() as td:
        build(Path(td))
        for tree in ("market", "market_edge"):
            for new in sorted((Path(td) / tree).rglob("bars.parquet")):
                rel = new.relative_to(td)
                old = committed / rel
                if not old.exists():
                    print(f"missing {rel}")
                    ok = False
                    continue
                a, b = pd.read_parquet(new), pd.read_parquet(old)
                try:
                    pd.testing.assert_frame_equal(
                        a, b, check_exact=False, rtol=1e-9, atol=0, check_freq=False
                    )
                except AssertionError as exc:
                    print(f"mismatch {rel}: {exc}")
                    ok = False
    return ok


def main() -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.write:
        m = build(FIXTURES)
        for k, v in m.items():
            print(f"{k}: {v:.4f}")
        return 0
    return 0 if check(FIXTURES) else 1


if __name__ == "__main__":
    sys.exit(main())
