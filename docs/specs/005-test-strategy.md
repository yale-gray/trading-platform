# SPEC-005: Test strategy and correctness guardrails

- **Lead:** quality-lead
- **Status:** approved (conforms to ADR-0001)
- **Roadmap item:** Phase 0.5 (lead research round); applies to Phases 1-4
- **Related ADRs/specs:** SPEC-001 market data, SPEC-002 strategy/backtest, SPEC-003 execution/sim/portfolio/risk, SPEC-004 platform and ADR-0001 (architecture and module contracts; it wins on every conflict).

## Goal
Define how we prove the numbers are true: test layers and who owns each, offline fixture policy, lookahead and accounting guardrails, golden backtests, hand-calculated references, validation-gate tests, the audit process and CI gates. Every claim in `docs/STRATEGY.md` §7-8 (no lookahead, costs on every fill, date-aware universe, deterministic results) becomes an automated check.

## Non-goals
- Designing features (other leads) or writing test code (workers write it from this spec).
- Live-broker or network integration tests (forbidden by CLAUDE.md hard rule 1 and the offline rule below).
- Performance or load testing beyond the runtime budget in §9.
- Mutation testing (deferred until after Phase 3, ADR-0001 D12).

## Design

### 1. Test layers and ownership
Directory layout under `tests/`: `unit/`, `property/`, `lookahead/`, `integration/`, `golden/`, `gate/`, `runner/`, `support/` (shared helpers, no tests), `fixtures/`. Each layer has a pytest marker (`unit`, `property`, `integration`, `golden`, `restart`, `slow`); `--strict-markers` is on.

| Layer | What it proves | Owned by (area) | Written by |
|---|---|---|---|
| Unit | One function/class, exact hand-worked numbers, edge cases (empty, NaN, single bar) | each area's lead defines in its spec; quality-lead defines the reference cases in §6 | test-engineer (haiku) |
| Property-based | Invariants hold for *all* generated inputs (accounting, risk limits, fill timing, determinism) | execution (accounting, risk), strategy (indicator/strategy invariants), data (bar validation) | engineer (sonnet) |
| Lookahead (truncation) | Past signals are unchanged by future bars | strategy (indicators, signals, backtest) | engineer (sonnet) |
| Integration | Backtest end-to-end on committed fixtures through the real cache read path, `BarDriver`, strategy, `PortfolioEngine`, ledger, metrics | strategy + execution jointly; quality-lead owns the scenario list | engineer (sonnet) |
| Golden | Pinned numbers for 4 backtests on the fixture window; catches silent behavior drift | quality-lead owns values and update procedure | engineer (sonnet) harness; PM approves values |
| Validation-gate | STRATEGY.md §7 as code rejects an overfit strategy and accepts a good one | strategy (gate code), quality-lead (test design) | engineer (sonnet) |
| Runner restart/idempotency | No double-trade on restart/replay/crash; runner result equals batch backtest | platform (runner), execution (idempotent order keys) | engineer (sonnet) |

Rule: anything that computes money, positions, fills, P&L or risk gets property tests in addition to unit tests. Anything that computes a signal gets a truncation test. No exceptions without a PM note.

**Use `hypothesis`: yes.** Reasons: (a) accounting bugs hide in rare order/price sequences (partial rebalances, tiny fractional crypto quantities, a stop firing on the same bar as a rebalance) that hand-picked examples never reach; (b) shrinking returns a minimal failing sequence, which makes a Decimal off-by-one cent debuggable; (c) it is dev-only, with no runtime cost; (d) it can be made deterministic. Settings in `tests/conftest.py`:
```python
settings.register_profile("ci", max_examples=200, deadline=None, derandomize=True, print_blob=True)
settings.register_profile("dev", max_examples=50, deadline=None)
# profile chosen by env HYPOTHESIS_PROFILE, default "dev"; CI sets "ci"
```
`derandomize=True` means CI never flakes, and failures reproduce locally. The `.hypothesis/` directory is git-ignored (T2 adds it to `.gitignore`). A property test that finds a bug must be turned into a plain regression unit test (copy the shrunk example) in the same PR.

### 2. Fixture policy and offline enforcement
**Rule: no test touches the network, the wall clock, or the user's real cache/DB.** Tests use committed fixtures and `tmp_path`. The repo is public, so **no Yahoo or exchange data is ever committed** (ADR-0001 D9). Every committed price is synthetic.

