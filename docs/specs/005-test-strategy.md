# SPEC-005: Test strategy and correctness guardrails

- **Lead:** quality-lead
- **Status:** approved with amendments (see ADR-0001 §4; ADR wins on conflict)
- **Roadmap item:** Phase 0.5 (lead research round); applies to Phases 1-4
- **Related ADRs/specs:** SPEC-001 market data, SPEC-002 strategy/backtest, SPEC-003 execution/sim/portfolio/risk, SPEC-004 platform (all being written in parallel; ADR-0001 will reconcile). Hook names below are illustrative until ADR-0001 fixes them.

## Goal
Define how we prove the numbers are true: test layers and who owns each, offline fixture policy, lookahead and accounting guardrails, golden backtests, hand-calculated references, validation-gate tests, the audit process and CI gates. Every claim in `docs/STRATEGY.md` §7-8 (no lookahead, costs on every fill, date-aware universe, deterministic results) becomes an automated check.

## Non-goals
- Designing features (other leads) or writing test code (workers write it from this spec).
- Live-broker or network integration tests (forbidden by CLAUDE.md hard rule 1 and the offline rule below).
- Performance or load testing beyond the runtime budget in §9.
- Mutation testing as a gate (optional later; see Open questions).

## Design

### 1. Test layers and ownership
Directory layout under `tests/`: `unit/`, `property/`, `lookahead/`, `integration/`, `golden/`, `gate/`, `runner/`, `support/` (shared helpers, no tests), `fixtures/`. Each layer has a pytest marker (`unit`, `property`, `integration`, `golden`, `restart`, `slow`); `--strict-markers` is on.

| Layer | What it proves | Owned by (area) | Written by |
|---|---|---|---|
| Unit | One function/class, exact hand-worked numbers, edge cases (empty, NaN, single bar) | each area's lead defines in its spec; quality-lead defines the reference cases in §6 | test-engineer (haiku) |
| Property-based | Invariants hold for *all* generated inputs (accounting, risk limits, fill timing, determinism) | execution (accounting, risk), strategy (indicator/strategy invariants), data (bar validation) | engineer (sonnet) |
| Lookahead (truncation) | Past signals are unchanged by future bars | strategy (indicators, signals, backtest) | engineer (sonnet) |
| Integration | Backtest end-to-end on committed fixtures through real data provider, strategy, SimBroker, ledger, metrics | strategy + execution jointly; quality-lead owns the scenario list | engineer (sonnet) |
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
`derandomize=True` means CI never flakes, and failures reproduce locally. The `.hypothesis/` directory is git-ignored. A property test that finds a bug must be turned into a plain regression unit test (copy the shrunk example) in the same PR.

### 2. Fixture policy and offline enforcement
**Rule: no test touches the network, the wall clock, or the user's real cache/DB.** Tests use committed fixtures and `tmp_path`.