**Format and location.** Fixtures are **parquet in SPEC-001's cache layout**, so tests exercise the real cache read path (`MarketData(DataConfig(cache_dir=<copy>, offline=True))` works on them directly):
```
tests/fixtures/market/                      # clean universe
  manifest.json
  <source>/<SYMBOL_FS>/meta.json            # {"current_gen": 1, "tf": "1d", "schema": 1}
  <source>/<SYMBOL_FS>/gen-0001/bars.parquet
  <source>/<SYMBOL_FS>/gen-0001/gen.json    # {"created": "2025-01-01T00:00:00Z", "basis": "synthetic_seed_20180102",
                                            #  "first": "<date>", "last": "<date>", "sha256": "<sha256 of bars.parquet>"}
tests/fixtures/market_edge/                 # edge-case symbols, SAME layout, own manifest.json, never mixed into market/
```
- `<source>` is `yfinance` for the 17 ETFs and `coinbaseexchange` for crypto (SPEC-001's routing keys). `SYMBOL_FS` is `SPY` and `BTC_USD`, per SPEC-001.
- `bars.parquet`: index `ts` (tz-aware UTC, midnight, sorted, unique, bar-open-date label per ADR D1); columns `open, high, low, close, volume` (float64) plus the cache-internal `fetched_at` (constant `2025-01-01T00:00:00Z`) and `source_rev` (constant `synthetic-v1`). Snappy compression by default.
- `manifest.json` (one in `market/`, one in `market_edge/`): `{"schema": 1, "generator": "scripts/make_fixtures.py", "generator_version": 1, "seed": 20180102, "synthetic": true, "files": {"<path relative to the fixture root>": {"sha256", "bytes", "rows", "first", "last"}}, "total_bytes": int}`. `gen.json`'s `sha256` equals the sha256 of that symbol's `bars.parquet`.

**Coverage of the clean universe (`market/`).**
- *Symbols (20):* the 17 ETFs of docs/STRATEGY.md §3 (SPY, QQQ, IWM, EFA, EEM, TLT, IEF, GLD, XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLU, XLB) plus BTC/USD, ETH/USD, SOL/USD.
- *Dates:* 2018-01-02 to 2024-12-31 inclusive. **SOL/USD starts 2020-04-10** (the late-listing case, 1,727 rows); every other symbol starts 2018-01-02.
- *Calendars:* ETFs have exactly the real XNYS sessions from `exchange_calendars` (1,761 rows each; weekends, holidays such as 2019-07-04 and 2018-12-05, the national day of mourning, are absent). Crypto has every calendar day (2,556 rows for BTC and ETH). `exchange_calendars` is offline, so generation needs no network.
- *Regimes the paths must exercise:* a COVID-style crash (2020-02-19 to 2020-03-23) and rebound, the 2022 bear (2022-01-03 to 2022-10-12) in which bonds fall with stocks, a crypto rally (2020-10-01 to 2021-04-14) and a crypto winter in 2022. These make the RSI, 200-day SMA, stop and kill-switch paths fire.
- *Edge cases (`market_edge/`, equity layout, source `yfinance`)* are kept out of the clean universe so that no strategy test can pick them up by accident:

| Symbol | Rows | Construction | Defect or case |
|---|---|---|---|
| `ZZNEW` | 2021-03-01 to 2024-12-31 | clone params, P0 = 50 | late listing (survivorship) |
| `ZZGONE` | 2018-01-02 to 2022-06-30 | clone params, P0 = 40 | delisting: last bar 2022-06-30 |
| `ZZGAP` | 2018-01-02 to 2019-12-31, minus the 6 sessions 2019-07-08 .. 2019-07-12 and 2019-07-15 | clone | missing sessions (gap) |
| `ZZDUP` | 2018-01-02 to 2019-12-31, plus a second row for 2019-03-12 whose close is the original x 1.01 (high raised if needed) | clone | duplicate timestamp |
| `ZZTZ` | 2018-01-02 to 2019-12-31 | clone, index converted with `tz_convert('America/New_York')` | non-UTC index |
| `ZZBAD` | 2018-01-02 to 2019-12-31, with 2019-05-15 high and low swapped (high < low) and 2019-05-16 close = 0.0 | clone | invalid OHLC and zero price |
| `ZZFLAT` | first 300 XNYS sessions | open = high = low = close = 100.0, volume 1,000,000 | RSI 0/0 case |
| `ZZUP` | first 300 XNYS sessions | close_t = 100 + 0.5 t (t = 0..299), open_t = close_{t-1} (open_0 = 99.75), high = close, low = open, volume 1,000,000 | RSI = 100 case |
"Clone" means the generator's last parameter row (beta 1.0, idio 0.004, no other loadings, alpha 0, base volume 1.0e6), P0 = 100 unless the table says otherwise, bars on the real XNYS sessions of the stated date range, with rows removed or added after generation so prices stay continuous. Edge files are written straight with pandas/pyarrow, **bypassing `validate_bars`**, so the defects survive on disk; the tests under test (SPEC-001 validators) must flag them. A short 2019 holiday-week test needs no edge file: it uses the real XNYS gaps in the clean ETFs.

**Budget.** Whole `tests/fixtures/` <= **2.5 MB (2,500,000 bytes)**, each parquet file <= 200 KB. (A reference build of the clean universe measured 1.92 MB with snappy, and the edge set adds about 0.3 MB.) If a rebuild exceeds the budget, the permitted levers are `compression="zstd"` or fewer edge rows, never fewer symbols or dates. `tests/test_fixtures_manifest.py` enforces the budget, the sha256s, row counts, calendars, OHLC sanity and the SOL start date, so nobody edits a fixture silently.

**Generator: `scripts/make_fixtures.py`.** Pure, deterministic, **no network** (it may run anywhere; CI never runs it). Usage: `uv run python scripts/make_fixtures.py --write` rebuilds both fixture trees and manifests; `--check` regenerates into a temporary directory and compares the DataFrames to the committed files (exact for the index and volume, `rtol=1e-9` for prices) and exits non-zero on mismatch. `--check` is for local use after a numpy/pandas upgrade and is **not** part of CI: **the committed files are the fixtures**, so CI never regenerates, and a different numpy version cannot change a test input. Regenerating the committed files still requires the PM-approved note of section 5.

*Randomness.* `SEED = 20180102`. The master stream is `rng = numpy.random.Generator(numpy.random.PCG64(SEED))` (pin `PCG64` explicitly, never `default_rng`'s implicit choice). Draws, in this order and no other: (1) `u = rng.random(N)`; (2) `Z = rng.standard_normal((N, 5)) @ chol(CORR).T`, where `N = 2556` is the number of calendar days 2018-01-02..2024-12-31 and `chol` is `numpy.linalg.cholesky`. Symbol `k` (0-based index in the order: the 17 ETFs as listed above, then BTC/USD, ETH/USD, SOL/USD; edge clones use `k = 100 + j` with j = 0..5 for ZZNEW, ZZGONE, ZZGAP, ZZDUP, ZZTZ, ZZBAD; ZZFLAT and ZZUP are fully deterministic and draw nothing) has its own stream `r = Generator(PCG64(SeedSequence([SEED, k])))`, drawn in this order: `eps = r.standard_normal(N)`, `ae = r.standard_normal(N)`, then `u1, u2, u3, w`, each `r.standard_normal(n_bars)`. All return arrays are generated over all N calendar days and then sliced to the symbol's bar dates, so a symbol's start date never shifts its draws.

*Regime chain (market factor M).* Per calendar day a regime label comes from this rule. Forced windows (inclusive): CRASH 2020-02-19..2020-03-23, REBOUND 2020-03-24..2020-05-29, BEAR 2022-01-03..2022-10-12. Outside the windows a two-state Markov chain over {BULL, CHOP} starts in BULL, moves with `if u[i] >= stay[s]: flip s`, where stay(BULL) = 0.985 and stay(CHOP) = 0.95, and is reset to BULL on every forced day. (`u[i]` is consumed every day.) Daily market return `M_i = mu[reg_i] + sigma[reg_i] * Z[i,0]`:

| Regime | mu | sigma |
|---|---|---|
| BULL | +0.0006 | 0.0075 |
| CHOP | 0.0000 | 0.0110 |
| CRASH | -0.0175 | 0.0300 |
| REBOUND | +0.0040 | 0.0200 |
| BEAR | -0.0009 | 0.0140 |

*Other factors.* `CORR` over (M, G, R, E, C) = rows `[1,.3,-.2,.3,.35]`, `[.3,1,-.1,0,.25]`, `[-.2,-.1,1,-.1,0]`, `[.3,0,-.1,1,.1]`, `[.35,.25,0,.1,1]` (positive definite; min eigenvalue 0.54). G (growth vs value), R (rates, bond price), E (energy and commodities), C (crypto beyond the market): `G_i = 0.006 Z[i,1]`; `R_i = dR[reg_i] + 0.0045 Z[i,2]` with dR = +0.0005 in CRASH, -0.0007 in BEAR, else 0; `E_i = 0.012 Z[i,3]`; `C_i = dC[reg_i] + 0.025 Z[i,4]` with dC = BULL +0.0001, CRASH -0.008, REBOUND +0.005, BEAR -0.002, CHOP 0, and dC overridden to +0.0035 on 2020-10-01..2021-04-14.

*Symbol return (log return, per calendar day).* `ret = alpha + a + beta*M + gG*G + gR*R + gE*E + gC*C + idio*eps`, where `a` is a persistent drift that creates momentum: `a_0 = 0`, `a_t = 0.99 a_{t-1} + sa*ae[t]`, `sa = 6e-5 * clip(idio/0.004, 0.3, 1.5)`. `alpha = alpha_ann/252` for ETFs; crypto alpha is 0. Equities take the return of the calendar date of each XNYS session (weekend factor moves are simply not applied to them); crypto uses every day. Unrounded close: `close_t = P0 * exp(cumsum(ret over the symbol's bar dates))`, so the first bar's close is `P0*exp(ret_first)`.

| Symbol | beta | gG | gR | gE | gC | alpha_ann | idio (daily) | P0 | base volume |
|---|---|---|---|---|---|---|---|---|---|
| SPY | 1.00 | 0 | 0 | 0 | 0 | 0.03 | 0.0015 | 270 | 8.0e7 |
| QQQ | 1.10 | 0.8 | 0 | 0 | 0 | 0.04 | 0.0030 | 160 | 4.0e7 |
| IWM | 1.15 | -0.2 | 0 | 0 | 0 | 0 | 0.0050 | 150 | 2.5e7 |
| EFA | 0.85 | 0 | 0 | 0 | 0 | 0 | 0.0040 | 70 | 2.0e7 |
| EEM | 0.95 | 0 | 0 | 0.1 | 0 | 0 | 0.0065 | 47 | 3.5e7 |
| TLT | -0.15 | 0 | 1.00 | 0 | 0 | 0 | 0.0020 | 125 | 1.0e7 |
| IEF | -0.05 | 0 | 0.45 | 0 | 0 | 0 | 0.0010 | 105 | 5.0e6 |
| GLD | 0.05 | 0 | 0.20 | 0.1 | 0 | 0.02 | 0.0050 | 125 | 8.0e6 |
| XLK | 1.05 | 0.9 | 0 | 0 | 0 | 0.03 | 0.0040 | 65 | 1.0e7 |
| XLF | 1.10 | 0 | -0.30 | 0 | 0 | 0 | 0.0045 | 28 | 4.0e7 |
| XLE | 0.90 | 0 | 0 | 1.0 | 0 | 0 | 0.0050 | 70 | 1.5e7 |
| XLV | 0.75 | 0 | 0 | 0 | 0 | 0.02 | 0.0040 | 85 | 8.0e6 |
| XLI | 1.00 | 0 | 0 | 0 | 0 | 0.01 | 0.0035 | 72 | 1.0e7 |
| XLY | 1.00 | 0.4 | 0 | 0 | 0 | 0.02 | 0.0040 | 105 | 5.0e6 |
| XLP | 0.55 | 0 | 0 | 0 | 0 | 0.01 | 0.0035 | 55 | 1.2e7 |
| XLU | 0.45 | 0 | 0.35 | 0 | 0 | 0.01 | 0.0050 | 52 | 9.0e6 |
| XLB | 0.95 | 0 | 0 | 0.3 | 0 | 0 | 0.0040 | 56 | 5.0e6 |
| BTC/USD | 0.60 | 0 | 0 | 0 | 1.00 | 0 | 0.0150 | 13500 | 3.0e4 |
| ETH/USD | 0.80 | 0 | 0 | 0 | 1.15 | 0 | 0.0200 | 750 | 3.0e5 |
| SOL/USD | 0.90 | 0 | 0 | 0 | 1.30 | 0 | 0.0300 | 0.5 | 5.0e6 |
| edge clone | 1.00 | 0 | 0 | 0 | 0 | 0 | 0.0040 | per table | 1.0e6 |

*OHLC and volume from closes.* Let `s` = the sample standard deviation (ddof=1) of the symbol's own log returns over its bar dates, `prev_t` = the previous unrounded close (`P0` for the first bar), `r_t` that bar's log return, and `n_bars` its row count.
- `open_t = prev_t * exp(0.3 r_t + 0.15 s u1_t)`
- `high_raw_t = max(open_t, close_t) * exp(|0.6 s u2_t|)`; `low_raw_t = min(open_t, close_t) * exp(-|0.6 s u3_t|)`
- Round open, high_raw, low_raw and close to **4 decimals** (`numpy.round`), then set `high = max(high_r, open_r, close_r)` and `low = min(low_r, open_r, close_r)`. This guarantees `low <= open, close <= high` after rounding. The generator asserts it, and asserts every price >= 0.01.
- `volume_t = max(1, round(base_volume * exp(0.2 w_t + 8 |r_t|)))`, stored as float64.

*Generator acceptance bands (asserted by the generator and by `test_fixtures_manifest.py`).* SPY max drawdown inside the crash window is 25-45% (reference build: 28.2%) and inside the bear window 20-35% (24.3%); BTC close(last)/P0 is in [3, 15] (6.8x) with max drawdown >= 60% (80%); ETH ratio in [2, 15] (6.5x); SOL ratio in [5, 100] (29.7x); of the 17 ETFs, the number above its own 200-day SMA varies between <= 4 and >= 15 across the run (reference: 0 and 17); the top-3 trailing-6-month-return set among the ETFs, sampled at month ends, changes in >= 50% of consecutive months (reference: 70%), so momentum ranking is non-trivial. Anchors from the reference build to confirm the draw order is right (tolerance 1e-3 relative): SPY first bar (open, high, low, close) = (271.8521, 277.9405, 268.1065, 274.3097), SPY last close 444.458; BTC/USD first close 14,476.3872, last close 91,316.3118; SOL/USD first close 0.5298 on 2020-04-10, last close 14.8407; XLE last close 90.2088. If an anchor misses, fix the draw order, never the anchor.

**Enforcement of "no network".** Three layers, all in `tests/conftest.py`:
1. An `autouse=True`, session-scoped fixture patches `socket.socket.connect`, `socket.socket.connect_ex`, `socket.create_connection` and `socket.getaddrinfo` to raise `NetworkAccessError` (an `AssertionError` subclass) naming the test (`PYTEST_CURRENT_TEST`). AF_UNIX and `socketpair()` stay allowed (asyncio and FastAPI `TestClient` need them). No test may opt out; there is no `network` marker.
2. In the same fixture, if `yfinance` or `ccxt` import, `yfinance.download`, `yfinance.Ticker` and `ccxt.Exchange.fetch` (the base HTTP method every exchange class uses) are replaced with functions that raise `NetworkAccessError`, so an un-mocked adapter fails with a clear message even if it catches the socket error.
3. `tests/test_offline_guard.py` is the **meta-test that the guard works** (a connect to 127.0.0.1:9, a DNS lookup of `example.com` and `yfinance.download` all raise; AF_UNIX `socketpair()` still works). CI is Ubuntu only; an extra network-less step (`unshare -n`) is optional and is platform-lead's call. Layers 1-3 are mandatory.

**Time.** Code under test takes an injected `Clock` (`tradebot/clock.py`: `Clock` protocol, `SystemClock`, `FixedClock`) and never calls `datetime.now()` directly; tests use `FixedClock`. A grep-based test (`tests/test_no_wallclock.py`) fails if `datetime.now(`, `datetime.utcnow(`, `time.time(` or `date.today(` appears under `src/tradebot/` outside `clock.py`. `freezegun` is not used.

### 3. Lookahead test design: truncation invariance
Claim: for every indicator and strategy, the output at bar *t* computed from `data[:t+1]` equals the output at *t* computed from the full data. Two checks run per target, because they catch different bugs.

```python
# tests/lookahead/helpers.py
def assert_truncation_invariant(fn, bars, ts_indices, rtol=0.0):
    """fn: bars -> Series/DataFrame aligned to bars.index (indicator or signal)."""
    full = fn(bars)
    for t in ts_indices:                       # t = last visible bar position
        prefix = fn(bars.iloc[: t + 1])        # future bars physically absent
        assert_frame_equal(prefix.iloc[-1:], full.iloc[t : t + 1],
                           check_exact=(rtol == 0.0), rtol=rtol, check_names=False)
        # NaN == NaN counts as equal; a value-vs-NaN mismatch is a failure

def assert_future_perturbation_invariant(fn, bars, ts_indices, rng):
    """Stronger: future bars present but poisoned. Catches centered windows,
    backward fill, whole-series normalization, shift(-1)."""
    base = fn(bars)
    for t in ts_indices:
        poisoned = bars.copy()
        poisoned.iloc[t + 1 :] = rng.uniform(0.01, 1e6, size=poisoned.iloc[t + 1 :].shape)
        # keep OHLC valid so validators do not mask the leak
        poisoned = repair_ohlc(poisoned)
        assert_frame_equal(fn(poisoned).iloc[t : t + 1], base.iloc[t : t + 1], check_exact=True)
```
- **Which `t`:** all `t` for short series (≤ 300 bars, synthetic); for the 1,761-bar ETF fixtures use all `t ≥ warmup - 2` with stride 7, plus the first 3 bars, the last 3 bars, and the warmup boundary exactly (warmup-1, warmup, warmup+1). Indicator tests cost O(n²/stride); budget ≤ 5 s total.
- **Which targets, with completeness enforced:** parametrize from the *indicator registry* (SPEC-002 must expose `INDICATORS: dict[str, Callable]`). A meta-test asserts every registry entry appears in the truncation test's parameter list, so adding an indicator without a lookahead test fails CI. The same applies to a `STRATEGIES`/sleeve registry for signal-level checks (the three STRATEGY.md §4 sleeves plus every template).
- **Exactness:** compare exactly (`rtol=0`). An algorithm that runs from index 0 forward does identical float operations on a prefix, so bit-equality is achievable (pandas `rolling`/`ewm` qualify). If a vectorized implementation cannot meet that, the owner may set `rtol=1e-12` for that indicator with a one-line justification in the test; anything looser needs quality-lead sign-off.
- **Backtest-level prefix invariance** (`test_backtest_prefix.py`): run the golden configs on `bars[:T]` and on all bars. Every fill dated ≤ the last bar of the short run, plus the equity values on those dates, must be identical. The short run's final-bar signal must produce no fill (nothing exists at t+1).
- **Monthly/weekly signals:** rebalance-date logic must also use only past data (month-end detection must not peek at the next bar to learn "this is the last trading day"; the fixture-based test runs the truncation check across month boundaries and the 2024-12 year end, where the next bar is absent).
- **Canary (the test must be able to fail):** `test_lookahead_detector_catches_leaks.py` feeds the checkers deliberately leaky functions (`close.shift(-1)`, `close.rolling(5, center=True).mean()`, `close / close.max()`, `close.bfill()`) and asserts each raises `AssertionError`. A detector that cannot catch these is itself broken.

### 4. Accounting invariants as property tests
All use an **independent reference ledger** (`tests/support/oracle.py`, about 60 lines): it replays the list of fills with plain `Decimal` arithmetic (`cash -= qty*price + fee` for buys, and so on) and shares no code with `src/tradebot/portfolio`. The system's reported cash, positions and equity are compared against the oracle, so a bug cannot hide by being mirrored in both places. Shared Hypothesis strategies live in `tests/support/strategies.py`:
```python
@st.composite
def bars_df(draw, n=st.integers(5, 120), symbols=...):   # valid OHLC: low<=min(o,c)<=max(o,c)<=high, price in [0.01, 1e5], 2-4 dp
@st.composite
def order_plan(draw, bars):   # list of (signal_bar_idx, symbol, target_weight in [0,1]) incl. zero and tiny weights
```
Sketches (names illustrative; run both stock and crypto cost schedules via `@given(market=st.sampled_from(["stock","crypto"]))`):

```python
# I1 equity identity, every bar, exact Decimal
@given(bars=bars_df(), plan=order_plan())
def test_cash_plus_positions_equals_equity(bars, plan):
    res = run_sim(bars, plan, start_cash=D("100000"))
    ref = oracle_replay(res.fills, start_cash=D("100000"))
    for snap in res.snapshots:                      # one per bar
        mv = sum(q * bars.close_at(sym, snap.ts) for sym, q in snap.positions.items())  # mv = q2 HALF_EVEN per SPEC-003 section 2
        assert snap.cash + mv == snap.equity        # exact; mark-to-market rounding rule from SPEC-003
        assert (snap.cash, snap.positions) == ref.state_at(snap.ts)

# I2 cash never negative, quantities never negative (no leverage / no shorting in v1)
    assert all(s.cash >= 0 for s in res.snapshots)
    assert all(q >= 0 for s in res.snapshots for q in s.positions.values())
# Orders that would overdraw must appear as REJECTED/shrunk in the order log, never as a fill.

# I3 sleeves sum to portfolio (includes the 10% cash-buffer sleeve), every bar, exact
    assert sum(sl.equity for sl in snap.sleeves) == snap.portfolio_equity
    assert sum(sl.cash for sl in snap.sleeves) == snap.cash       # no unattributed cash

# I4 fees >= 0 and costs adverse and applied both directions
    for f in res.fills:
        assert f.fee >= 0 and f.qty > 0
        raw = bars.open_at(f.symbol, f.fill_ts)
        assert (f.price >= raw) if f.side == BUY else (f.price <= raw)
        assert f.price == expected_price(raw, f.side, f.market)        # buy ceil8(open*(1+bps)), sell floor8(open*(1-bps)); SPEC-003 section 2
        assert f.fee == expected_fee(f.qty * f.price, f.market)        # q2 HALF_UP of notional*bps; ETF 0, crypto 10 bps
# at least one BUY and one SELL must occur in a sampled run (use hypothesis.assume / target())

# I5 no fill before signal bar + 1, and fills are exactly at t+1's open
    for f in res.fills:
        assert f.fill_idx == f.signal_idx + 1               # v1: no multi-bar working orders
        assert f.raw_open == bars.open_at(f.symbol, bars.ts[f.signal_idx + 1])
    assert not any(f.fill_idx >= len(bars) for f in res.fills)   # last-bar signal => no fill

# I6 cash conservation and append-only ledger
    assert final.cash == start - sum(side_sign*f.qty*f.price + f.fee for f in fills)
    # ledger prefix stable: running with bars[:k] yields a prefix of the full-run fills

# I7 risk limits (SPEC-003 decides whether checked at order time or every bar; test the stated rule)
    # after any BUY fill: asset weight <= 25%, total crypto weight <= 30%, using fill-time prices
    # no order may *increase* exposure of an already-breached limit

# I8 kill switch: once drawdown from peak equity >= 20%, no BUY fills until the reset event
@given(...path with forced crash...)
def test_no_entries_after_kill_switch(...)

# I9 determinism: same bars + same plan => identical ledger hash, run twice in one process
```
Additional canaries (`tests/property/test_invariants_catch_bugs.py`): monkeypatch the SimBroker with seeded bugs (fill at `close[t]`, fee sign flipped, no slippage on sells, rounding that drops a cent, sleeve cash leaked to portfolio only) and assert at least one invariant test fails for each. This proves the invariants have teeth.

### 5. Golden backtests
Four pinned runs on the §2 synthetic fixtures (`tests/fixtures/market/`, read through the real cache path), start 2018-01-02, end 2024-12-31, initial cash `Decimal("100000")`, costs per STRATEGY.md §6:

| Id | Strategy | Notes |
|---|---|---|
| G1 | Mean-reversion sleeve alone (SPY, QQQ; RSI(2)<10 and price>200-day SMA; exit RSI(2)>70 or 5 days; ATR(14) 2.5x trailing stop) | exercises exits, stop, many trades |
| G2 | Core ETF momentum alone (monthly, top 3 by 6-month return above 200-day SMA, IEF fallback) | exercises rebalance and ranking |
| G3 | Crypto trend alone (BTC, ETH, SOL; 50-day SMA / 20>50 rule) | exercises 10+10 bps costs, fractional qty, late-listed SOL, 365 annualization |
| G4 | Full paper fund, 50/20/20/10 with monthly rebalance | exercises sleeves, caps, kill-switch path |

**Pinned per golden** (`tests/golden/G1.json` etc., with `schema_version`):
- *Exact:* final equity, final cash, per-symbol final quantities (all `Decimal`, stored as strings); fill count; first 5 and last 5 fills in full (symbol, side, qty, price, fee, signal_ts, fill_ts); `ledger_sha256` (hash of the canonical, sorted, string-serialized fill list); number of signals.
- *Float metrics, abs tolerance 1e-9:* total return, CAGR, max drawdown, Sharpe, Sortino, win rate, exposure, plus the same set for the matching buy-and-hold benchmark (SPY and/or BTC) over the same window. The golden must include the benchmark block, so a report without a benchmark fails.
- Dependencies are pinned via `uv.lock`; if a numpy/pandas upgrade shifts a float beyond 1e-9, that is a real finding to investigate, not a reason to widen the tolerance.

**Harness** (`tests/golden/harness.py`): runs the config, builds the same dict shape, and on mismatch prints a field-by-field diff (expected vs actual, which fills first diverge). It never rewrites silently.

**Sensitivity meta-tests** (so a golden can't pass by accident): re-running G4 with slippage +1 bp must change final equity; with the cost schedule set to zero it must increase it; running with the signal shifted one bar earlier (a deliberately lookahead variant, test-only flag) must produce a *different* ledger hash.

**Intentional update procedure.**
1. The engineer proposes the change in a PR/branch and runs `uv run pytest tests/golden --update-golden --golden-note=docs/reviews/golden/<date>-<id>.md`. Without both the flag and an existing note file, the harness refuses to write.
2. The note (template below) states *why* values changed, which commit caused it, a before/after table, and quality-lead's reasonableness check (for example, hand-recomputing one changed trade). It contains a line `Approved-by: PM <date>`.
3. The note records `golden-sha256:` of each changed JSON. `tests/golden/test_golden_change_note.py` fails if any golden file's current hash is not listed in a note carrying `Approved-by: PM`. A golden edited by hand, or without approval, therefore fails CI.
4. Changes to fixtures (§2) follow the same procedure for all four goldens.
Note template:
```
# Golden change <date> <ids>
Cause: <commit/spec task>      Intended behavior change: <one paragraph, plain language>
| field | before | after | explanation |
Independent check: <hand recomputation of one affected trade or metric>
golden-sha256: G1=<hash> G4=<hash>
Approved-by: PM <date>
```

### 6. Hand-calculated reference cases
These are literal expected values the tests assert. Test authors must copy the arithmetic into comments. Values come from this spec, not from running the code. All money cases use `Decimal` and **SPEC-003 §2's rounding table**, which is the single source of truth:

| Quantity | Quantum | Rounding |
|---|---|---|
| Buy fill price `ceil8(open x (1 + slip_bps/10000))` | 1e-8 | CEILING |
| Sell fill price `floor8(open x (1 - slip_bps/10000))` | 1e-8 | FLOOR |
| Input price (`to_price`) | 1e-8 | HALF_EVEN |
| Notional and market value `q2(qty x price)` | 0.01 | HALF_EVEN |
| Fee `q2(notional x fee_bps/10000)` | 0.01 | HALF_UP |
| Equity quantity (buy sizing) | 1 share | DOWN (sells exact) |
| Crypto quantity (buy sizing) | 1e-8 | DOWN (sells exact) |

Cost schedule: ETF fee 0 bps, slippage 5 bps; crypto fee 10 bps, slippage 10 bps. Buy cash out = notional + fee; sell cash in = notional - fee.

**6.1 SMA(3).** Closes `[10, 12, 14, 16, 12]` give `[NaN, NaN, 12, 14, 14]`.
Check: (10+12+14)/3=12; (12+14+16)/3=14; (14+16+12)/3=14. Also assert the first `n-1=2` values are NaN (not partial means).

**6.2 RSI(2), Wilder smoothing (alpha=1/2), as defined in SPEC-002.** Closes `[100, 102, 101, 99, 98]`, so changes are `+2, -1, -2, -1`.
- Seed at index 2 (first two changes +2, -1): avg gain = (2+0)/2 = 1; avg loss = (0+1)/2 = 0.5; RS = 2; **RSI = 100 - 100/3 = 66.6667**.
- Index 3 (change -2): avg gain = (1x1+0)/2 = 0.5; avg loss = (0.5x1+2)/2 = 1.25; RS = 0.4; **RSI = 100 - 100/1.4 = 28.5714**.
- Index 4 (change -1): avg gain = (0.5+0)/2 = 0.25; avg loss = (1.25+1)/2 = 1.125; RS = 2/9; **RSI = 100 - 100/(11/9) = 18.1818**.
Expected series `[NaN, NaN, 66.6667, 28.5714, 18.1818]`, abs tol 1e-4. Edge cases (SPEC-002's convention: avg loss 0 gives 100 if avg gain > 0, else 50): the `ZZUP` fixture gives RSI 100 and the `ZZFLAT` fixture gives 50, with no exception and no inf.

**6.3 ETF round trip (fee 0, slippage 5 bps, whole shares).** Cash 100,000.00. Signal on bar t; bar t+1 open = 100.00; buy 100 shares.
- Buy fill price = ceil8(100.00 x 1.0005) = ceil8(100.05000000) = **100.05000000** (exact, so CEILING changes nothing). Notional = q2(100 x 100.05) = **10,005.00**. Fee = q2_half_up(10,005.00 x 0/10000) = 0.00. Cash after = 100,000.00 - 10,005.00 = **89,995.00**.
- Later signal; bar open = 110.00; sell 100 shares. Sell fill price = floor8(110.00 x 0.9995) = floor8(109.94500000) = **109.94500000** (exact at 8 dp). Notional = q2(100 x 109.945) = q2(10,994.500) = **10,994.50**. Fee 0.00. Cash after = 89,995.00 + 10,994.50 = **100,989.50**.
- Net P&L = 100,989.50 - 100,000.00 = **+989.50**. Positions empty; equity = cash = 100,989.50. Prices are kept to 8 decimals (not cents), so there is no 109.94 / 109.95 ambiguity; assert the 8-dp price.
- Bar-end equity while holding (close 105.00): market value q2(100 x 105.00) = 10,500.00; equity = 89,995.00 + 10,500.00 = **100,495.00**.

**6.4 Crypto round trip (fee 10 bps, slippage 10 bps, fractional qty).** Cash 100,000.00, buy 0.5 BTC at next-bar open 20,000.
- Buy fill price = ceil8(20,000 x 1.001) = **20,020.00000000**. Notional = q2(0.5 x 20,020) = **10,010.00**. Fee = q2_half_up(10,010.00 x 0.001) = q2_half_up(10.010) = **10.01**. Cash out = 10,010.00 + 10.01 = 10,020.01; cash after = 100,000.00 - 10,020.01 = **89,979.99**.
- Sell 0.5 BTC at open 22,000: price = floor8(22,000 x 0.999) = **21,978.00000000**. Notional = q2(0.5 x 21,978) = **10,989.00**. Fee = q2_half_up(10,989.00 x 0.001) = q2_half_up(10.989) = **10.99** (HALF_UP and HALF_EVEN agree here). Cash in = 10,989.00 - 10.99 = 10,978.01; cash after = 89,979.99 + 10,978.01 = **100,958.00**.
- Net P&L = **+958.00**; total fees = 10.01 + 10.99 = **21.00**. Assert both legs have fee > 0 and slippage in the adverse direction. (Round-trip check: 20,000 -> 22,000 gross 1,000.00, less 10.00 + 11.00 slippage, less 21.00 fees = 958.00.)

**6.5 Rounding-edge cases (each one fails under a wrong rounding mode).**
- *Fill price direction.* ETF open 10.00000001 (= `to_price` of the float 10.00000001, already at 8 dp). Buy: 10.00000001 x 1.0005 = 10.005000010005, **CEILING -> 10.00500002** (HALF_EVEN would give 10.00500001). Sell: 10.00000001 x 0.9995 = 9.995000009995, **FLOOR -> 9.99500000** (HALF_EVEN would give 9.99500001). So buys never get a better price and sells never get a better price than the exact one.
- *Notional is HALF_EVEN at cents.* Buy 10 shares at 10.00500002: 100.0500002 -> **100.05**; sell 10 at 9.995: 99.95000000 -> **99.95**. Ties: `q2(5 x 20.001)` = 100.005 -> **100.00** (HALF_EVEN; HALF_UP would give 100.01); `q2(7 x 14.275)` = 99.925 -> **99.92** (HALF_UP would give 99.93).
- *Fee is HALF_UP.* Crypto fee on notional 5.00 at 10 bps: 0.005 -> **0.01** (HALF_EVEN would give 0.00). On 25.00: 0.025 -> **0.03** (HALF_EVEN 0.02). On 15.00: 0.015 -> 0.02 (both agree).
- *Whole-share sizing rounds DOWN.* Target value 10,000.00 at ref close 33.33: 10,000 / 33.33 = 300.0300... -> **300 shares** (not 301, not 300.03). Filled at open 33.40: price = ceil8(33.40 x 1.0005) = 33.41670000; notional = q2(300 x 33.4167) = **10,025.01**; fee 0; cash out 10,025.01 (the fill may exceed the target value slightly because sizing uses the ref close).
- *Crypto sizing rounds DOWN at 1e-8.* Target value 10,000.00 at ref close 30,000.12345678: 10,000 / 30,000.12345678 = 0.33333196159... -> **0.33333196 BTC** (HALF_EVEN would also give ...196 here; the case pins the quantum and DOWN). Filled at open 30,000.12345678: price = ceil8(30,000.12345678 x 1.001) = ceil8(30,030.12358023678) = **30,030.12358024**; notional = q2(0.33333196 x 30,030.12358024) = q2(10,009.99995204...) = **10,010.00** (rounds up to cents, HALF_EVEN); fee = q2_half_up(10,010.00 x 0.001) = **10.01**; cash out = **10,020.01**.

**6.6 Others (haiku, workers compute the arithmetic in-test comments).**
- Metrics: max drawdown for equity `[100, 120, 90, 110]` is (90-120)/120 = **-25.0%**; total return 10%; CAGR from known day counts with P=252 (equity-only) vs P=365 (any run containing crypto), per ADR D6.
- Rebalance: 100,000 portfolio, 50/20/20/10 targets -> sleeve dollar targets 50,000.00 / 20,000.00 / 20,000.00 / 10,000.00.
- Cap: a BUY that would take one asset above 25% of equity is shrunk so post-trade weight is exactly <= 25%.
- Trailing stop (ADR D5: close-based, trigger `close <= stop_level`): highest close since entry 120, ATR(14) = 2 -> stop level 120 - 2.5 x 2 = **115**. Stop level never decreases: if the next ATR is 3 and the high-water close is still 120, candidate = 120 - 7.5 = 112.5 < 115, so the level stays 115. A close at **115.00 triggers** a STOP-origin sell that fills at the *next* open; a close at 115.01 does not; 114.99 does.
- Kill switch: peak equity 100,000; equity 80,000 -> drawdown 0.20 -> triggered (>= 20%); equity 80,001 -> drawdown 0.19999 -> not triggered.

### 7. Validation-gate tests (STRATEGY.md §7)
The gate is a pure function owned by SPEC-002: `validate(report: BacktestBundle) -> GateReport` with one named pass/fail per criterion (history length, holdout discipline, walk-forward consistency, OOS Sharpe/Sortino vs benchmark and drawdown <= 1.0x benchmark, >= 30 trades, +/-20% robustness). The paper-trading criterion (6) is a time check recorded by the runner and is covered by a unit test that the gate stays "pending" until 3 months of forward data exist.

All gate tests use **synthetic, seeded data** from `tests/support/synthetic.py` (12 years of daily bars for stocks, about 3,000 rows; independent of the committed fixtures (which span only 7 years), so a fixture regeneration cannot change them).

**Negative test: the "memorizer".**
1. Generate a zero-drift random-walk asset (seed fixed, noted in the test) plus its buy-and-hold benchmark.
2. Define a test-only overfit strategy: a lookup of dates on which the *next* bar's return was positive, restricted to the in-sample 70%; it holds on those days and is flat otherwise, with out-of-sample behavior continuing the same rule from a fixed pseudo-random schedule. It has many free parameters (the dates), so it is what an over-tuned grid search looks like.
3. Add a second path: a grid-search tuner over (fast SMA, slow SMA, RSI threshold) on the random walk, picking the best in-sample Sharpe.
4. **Precondition asserts (guard against a vacuous test):** in-sample Sharpe of the memorizer exceeds the benchmark by a wide margin (> 2.0), trades >= 30, history long enough. If the precondition fails the test fails with "fixture no longer overfit", so the test can't pass because the strategy was never good in-sample.
5. Assert `validate(...).passed is False` and specifically that criteria 3 (OOS Sharpe/Sortino vs benchmark) and 5 (+/-20% robustness) fail, and that the report names them.

**Positive control** (a gate that rejects everything is useless): a trend-following strategy on a synthetic series with a persistent trend and benchmark-beating drawdown control must pass criteria 1-5 with the same code path.

**Per-criterion negatives:** 9 years of stock history fails criterion 1; 4.5 years of crypto fails criterion 1; 29 trades fails criterion 4 while 30 passes; a strategy with OOS Sharpe equal to the benchmark passes (>=) and one just below fails; max drawdown 1.01x benchmark fails.

**Holdout discipline (criterion 1/2):** the tuner receives the data through a spy. Replace the last 30% with NaN/poison values; the tuner's chosen parameters and in-sample metrics must be identical to those on clean data, and the tuner must never receive an index past the split date. The split is by date, computed before tuning, and the holdout is touched exactly once (by the final OOS evaluation).

### 8. Review and audit process
**Quality-lead audit is mandatory** (verdict PASS / PASS-WITH-NOTES / FAIL before the PM merges) when a diff touches any of:
- fills, fees, slippage, the ledger, portfolio/sleeve accounting, sizing, risk limits, stops or the kill switch (`sim/`, `portfolio/`, `risk/`);
- indicator or signal code, rebalance-date logic, the backtest loop, or metric formulas (`strategy/`, `backtest/`);
- data adjustment, resampling, timestamp or timezone handling, calendars, universe membership (`data/`);
- the validation gate, golden values or golden harness, fixtures or their generator;
- runner idempotency, last-processed-bar tracking or transaction boundaries (`runner/`, DB schema for fills);
- any `Decimal`/rounding policy or money-type change;
- any test that is deleted, skipped, xfailed or has its assertions loosened in these areas.
It is also mandatory at the end of Phase 2 and Phase 3, and before any strategy is promoted to forward paper trading.

**Reviewer alone is enough** for: dashboard layout and API plumbing that only reads and does not compute numbers, config and logging, docs, renames with no behavior change, CI/tooling tweaks, new tests that only add coverage (the reviewer must check they assert something), dependency bumps without numeric-golden changes (if any golden changes, it escalates to an audit). When in doubt, audit; the PM decides by file path using this list.

**Audit report** (`docs/reviews/NNN-<spec>-audit.md`):
```
# Audit NNN: <spec / task / diff>
Verdict: PASS | PASS-WITH-NOTES | FAIL      Auditor: quality-lead      Date:
Scope: <commits/paths>     Evidence: <commands run + results>
## Checklist (Y / N / n.a. with file:line)
Lookahead (indicators use <= t; truncation test present) | Survivorship | Costs both directions |
Accounting invariants tested | Determinism / golden pinned | Decimal + explicit rounding |
Benchmark reported | Offline tests | Restart/idempotency (if runner)
## Findings (ranked)
| # | Severity (blocker/major/minor/nit) | file:line | Issue | Required fix |
## Test gaps
## Re-audit needed? <yes/no, trigger>
```
FAIL means: any blocker (wrong numbers, lookahead, missing costs, invariant violation, network in tests). The PM does not merge on FAIL.

**Definition of done for any task:** (1) every acceptance criterion in the task has a test that fails without the feature; (2) `uv run pytest` and `uv run ruff check .` are clean locally, with the `ci` Hypothesis profile for anything touching money/signals; (3) no new network, wall-clock or unseeded randomness; (4) money code uses `Decimal` with an explicit rounding mode; (5) new indicators/signals registered so the truncation tests pick them up; (6) coverage thresholds (§9) still met; (7) worker's completion report filed, reviewer's report received, audit report if §8 says mandatory; (8) the PM adds the process-log row and the roadmap tick.

### 9. CI gates (platform-lead owns the pipeline; these are the requirements)
1. `uv sync --locked` (lock file must be current).
2. `uv run ruff check .` and `uv run ruff format --check .` (platform-lead to add format config).
3. `uv run pytest --cov=tradebot --cov-branch --cov-report=json -p no:cacheprovider` with `HYPOTHESIS_PROFILE=ci`, `--strict-markers`, and `filterwarnings = ["error"]` (a pandas FutureWarning is a build failure; explicit allowlist entries need a comment).
4. **Coverage floors** (checked by `scripts/check_coverage.py` reading the JSON, because coverage.py supports only one global threshold):
   - `tradebot/sim`, `tradebot/portfolio`, `tradebot/risk`, backtest ledger/fill glue, and metrics: **>= 95% line and >= 90% branch**;
   - `tradebot/strategy` (indicators, signals) and `tradebot/data`: **>= 90% line**;
   - whole package: **>= 80% line**; `api/`, `dashboard/` have no floor in v1.
   Floors are ratchets: they only go up. Coverage is a floor, not the goal; invariants and goldens are what protect correctness.
5. **Runtime budget:** full suite <= **90 s** on CI; unit + property + lookahead <= 30 s; golden + integration <= 30 s; runner restart tests <= 20 s. Per-test `--timeout=20` (hard fail), tests over 2 s must carry `@pytest.mark.slow`; `--durations=10` is printed. If the budget is exceeded, reduce Hypothesis `max_examples` for the slowest test, never skip it.
6. Golden-change-note test (§5) and fixture-manifest test run in the default suite, so they gate the merge.
7. CI is Ubuntu only, Python 3.12 (ADR-0001 D12). There is no macOS leg.

### Hooks needed from each area (named by ADR-0001; the ADR wins on any naming conflict)
- **SPEC-001 data:** `MarketData(DataConfig(cache_dir=<copy of tests/fixtures/market>, offline=True))` and `FixtureProvider` reading the committed parquet through the real cache read path; `get_bars(..., end)` never returns bars after `end`; validators for gap/duplicate/non-UTC/OHLC-sanity; date-aware `universe.members(date)`; immutable `Bar` once cached (re-fetch with different values raises).
- **SPEC-002 strategy/backtest:** `INDICATORS` and `STRATEGIES` registries; indicators as pure functions of the visible bars; deterministic `run_backtest(...) -> BacktestResult` built on `BarDriver` (ADR D3), with `trades.py` pure functions of ledger and snapshots, exposing `signals`, `fills`, `snapshots`, `equity_curve`, `metrics`, `benchmark`; metrics as pure functions; `validate()` gate with per-criterion results; documented RSI convention; test-only flag to shift signals earlier (for the sensitivity meta-test).
- **SPEC-003 execution:** `Fill` ledger events carry `signal_ts`, `fill_ts`, `raw_open`, `price`, `qty`, `fee`, `side`, `market`, `sleeve_id`, `symbol`; SPEC-003 section 2's rounding table as the single source of truth (D7); snapshots per bar with cash, positions, sleeve breakdown; an order log with REJECTED/shrunk reasons; kill-switch state and reset event; the invariants list from the execution-lead return.
- **SPEC-004 platform:** injected `Clock`; one transaction per portfolio per bar containing ledger events, snapshot, `export_state()` checkpoint, `run_log` row (unique on portfolio and bar) and the `last_processed_bar_ts` advance (D8); `process_bar` with a stale `asof` raises `StaleBarError`; a fault-injection seam in the runner (`fault_hook(point)` with points `after_driver`, `before_commit`, `after_commit`, per SPEC-004) active only in tests; runner step function callable bar-by-bar without APScheduler; pytest/coverage/timeout config and CI as in §9.

### Runner restart and idempotency tests (layer detail)
All use a file-based SQLite DB in `tmp_path`, with a fresh engine created for "restart", a `FixedClock`, and the fixture provider.
1. `test_replay_same_bar_is_noop`: process bar d twice -> identical ledger, no extra fills, no duplicate equity row.
2. `test_crash_at_each_fault_point_then_restart`: for each fault point, raise, rebuild the runner, resume; the final ledger hash must equal the uninterrupted run's. After `after_driver` and `before_commit` the DB must hold *no* partial rows (no ledger event, snapshot, checkpoint, `run_log` row or marker advance); after `after_commit` all of them are present and a restart is a no-op (atomicity).
3. `test_catch_up_after_downtime`: stop for 3 trading days, restart -> missed bars processed in order, result equals the batch backtest over the same bars.
4. `test_runner_equals_backtest`: feed the G4 fixture bar-by-bar through the runner; the ledger hash and final equity equal golden G4 exactly. This enforces the PM rule that backtest and forward paths share one implementation.
5. `test_no_bar_on_holiday_or_weekend_for_stocks` (2024-07-04, a holiday, and 2024-07-06, a Saturday) and crypto processes weekends.
6. `test_restated_bar_raises`: the provider returns a different value for an already-stored bar -> an error/alert, no silent history change.
7. Optional `@slow` subprocess test using `os._exit` mid-run and then restarting.

## Acceptance criteria
1. Running `uv run pytest` makes zero network connections; the guard test proves a socket connect and a DNS lookup both fail.
2. `tests/fixtures/` is <= 2.5 MB (2,500,000 bytes), each parquet <= 200 KB, hashes match both `manifest.json` files, the clean universe has the 20 symbols, dates and calendars of section 2, and every generator anchor and band holds.
3. Every registered indicator and strategy has a passing truncation and future-perturbation test; the canary leak functions are detected.
4. Properties I1-I9 pass under the `ci` profile, and every seeded-bug canary is caught by at least one.
5. Hand-calculated cases in §6 are asserted with the exact numbers given.
6. G1-G4 pass with the stated tolerances; an unapproved edit to any golden file fails the change-note test.
7. The gate rejects the memorizer (failing criteria 3 and 5), accepts the positive control, and each per-criterion negative behaves as listed.
8. Runner tests 1-6 pass, including crash at each fault point and runner-equals-golden-G4.
9. CI enforces §9, including per-package coverage floors and the runtime budget.

## Tasks
Per CLAUDE.md routing, tags are `[sonnet]` or `[haiku]`. Each task carries its **dispatch wave** (ADR-0001 §3): Wave 1 is test infrastructure and the fixture generator plus committed fixtures; every other task lands in the wave of the code it tests. A test task is dispatched once that code has merged, and it depends on the code task named under "Depends on". Tasks in the same wave with disjoint file lists run in parallel. File lists are exact and do not overlap between tasks. `tradebot/clock.py` is a source file that this spec specifies and a worker writes (T1). Wave-1 tasks T1-T4 are self-contained: a worker needs no other spec and no network (`exchange_calendars` works offline).

**Dependencies (D11).** quality-lead requests no `pyproject.toml` edit of its own. Platform's single Wave-1 task (004 T1, "tooling, dependencies and CI") applies every dependency in one edit. T1-T4 need these from it: `numpy`, `pandas`, `pyarrow`, `exchange-calendars` (runtime) and `pytest`, `pytest-cov`, `pytest-timeout`, `hypothesis`, `ruff` (dev), plus the pytest config (`--strict-markers`, markers `unit, property, integration, golden, restart, slow`, `filterwarnings = ["error"]`). "004 T1" is therefore a dependency of T2-T4 below.

### T1 [haiku] Wave 1: `Clock` module
- **Files:** `src/tradebot/clock.py`, `tests/unit/test_clock.py`
- **Depends on:** none
- **Do:** create `tradebot/clock.py` with exactly this public API (stdlib only, fully typed, `from __future__ import annotations`):
  ```python
  @runtime_checkable
  class Clock(Protocol):
      def now(self) -> datetime: ...            # always tz-aware, UTC
      def __call__(self) -> datetime: ...       # same value, so a Clock can be passed where Callable[[], datetime] is expected

  class SystemClock:
      def now(self) -> datetime: ...            # datetime.now(timezone.utc)
      def __call__(self) -> datetime: ...

  class FixedClock:
      def __init__(self, start: datetime) -> None: ...   # ValueError if start is naive; stored converted to UTC
      def now(self) -> datetime: ...                     # returns the stored instant; does not move on its own
      def __call__(self) -> datetime: ...
      def set(self, when: datetime) -> None: ...         # ValueError if naive, or if earlier than the current instant
      def advance(self, delta: timedelta) -> None: ...   # ValueError if delta is negative
  ```
  This is the only file under `src/tradebot/` allowed to call `datetime.now(`. Unit tests (`@pytest.mark.unit`): `SystemClock().now()` is tz-aware with `utcoffset() == timedelta(0)` and lies within 5 seconds of `datetime.now(timezone.utc)`; `FixedClock` returns the same value on repeated calls, `advance(timedelta(days=1))` moves exactly one day, `set` backwards raises, naive input raises, a non-UTC aware input is returned as UTC (same instant), `FixedClock(...)()` equals `.now()`, and both classes satisfy `isinstance(x, Clock)`.
- **Done when:** `uv run pytest tests/unit/test_clock.py` and `uv run ruff check .` pass.

### T2 [haiku] Wave 1: `tests/conftest.py`, network guard, Hypothesis profiles, offline meta-test
- **Files:** `tests/conftest.py`, `tests/test_offline_guard.py`, `tests/test_no_wallclock.py`, `.gitignore` (append the single line `.hypothesis/`; touch nothing else)
- **Depends on:** 004 T1 (deps). T1 is not required at import time: fixtures import `tradebot.clock` lazily.
- **Do:** implement section 2 and section 1's Hypothesis block in `tests/conftest.py`:
  1. `class NetworkAccessError(AssertionError)`. A session-scoped `autouse=True` fixture (use `pytest.MonkeyPatch.context()`) replaces `socket.socket.connect`, `socket.socket.connect_ex`, `socket.create_connection` and `socket.getaddrinfo` with functions raising `NetworkAccessError("network access blocked in tests: <PYTEST_CURRENT_TEST>")`. `connect` and `connect_ex` must still allow `AF_UNIX` sockets (check `self.family`), and `socket.socketpair()` must keep working. If `yfinance` is importable, replace `yfinance.download` and `yfinance.Ticker`; if `ccxt` is importable, replace `ccxt.Exchange.fetch`, all with functions raising `NetworkAccessError`. Wrap each optional import in `try/except ImportError`.
  2. Hypothesis: `settings.register_profile("ci", max_examples=200, deadline=None, derandomize=True, print_blob=True)`; `settings.register_profile("dev", max_examples=50, deadline=None)`; `settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))`.
  3. Function-scoped `autouse` fixture setting `TRADEBOT_CACHE_DIR=<tmp_path>/cache` and `TRADEBOT_DB_PATH=<tmp_path>/tradebot.db` via `monkeypatch.setenv`, so no test touches the user's real cache or database.
  4. Fixtures: `fixtures_root` (session; `Path(__file__).parent / "fixtures"`); `market_cache` (function; `shutil.copytree(fixtures_root/"market", tmp_path/"market")`, returns the copy so a test may write to it); `edge_cache` (same for `market_edge`); `fixed_clock` (returns `tradebot.clock.FixedClock(datetime(2024, 12, 31, 23, 0, tzinfo=timezone.utc))`, imported inside the fixture).
  - `tests/test_offline_guard.py` (the meta-test that the guard works): connecting to `("127.0.0.1", 9)` via `socket.socket().connect`, `socket.create_connection(("127.0.0.1", 9))` and `socket.getaddrinfo("example.com", 80)` each raise `NetworkAccessError`; the message contains the test's own name; `socket.socketpair()` still round-trips `b"x"`; `NetworkAccessError` subclasses `AssertionError`; `yfinance.download("SPY")` and `ccxt.Exchange.fetch(object(), "https://example.com")` raise `NetworkAccessError` (`pytest.importorskip` each module); the `ci` profile has `derandomize=True` and `max_examples=200`, and the `dev` profile has `max_examples=50` (`settings.get_profile(...)`).
  - `tests/test_no_wallclock.py`: a helper `find_wallclock_calls(root: Path) -> list[tuple[Path, int, str]]` scans `root/**/*.py`, skips files named `clock.py`, ignores lines whose first non-space character is `#`, and reports lines containing `datetime.now(`, `datetime.utcnow(`, `time.time(` or `date.today(`. Tests: (a) on a `tmp_path` tree, each of the four patterns is detected, and the same patterns inside a file named `clock.py` or after `#` are not; (b) the real `src/tradebot` tree returns `[]`.
- **Done when:** `uv run pytest tests/test_offline_guard.py tests/test_no_wallclock.py` passes, `HYPOTHESIS_PROFILE=ci uv run pytest tests` passes, the guard demonstrably raises, and AC 1 holds.

### T3 [sonnet] Wave 1: fixture generator and committed synthetic fixtures
- **Files:** `scripts/make_fixtures.py`, `tests/fixtures/market/**` (generated: `manifest.json` plus per-symbol `meta.json`, `gen-0001/bars.parquet`, `gen-0001/gen.json`), `tests/fixtures/market_edge/**` (generated, same shape)
- **Depends on:** 004 T1 (deps). Nothing else; this task needs no other spec and no network.
- **Do:** implement `scripts/make_fixtures.py` exactly as specified under section 2: layout, `--write` and `--check`, the PCG64 draw order, regime chain, factor model, per-symbol parameter table, OHLC and volume derivation, the eight edge symbols, manifests and `gen.json`. Use `exchange_calendars.get_calendar("XNYS").sessions_in_range("2018-01-02", "2024-12-31")` for equity dates (convert to tz-aware UTC midnight, index name `ts`) and every UTC day for crypto. SOL/USD starts 2020-04-10. Write the parquet with `DataFrame.to_parquet(path, engine="pyarrow", compression="snappy")` (columns `open, high, low, close, volume, fetched_at, source_rev`); edge files are written the same way but without validation. Run `uv run python scripts/make_fixtures.py --write`, run it a second time and confirm the file sha256s are unchanged, and commit the generated trees. Add module docstring stating "synthetic data; no network; seed 20180102".
- **Done when:** (1) all 20 clean symbols and 8 edge symbols exist with the row counts and dates of section 2 (ETFs 1,761 rows; BTC and ETH 2,556; SOL 1,727); (2) the anchors, bands and the `low <= open, close <= high` guarantee of section 2 hold (the generator asserts them and exits non-zero otherwise); (3) total `tests/fixtures/` size <= 2,500,000 bytes and each parquet <= 200 KB; (4) `--check` exits 0; (5) AC 2.

### T4 [haiku] Wave 1: fixture manifest and content test
- **Files:** `tests/test_fixtures_manifest.py`
- **Depends on:** T3, 004 T1
- **Do:** (`@pytest.mark.unit`, reads parquet directly with pandas/pyarrow; it never calls the generator.) For both `market/` and `market_edge/`: every file under the tree except `manifest.json` is listed in the manifest and vice versa; each file's sha256, byte size and (for parquet) row count and first/last date match; each `gen.json` `sha256` equals its `bars.parquet` sha256; `total_bytes` equals the real sum; `synthetic is True` and `seed == 20180102`. Budget: total bytes under `tests/fixtures/` <= 2,500,000 and each parquet <= 200,000. Clean universe: exactly the 20 expected symbols; for the 17 ETFs the index equals `exchange_calendars` XNYS sessions 2018-01-02..2024-12-31 (and 2019-07-04 and 2018-12-05 are absent); BTC/USD and ETH/USD have every calendar day; SOL/USD starts 2020-04-10 and ends 2024-12-31; index tz-aware UTC midnight, sorted, unique; columns exactly `open, high, low, close, volume` plus `fetched_at, source_rev`; `low <= min(open, close)` and `high >= max(open, close)` on every row; all prices > 0 and volume >= 1; no NaN. Edge set: `ZZNEW` first date 2021-03-01; `ZZGONE` last date 2022-06-30; `ZZGAP` lacks exactly the six sessions listed in section 2; `ZZDUP` has exactly one duplicated timestamp (2019-03-12); `ZZTZ` index tz is not UTC; `ZZBAD` has exactly one row with `high < low` (2019-05-15) and one row with `close == 0` (2019-05-16); `ZZFLAT` is constant; `ZZUP` is strictly increasing with `close_t = 100 + 0.5 t` (t = 0..299).
- **Done when:** AC 2.

### T5 [haiku] Wave 2: money and rounding hand-calc tests
- **Files:** `tests/unit/test_money_handcalc.py`
- **Depends on:** after 003 T1 (money module, Wave 1) merges
- **Do:** section 6.3-6.5 rounding cases against the money helpers (price CEILING/FLOOR, notional HALF_EVEN, fee HALF_UP, DOWN sizing); arithmetic in comments.
- **Done when:** AC 5 (rounding part).

### T6 [haiku] Wave 2: indicator hand-calc tests
- **Files:** `tests/unit/test_indicators_handcalc.py`
- **Depends on:** after 002 T2 (indicators, Wave 1) and T4 (edge fixtures `ZZFLAT`, `ZZUP`)
- **Do:** sections 6.1 and 6.2 with arithmetic in comments, plus the RSI 100 / 50 edge cases from the fixtures.
- **Done when:** AC 5 (indicators).

### T7 [sonnet] Wave 2: lookahead helpers, indicator truncation tests, leak canaries
- **Files:** `tests/lookahead/helpers.py`, `tests/lookahead/test_indicator_truncation.py`, `tests/lookahead/test_lookahead_detector_catches_leaks.py`
- **Depends on:** after 002 T2 (indicators and `INDICATORS` registry, Wave 1); T3 (uses `market_cache` data)
- **Do:** section 3: both checkers, strided `t` selection on the 1,761-bar ETF fixtures, registry completeness meta-test, canaries.
- **Done when:** AC 3 (indicator part).

### T8 [haiku] Wave 2: data-quality and survivorship unit tests
- **Files:** `tests/unit/test_data_quality.py`, `tests/unit/test_universe_membership.py`
- **Depends on:** T3; after 001 validators and universe (Wave 2) merge
- **Do:** run SPEC-001's validators over the `market_edge/` fixtures: `ZZGAP`, `ZZDUP`, `ZZTZ`, `ZZBAD` must each be flagged or rejected for their stated defect; no bars after `end`; bar immutability; `ZZNEW` and `ZZGONE` are never signaled or ranked outside their membership dates.
- **Done when:** every edge fixture defect is flagged.

### T9 [haiku] Wave 2: coverage floor checker
- **Files:** `scripts/check_coverage.py`, `tests/unit/test_check_coverage.py`
- **Depends on:** 004 T1 (pytest-cov); CI wiring is platform's
- **Do:** read coverage JSON, apply the section 9 item 4 floors only to packages that contain code, exit non-zero with a table on failure; unit-test it with a small fake JSON.
- **Done when:** AC 9 (coverage part).

### T10 [haiku] Wave 2: audit report template
- **Files:** `docs/reviews/_TEMPLATE.md`
- **Depends on:** none
- **Do:** copy the template from section 8 verbatim.
- **Done when:** the file exists.

### T11 [haiku] Wave 3: fills, fees, rebalance, caps, stops hand-calc tests
- **Files:** `tests/unit/test_fills_handcalc.py`, `tests/unit/test_risk_handcalc.py`
- **Depends on:** after 003 T6-T10 (risk, stops, kill switch, SimBroker, rebalancer, `PortfolioEngine`, Wave 3) merge
- **Do:** sections 6.3, 6.4, 6.5 (fill and sizing cases) and 6.6 (rebalance, cap, stop at `<=`, kill switch) through the engine's public API. Reviewer must double-check the arithmetic.
- **Done when:** AC 5 (execution).

### T12 [sonnet] Wave 3: strategy truncation tests
- **Files:** `tests/lookahead/test_strategy_truncation.py`
- **Depends on:** T7; after 002 T3 (`evaluate` and the `STRATEGIES` registry, Wave 3)
- **Do:** section 3 at signal level for the three STRATEGY.md section 4 sleeves and every registered template, including month-end and the 2024-12 year-end boundary.
- **Done when:** AC 3 (strategy part).

### T13 [sonnet] Wave 4: reference oracle and Hypothesis strategies
- **Files:** `tests/support/oracle.py`, `tests/support/strategies.py`, `tests/support/__init__.py`
- **Depends on:** after 003 ledger and `PortfolioEngine` types (Waves 2-3)
- **Do:** independent Decimal replay of fills using SPEC-003 section 2's rounding table, with no import from `tradebot.portfolio`; `bars_df`, `order_plan` strategies.
- **Done when:** the oracle reproduces the section 6.3 and 6.4 numbers.

### T14 [sonnet] Wave 4: accounting and risk property tests
- **Files:** `tests/property/test_accounting_invariants.py`, `tests/property/test_risk_limits.py`, `tests/property/test_invariants_catch_bugs.py`
- **Depends on:** T13; after 003 `PortfolioEngine` (Wave 3)
- **Do:** I1-I9 per section 4 plus seeded-bug canaries.
- **Done when:** AC 4.

### T15 [haiku] Wave 4: metrics hand-calc tests
- **Files:** `tests/unit/test_metrics_handcalc.py`
- **Depends on:** after 002 T4 (metrics, Wave 4)
- **Do:** section 6.6 metrics cases (drawdown, return, CAGR with P=252 vs 365).
- **Done when:** AC 5 (metrics).

### T16 [sonnet] Wave 4: backtest prefix-invariance tests
- **Files:** `tests/lookahead/test_backtest_prefix.py`
- **Depends on:** T7; after 002 `BarDriver`, T5 engine (Wave 4)
- **Do:** section 3 backtest-level prefix invariance on the golden configs.
- **Done when:** AC 3 (backtest part).

### T17 [sonnet] Wave 4: integration and golden harness
- **Files:** `tests/integration/test_backtest_e2e.py`, `tests/golden/harness.py`, `tests/golden/test_golden_backtests.py`, `tests/golden/test_golden_change_note.py`, `tests/golden/G1.json`, `G2.json`, `G3.json`, `G4.json`, `tests/golden/conftest.py` (the `--update-golden` option), `docs/reviews/golden/` (initial approved note, written by quality-lead)
- **Depends on:** T3; after 002 backtest and 003 `PortfolioEngine` are complete
- **Do:** section 5 on the synthetic fixtures. Initial values are produced by the first run, then quality-lead audits them by hand-recomputing at least three fills and the benchmark return before the PM approves the first note.
- **Done when:** AC 6.

### T18 [sonnet] Wave 4: validation-gate tests
- **Files:** `tests/gate/test_validation_gate.py`, `tests/gate/test_holdout_discipline.py`, `tests/support/synthetic.py`
- **Depends on:** after 002 T6 (gate and walk-forward, Wave 4)
- **Do:** section 7. `tests/support/synthetic.py` holds the in-memory, seeded 12-year random-walk generators the gate tests need (the committed fixtures only span 7 years); it shares no code with `scripts/make_fixtures.py`.
- **Done when:** AC 7.

### T19 [sonnet] Wave 5: runner restart/idempotency tests
- **Files:** `tests/runner/test_restart_idempotency.py`, `tests/runner/test_runner_equals_backtest.py`
- **Depends on:** T17; after 004 runner, DB and fault hook (Wave 5)
- **Do:** the seven tests under "Runner restart and idempotency tests".
- **Done when:** AC 8.

## New dev dependencies
None requested separately: platform's single Wave-1 task (004 T1) applies every dependency in one edit (ADR-0001 D11). The test-side set it must include is `hypothesis` (property tests, shrinking, deterministic CI profile; see section 1), `pytest-cov` (coverage and the JSON report that `scripts/check_coverage.py` reads) and `pytest-timeout` (a hung runner or infinite loop fails fast instead of stalling CI), plus runtime `numpy`, `pandas`, `pyarrow` and `exchange-calendars` for the fixture generator and manifest test.
Considered and rejected: `pytest-socket` (we need the blocker plus the yfinance/ccxt guards in one place; about 25 lines of our own conftest avoids a dependency), `freezegun` (injected `Clock` is cleaner and testable), `pytest-xdist` (suite is small, and it complicates the Hypothesis DB and golden ordering), `syrupy` (our golden harness needs the approval-note workflow), `pytest-randomly` (reseeds RNGs, interacting with deterministic fixtures). Runtime libraries (pandas, numpy, pydantic, SQLModel, FastAPI) come from the other specs.

## Open questions
- **For SPEC-003:** none outstanding for rounding: section 6 is checked against SPEC-003 section 2's table. Whole shares for equities, no shorting, no leverage, close-based stop with `<=`, and PM-only kill-switch reset are settled by ADR-0001.
- **For SPEC-002:** definition of walk-forward "consistency" for the gate tests; whether `INDICATORS` and `STRATEGIES` registries are exposed as section 3 requires (not stated in SPEC-002 today).
- **For platform-lead:** can CI run a network-less step (`unshare -n`) as an extra backstop? (Optional; the in-process guard is mandatory and sufficient.)
Resolved by ADR-0001 and removed: real-data fixtures (all fixtures are synthetic, D9), macOS matrix leg (CI is Ubuntu only, D12), mutation testing (deferred until after Phase 3, D12).

## Risks
- Golden tests can calcify a wrong number: the first golden values are audited by hand (T17) and the sensitivity meta-tests prove costs and timing matter.
- Oracle and system share an assumption (e.g. a misread rounding rule): the oracle is written from the SPEC-003 text, not by copying `src`; the audit compares the two implementations' rounding.
- Hypothesis runtime growth: capped by the `ci` profile and the 90 s budget; the slowest property tests are reduced rather than removed.
- Synthetic fixtures are not real markets: they prove mechanics (accounting, timing, lookahead, gates), not that a strategy works. Real-data sanity checks are a manual, local step from the user's own git-ignored cache (ADR D9). Regenerating the committed fixtures shifts every golden, so it needs the PM-approved note of section 5; the generator pins `PCG64` and the committed files are the source of truth, so a numpy upgrade cannot change a test input.
- Truncation tests are O(n²) on long series: strided sampling plus a short-series exhaustive pass keeps cost bounded while covering warmup and month boundaries.
- Parallel specs may disagree on names/ownership; the hook list above is the contract this spec needs, as reconciled by ADR-0001.