**Format.** CSV (text, diffable in review), one file per symbol: `tests/fixtures/bars/<SYMBOL>.csv` (`BTC-USD.csv` for `BTC/USD`; filename mapping defined by data-lead's symbol normalization). Columns: `date,open,high,low,close,volume`, UTC dates `YYYY-MM-DD`, prices to 4 decimals as written by the source, adjusted-price convention exactly as the real provider cache stores it (data-lead decides; the fixture must match the cache schema so the integration test exercises the real parquet-cache read path from a `tmp_path` copy, not a CSV shortcut). `tests/fixtures/MANIFEST.json` records, per file: sha256, row count, first/last date, source (`yfinance`/`ccxt:<exchange>`), retrieval date, and the generator script git revision.

**Contents and size budget.**
- *Real-data snapshot*, window **2018-01-02 to 2023-12-29** (about 1,510 stock bars, 2,190 crypto bars). It covers the Feb-Mar 2020 crash, the 2020-21 rally, the 2022 bear market and the 2023 recovery, so the RSI, 200-day SMA and kill-switch paths all fire. Symbols: stocks SPY, QQQ, IWM, EFA, TLT, IEF, GLD, XLK, XLE; crypto BTC/USD, ETH/USD, SOL/USD. SOL/USD starts in 2020 and the fixture keeps that partial history (a natural late-listing case). The fixture stays as returned by the source: no hand edits.
- *Synthetic edge-case fixtures* (generated by seeded code in `tests/support/synthetic.py`, never from the network): a late-listed and a delisted symbol (`ZZNEW`, `ZZGONE`) for survivorship tests; bars with a gap, a duplicate timestamp, a non-UTC timestamp and a high<low row (must be rejected or flagged by data-lead's validators); a US-holiday week for calendar tests; a flat series (RSI undefined case); a monotone-up series (RSI=100 case).
- **Budget:** each CSV ≤ 150 KB, whole `tests/fixtures/` ≤ 1.5 MB, enforced by `tests/test_fixtures_manifest.py` (also verifies the sha256s so nobody edits a fixture silently). Real-data snapshots are for private test use; the manifest notes this.

**Regeneration.** `scripts/make_fixtures.py` is the *only* code allowed to use the network. It is run by hand (`uv run python scripts/make_fixtures.py --write`), never in CI or tests. It rewrites the CSVs and manifest. Because regenerated data can change every golden number, a fixture regeneration requires the same PM-approved note as a golden update (§5). Fixtures are otherwise immutable.

**Enforcement of "no network".** Three layers, all in `tests/conftest.py`:
1. An `autouse=True`, session-scoped fixture patches `socket.socket.connect`, `socket.socket.connect_ex`, `socket.create_connection` and `socket.getaddrinfo` to raise `NetworkAccessError` (an `AssertionError` subclass) naming the test. AF_UNIX and `socketpair()` stay allowed (asyncio and FastAPI `TestClient` need them). No test may opt out; there is no `network` marker.
2. An `autouse` fixture replaces `yfinance.download`, `yfinance.Ticker`, and `ccxt.<exchange>.fetch_ohlcv` with functions that raise, so an un-mocked adapter fails with a clear message even if it caught the socket error.
3. `tests/test_offline_guard.py` proves the guard works (a connect to 127.0.0.1:9 and a DNS lookup both raise). CI additionally runs the suite once inside a network-less step if the platform makes that cheap (`unshare -n` on Linux), as a backstop. Defining this in CI is platform-lead's call; layers 1-3 are mandatory.

**Time.** Code under test must take an injected `Clock` (SPEC-004 runner) and never call `datetime.now()` directly; tests use a `FakeClock`. A grep-based test (`tests/test_no_wallclock.py`) fails if `datetime.now(`, `datetime.utcnow(`, `time.time(` or `date.today(` appears under `src/tradebot/` outside `clock.py`. `freezegun` is not used.

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
- **Which `t`:** all `t` for short series (≤ 300 bars, synthetic); for the 1,510-bar fixture use all `t ≥ warmup - 2` with stride 7, plus the first 3 bars, the last 3 bars, and the warmup boundary exactly (warmup-1, warmup, warmup+1). Indicator tests cost O(n²/stride); budget ≤ 5 s total.
- **Which targets, with completeness enforced:** parametrize from the *indicator registry* (SPEC-002 must expose `INDICATORS: dict[str, Callable]`). A meta-test asserts every registry entry appears in the truncation test's parameter list, so adding an indicator without a lookahead test fails CI. The same applies to a `STRATEGIES`/sleeve registry for signal-level checks (the three STRATEGY.md §4 sleeves plus every template).
- **Exactness:** compare exactly (`rtol=0`). An algorithm that runs from index 0 forward does identical float operations on a prefix, so bit-equality is achievable (pandas `rolling`/`ewm` qualify). If a vectorized implementation cannot meet that, the owner may set `rtol=1e-12` for that indicator with a one-line justification in the test; anything looser needs quality-lead sign-off.
- **Backtest-level prefix invariance** (`test_backtest_prefix.py`): run the golden configs on `bars[:T]` and on all bars. Every fill dated ≤ the last bar of the short run, plus the equity values on those dates, must be identical. The short run's final-bar signal must produce no fill (nothing exists at t+1).
- **Monthly/weekly signals:** rebalance-date logic must also use only past data (month-end detection must not peek at the next bar to learn "this is the last trading day"; the fixture-based test runs the truncation check across month boundaries and the 2023-12 year end, where the next bar is absent).
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
        mv = sum(q * bars.close_at(sym, snap.ts) for sym, q in snap.positions.items())  # rounding per SPEC-003
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
        assert f.price == expected_price(raw, f.side, f.market)        # open*(1 +/- bps), SPEC-003 rounding
        assert f.fee == expected_fee(f.qty * f.price, f.market)        # stock 0, crypto 10 bps
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
Four pinned runs on the §2 real-data fixture, start 2018-01-02, end 2023-12-29, initial cash `Decimal("100000")`, costs per STRATEGY.md §6:

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
These are literal expected values the tests assert. Test authors must copy the arithmetic into comments. Values come from this spec, not from running the code.

**6.1 SMA(3).** Closes `[10, 12, 14, 16, 12]` give `[NaN, NaN, 12, 14, 14]`.
Check: (10+12+14)/3=12; (12+14+16)/3=14; (14+16+12)/3=14. Also assert the first `n-1=2` values are NaN (not partial means).

**6.2 RSI(2), Wilder smoothing (alpha=1/2).** Closes `[100, 102, 101, 99, 98]`, so changes are `+2, -1, -2, -1`.
- Seed at index 2 (first two changes +2, -1): avg gain = (2+0)/2 = 1; avg loss = (0+1)/2 = 0.5; RS = 2; **RSI = 100 - 100/3 = 66.6667**.
- Index 3 (change -2): avg gain = (1x1+0)/2 = 0.5; avg loss = (0.5x1+2)/2 = 1.25; RS = 0.4; **RSI = 100 x 0.4/1.4 = 28.5714**.
- Index 4 (change -1): avg gain = (0.5+0)/2 = 0.25; avg loss = (1.25+1)/2 = 1.125; RS = 2/9; **RSI = 100 x 2/11 = 18.1818**.
Expected series `[NaN, NaN, 66.6667, 28.5714, 18.1818]`, abs tol 1e-4. Edge cases: monotone-up closes give RSI 100; flat closes give the value SPEC-002 defines for 0/0 (must be documented, and must not raise or emit inf). **Assumption:** Wilder smoothing with a simple-average seed; if SPEC-002 picks another RSI convention, the numbers must be redone with that formula, but the test stays hand-worked.

**6.3 Stock round trip (ETF: $0 commission, 5 bps slippage).** Cash 100,000.00. Signal on bar t; bar t+1 open = 100.00; buy 100 shares.
- Buy fill price = 100.00 x 1.0005 = **100.05**; notional 10,005.00; fee 0.00. Cash after = **89,995.00**.
- Later signal; bar open = 110.00; sell 100 shares. Sell fill price = 110.00 x 0.9995 = **109.945**; notional = 10,994.50 (exact at cents); fee 0.00. Cash after = **100,989.50**.
- Net P&L = **+989.50**. Positions empty; equity = cash = 100,989.50. (If SPEC-003 quantizes prices to cents, 109.945 rounds under its stated mode to 109.94 (half-even) or 109.95 (half-up); the test then uses the mode SPEC-003 names. The notional rounding rule must be single and documented.)
- Bar-end equity while holding (close 105.00): 89,995.00 + 100 x 105.00 = **100,495.00**.

**6.4 Crypto round trip (10 bps fee, 10 bps slippage, fractional qty).** Cash 100,000.00, buy 0.5 BTC at next-bar open 20,000.
- Buy price = 20,000 x 1.001 = **20,020.00**; notional 10,010.00; fee = 10,010.00 x 0.001 = **10.01**; cash out 10,020.01; cash after = **89,979.99**.
- Sell 0.5 BTC at open 22,000: price = 22,000 x 0.999 = **21,978.00**; notional 10,989.00; fee = 10.989, which rounds to **10.99** (half-even/half-up agree at 2 dp); cash in 10,978.01; cash after = **100,958.00**.
- Net P&L = **+958.00**; total fees 21.00 (10.01+10.99). Assert both legs have fee > 0 and slippage in the adverse direction.

**6.5 Others (haiku, workers compute the arithmetic in-test comments).**
- Metrics: max drawdown for equity `[100, 120, 90, 110]` is (90-120)/120 = **-25.0%**; total return 10%; CAGR from known day counts with 252 vs 365 annualization.
- Rebalance: 100,000 portfolio, 50/20/20/10 targets -> sleeve dollar targets 50,000 / 20,000 / 20,000 / 10,000.
- Cap: a BUY that would take one asset above 25% of equity is shrunk so post-trade weight is exactly <= 25%.
- Trailing stop: highest close since entry 120, ATR(14)=2 -> stop level 120 - 2.5x2 = **115**; stop level never decreases when price falls; a close at 114.99 triggers an exit order that fills at the *next* open.
- Kill switch: peak equity 100,000; equity 80,000 -> triggered (>= 20%); equity 80,001 -> not triggered.

### 7. Validation-gate tests (STRATEGY.md §7)
The gate is a pure function owned by SPEC-002: `validate(report: BacktestBundle) -> GateReport` with one named pass/fail per criterion (history length, holdout discipline, walk-forward consistency, OOS Sharpe/Sortino vs benchmark and drawdown <= 1.0x benchmark, >= 30 trades, +/-20% robustness). The paper-trading criterion (6) is a time check recorded by the runner and is covered by a unit test that the gate stays "pending" until 3 months of forward data exist.

All gate tests use **synthetic, seeded data** from `tests/support/synthetic.py` (12 years of daily bars for stocks, about 3,000 rows; never real data, so they are immune to fixture refreshes).

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
7. Single OS (ubuntu, Python 3.12) is required; adding macOS is optional and, if added, relies on the 1e-9 float tolerance.

### Hooks needed from each area (to be named in ADR-0001)
- **SPEC-001 data:** a `FixtureProvider`/provider that reads `tests/fixtures/bars` through the real cache read path; `get_bars(..., end)` never returns bars after `end`; validators for gap/duplicate/non-UTC/OHLC-sanity; date-aware `universe.members(date)`; immutable `Bar` once cached (re-fetch with different values raises).
- **SPEC-002 strategy/backtest:** `INDICATORS` and `STRATEGIES` registries; indicators as pure functions of the visible bars; deterministic `run_backtest(config, provider, broker) -> BacktestResult` exposing `signals`, `fills`, `snapshots`, `equity_curve`, `metrics`, `benchmark`; metrics as pure functions; `validate()` gate with per-criterion results; documented RSI convention; test-only flag to shift signals earlier (for the sensitivity meta-test).
- **SPEC-003 execution:** `Fill` carries `signal_ts`, `fill_ts`, `raw_open`, `price`, `qty`, `fee`, `side`, `market`, `sleeve_id`, `symbol`; one documented rounding function and mode; snapshots per bar with cash, positions, sleeve breakdown; an order log with REJECTED/shrunk reasons; kill-switch state and reset event; the invariants list from the execution-lead return.
- **SPEC-004 platform:** injected `Clock`; fills + `last_processed_bar` committed in one DB transaction; deterministic idempotency key per order (`portfolio_id, strategy_id, symbol, signal_bar_ts`); a fault-injection seam in the runner (`fault_hook(point)` with points `after_signal`, `after_fills_before_commit`, `after_commit_before_marker`) active only in tests; runner step function callable bar-by-bar without APScheduler; pytest/coverage/timeout config and CI as in §9.

### Runner restart and idempotency tests (layer detail)
All use a file-based SQLite DB in `tmp_path`, with a fresh engine created for "restart", a `FakeClock`, and the fixture provider.
1. `test_replay_same_bar_is_noop`: process bar d twice -> identical ledger, no extra fills, no duplicate equity row.
2. `test_crash_at_each_fault_point_then_restart`: for each fault point, raise, rebuild the runner, resume; the final ledger hash must equal the uninterrupted run's. After `after_fills_before_commit` the DB must hold *no* partial fills (atomicity).
3. `test_catch_up_after_downtime`: stop for 3 trading days, restart -> missed bars processed in order, result equals the batch backtest over the same bars.
4. `test_runner_equals_backtest`: feed the G4 fixture bar-by-bar through the runner; the ledger hash and final equity equal golden G4 exactly. This enforces the PM rule that backtest and forward paths share one implementation.
5. `test_no_bar_on_holiday_or_weekend_for_stocks` (2023-07-04, a weekend) and crypto processes weekends.
6. `test_restated_bar_raises`: the provider returns a different value for an already-stored bar -> an error/alert, no silent history change.
7. Optional `@slow` subprocess test using `os._exit` mid-run and then restarting.

## Acceptance criteria
1. Running `uv run pytest` makes zero network connections; the guard test proves a socket connect and a DNS lookup both fail.
2. `tests/fixtures/` is <= 1.5 MB, each file <= 150 KB, and hashes match `MANIFEST.json`.
3. Every registered indicator and strategy has a passing truncation and future-perturbation test; the canary leak functions are detected.
4. Properties I1-I9 pass under the `ci` profile, and every seeded-bug canary is caught by at least one.
5. Hand-calculated cases in §6 are asserted with the exact numbers given.
6. G1-G4 pass with the stated tolerances; an unapproved edit to any golden file fails the change-note test.
7. The gate rejects the memorizer (failing criteria 3 and 5), accepts the positive control, and each per-criterion negative behaves as listed.
8. Runner tests 1-6 pass, including crash at each fault point and runner-equals-golden-G4.
9. CI enforces §9, including per-package coverage floors and the runtime budget.

## Tasks
Tags per CLAUDE.md. "After NNN X lands" means that area's task must be merged first. Tasks in separate directories have no file overlap and can run in parallel once their dependencies are met.

### T1 [haiku] Test infrastructure and offline guard
- **Files:** `tests/conftest.py`, `tests/test_offline_guard.py`, `tests/test_no_wallclock.py`
- **Depends on:** none (the `Clock` module name from 004 only for the grep exception; use `clock.py`)
- **Do:** autouse socket/yfinance/ccxt blockers (§2), Hypothesis profiles, markers registered; guard proof test; wall-clock grep test.
- **Done when:** AC 1.

### T2 [haiku] Dev dependencies and pytest config (coordinate with platform-lead, who owns `pyproject.toml`)
- **Files:** `pyproject.toml`
- **Depends on:** none (PM sequences against 004's pyproject edits)
- **Do:** add the dev deps listed under "New dev dependencies"; add `markers`, `addopts = "--strict-markers --timeout=20"`, `filterwarnings = ["error"]`, `[tool.coverage.run] branch = true, source = ["tradebot"]`.
- **Done when:** `uv sync` and `uv run pytest` pass on the existing smoke test.

### T3 [sonnet] Fixture generator, manifest and loader
- **Files:** `scripts/make_fixtures.py`, `tests/fixtures/bars/*.csv`, `tests/fixtures/MANIFEST.json`, `tests/support/fixtures.py`, `tests/support/synthetic.py`, `tests/test_fixtures_manifest.py`
- **Depends on:** after 001 DataProvider/cache schema lands
- **Do:** implement §2 (real snapshot via the real adapters, synthetic edge-case series seeded); size and hash checks.
- **Done when:** AC 2.

### T4 [sonnet] Reference oracle and Hypothesis strategies
- **Files:** `tests/support/oracle.py`, `tests/support/strategies.py`, `tests/support/__init__.py`
- **Depends on:** after 003 Fill/Ledger types land
- **Do:** independent Decimal replay of fills; `bars_df`, `order_plan` strategies.
- **Done when:** oracle unit-checked on §6.3/6.4 numbers.

### T5 [sonnet] Accounting and risk property tests
- **Files:** `tests/property/test_accounting_invariants.py`, `tests/property/test_risk_limits.py`, `tests/property/test_invariants_catch_bugs.py`
- **Depends on:** T4; after 003 SimBroker, ledger, sleeves and risk land
- **Do:** I1-I9 per §4 plus seeded-bug canaries.
- **Done when:** AC 4.

### T6 [sonnet] Lookahead (truncation) tests
- **Files:** `tests/lookahead/helpers.py`, `tests/lookahead/test_indicator_truncation.py`, `tests/lookahead/test_strategy_truncation.py`, `tests/lookahead/test_backtest_prefix.py`, `tests/lookahead/test_lookahead_detector_catches_leaks.py`
- **Depends on:** T3; after 002 indicators/registries and backtest loop land
- **Do:** §3 including registry completeness meta-test.
- **Done when:** AC 3.

### T7 [haiku] Hand-calculated unit tests: indicators and metrics
- **Files:** `tests/unit/test_indicators_handcalc.py`, `tests/unit/test_metrics_handcalc.py`
- **Depends on:** after 002 indicators and metrics land
- **Do:** §6.1, 6.2, 6.5 metrics cases, with arithmetic in comments.
- **Done when:** AC 5 (indicators and metrics).

### T8 [haiku] Hand-calculated unit tests: fills, fees, rebalance, caps, stops
- **Files:** `tests/unit/test_fills_handcalc.py`, `tests/unit/test_risk_handcalc.py`
- **Depends on:** after 003 SimBroker and risk land
- **Do:** §6.3, 6.4, 6.5 rebalance/cap/stop/kill-switch cases. Reviewer must double-check the arithmetic.
- **Done when:** AC 5 (execution).

### T9 [haiku] Data-quality and survivorship unit tests
- **Files:** `tests/unit/test_data_quality.py`, `tests/unit/test_universe_membership.py`
- **Depends on:** T3; after 001 validators and universe land
- **Do:** gap/duplicate/non-UTC/OHLC-sanity rejection, no bars after `end`, bar immutability, late-listed and delisted symbols never signaled or ranked outside their membership dates.
- **Done when:** the synthetic fault fixtures are all flagged.

### T10 [sonnet] Integration and golden harness
- **Files:** `tests/integration/test_backtest_e2e.py`, `tests/golden/harness.py`, `tests/golden/test_golden_backtests.py`, `tests/golden/test_golden_change_note.py`, `tests/golden/G1.json`, `G2.json`, `G3.json`, `G4.json`, `tests/golden/conftest.py` (the `--update-golden` option), `docs/reviews/golden/` (initial approved note, written by quality-lead)
- **Depends on:** T3; after 002 backtest and 003 SimBroker/portfolio are complete
- **Do:** §5. Initial values are produced by the first run, then quality-lead audits them by hand-recomputing at least three fills and the benchmark return before the PM approves the first note.
- **Done when:** AC 6.

### T11 [sonnet] Validation-gate tests
- **Files:** `tests/gate/test_validation_gate.py`, `tests/gate/test_holdout_discipline.py`
- **Depends on:** T3 (synthetic); after 002 gate and walk-forward land
- **Do:** §7.
- **Done when:** AC 7.

### T12 [sonnet] Runner restart/idempotency tests
- **Files:** `tests/runner/test_restart_idempotency.py`, `tests/runner/test_runner_equals_backtest.py`
- **Depends on:** T10; after 004 runner, DB and fault hooks land
- **Do:** the seven tests under "Runner restart and idempotency tests".
- **Done when:** AC 8.

### T13 [haiku] Coverage floor checker
- **Files:** `scripts/check_coverage.py`, `tests/unit/test_check_coverage.py`
- **Depends on:** T2; CI wiring is platform-lead's (004)
- **Do:** read coverage JSON, apply per-package floors from §9 item 4, nonzero exit with a table on failure; unit-test it with a small fake JSON.
- **Done when:** AC 9 (coverage part).

### T14 [haiku] Audit report template
- **Files:** `docs/reviews/_TEMPLATE.md`
- **Depends on:** none
- **Do:** copy the template from §8 verbatim.
- **Done when:** the file exists.

## New dev dependencies
- `hypothesis` (property tests, shrinking, deterministic CI profile; see §1).
- `pytest-cov` (coverage and the JSON report that `scripts/check_coverage.py` reads).
- `pytest-timeout` (a hung runner or infinite loop fails fast instead of stalling CI; enforces the runtime budget).
Considered and rejected: `pytest-socket` (we need the blocker plus the yfinance/ccxt guards in one place; about 25 lines of our own conftest avoids a dependency), `freezegun` (injected `Clock` is cleaner and testable), `pytest-xdist` (suite is small, and it complicates the Hypothesis DB and golden ordering), `syrupy` (our golden harness needs the approval-note workflow), `pytest-randomly` (reseeds RNGs, interacting with deterministic fixtures). Runtime libraries (pandas, numpy, pydantic, SQLModel, FastAPI) come from the other specs.

## Open questions
- **For SPEC-003:** the rounding mode and where it applies (per-price, per-notional, per-fee, per-mark); fractional shares for stocks (affects I5/§6.3 quantities); leverage and shorting being disallowed (I2 assumes so); whether risk limits are checked only at order time or also flagged on drift; kill-switch reset mechanism. §6.3 and §6.4 numbers must be re-checked against the final rounding rule.
- **For SPEC-002:** RSI smoothing convention (the §6.2 numbers assume Wilder seed-average), definition of walk-forward "consistency", how 0/0 RSI is returned.
- **For SPEC-001:** adjusted vs raw OHLC in the cache (fills use the open, so the convention must be consistent between fixture and live cache); symbol-to-filename mapping.
- **For the PM:** are real-data fixture snapshots acceptable to commit (private repo, small, with a provenance manifest), or should all fixtures be synthetic? Real data is preferred because the 2020 and 2022 regimes exercise the strategies. Also: mutation testing (`mutmut`) on `sim/` and `risk/` as a non-blocking nightly job after Phase 3?
- **For platform-lead:** can CI run a network-less step (`unshare -n`)? Is a macOS matrix leg wanted?

## Risks
- Golden tests can calcify a wrong number: the first golden values are audited by hand (T10) and the sensitivity meta-tests prove costs and timing matter.
- Oracle and system share an assumption (e.g. a misread rounding rule): the oracle is written from the SPEC-003 text, not by copying `src`; the audit compares the two implementations' rounding.
- Hypothesis runtime growth: capped by the `ci` profile and the 90 s budget; the slowest property tests are reduced rather than removed.
- Real-data fixture snapshots drift if regenerated at a different date (adjusted prices change after dividends): regeneration is gated by the PM-approved note, and the manifest records the retrieval date.
- Truncation tests are O(n²) on long series: strided sampling plus a short-series exhaustive pass keeps cost bounded while covering warmup and month boundaries.
- Parallel specs may disagree on names/ownership; the hook list above is the contract this spec needs and will be reconciled in ADR-0001.
