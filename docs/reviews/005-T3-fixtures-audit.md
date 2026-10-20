# Audit: SPEC-005 T3 synthetic fixture generator and committed fixtures (Wave 1)

- **Auditor:** quality-lead
- **Date:** 2026-10-20
- **Code:** `scripts/make_fixtures.py` (516 lines)
- **Data:** `tests/fixtures/market/**` (60 files, 1,922,852 bytes) and `tests/fixtures/market_edge/**` (24 files, 262,238 bytes)
- **Against:** SPEC-005 section 2 and T3, ADR-0001 D9, SPEC-001 cache layout (Section 4 and its fixture requirements)
- **Verdict: PASS-WITH-NOTES**

The generator is deterministic, offline and synthetic. The committed tree is byte-identical to a fresh regeneration. All 20 clean series and 8 edge series have the specified calendars, row counts, layout and intended defects. The data is good enough for G1-G4 and the gate tests, with two soft spots: a weak BTC rally window and a thin gold-plating of edge-defect assertions in the generator itself (see F1 and F2).

## Method

1. Copied `scripts/make_fixtures.py` into the scratchpad (`.../scratchpad/repo/scripts/`). `ROOT` derives from `__file__`, so `--write` wrote into `scratchpad/repo/tests/fixtures`, never over the committed files. The script has no `--out` option.
2. Ran `--write` twice there, then `diff -rq` against the committed tree and `cmp` on both manifests.
3. Ran `--check` from the copy. It exited 0.
4. Loaded every committed parquet and independently verified calendars, OHLC, edge defects, `gen.json` / `meta.json` / manifest keys and sha256s, and regime behaviour (own script, not the generator's).

## Checklist

| Item | Result | Evidence |
|---|---|---|
| Determinism: regenerated tree vs committed | Y | `diff -rq` reports identical trees (both runs). Both `manifest.json` files are byte-identical (`cmp`), so every sha256 in the manifest matches. |
| `--check` passes | Y | Exit 0 |
| Manifest vs disk | Y | Files listed == files on disk (60 and 24). Every sha256 and byte size matches. `total_bytes` equals the sum. Every `gen.json` sha256 equals the sha256 of its `bars.parquet`. |
| Budget | Y | Total about 2.18 MB with parquet (2,177,390 bytes), all files under 200 KB. Generator asserts it (`make_fixtures.py:466-471`). Budget measured as 2,200,253 bytes by the generator over everything it wrote. |
| No network | Y | Grep for `socket`, `requests`, `urllib`, `http`, `yfinance`, `ccxt`, `download`: only the string literals `"yfinance"` / `"coinbaseexchange"` as source folder names (`:251`, `:327`). `exchange_calendars` is used offline (`:121-125`). |
| No wall clock | Y | No `datetime.now`, `time.time` or `utcnow`. `fetched_at` is the constant `CREATED` (`:32`, `:231`). |
| No real market data | Y | Prices come from `p0 * exp(cumsum(...))` over seeded PCG64 draws (`:179-225`). The first prices are the P0 table, and the generator pins anchors to the spec's synthetic reference build (`:371-383`). Every row has `source_rev == "synthetic-v1"`. The manifest says `"synthetic": true`. |
| Calendars | Y | ETFs: all 17 equal `exchange_calendars` XNYS sessions exactly (1,761 rows), and 2019-07-04 and 2018-12-05 are absent. BTC and ETH: all 2,556 UTC days. SOL: 1,727 rows from 2020-04-10 to 2024-12-31. |
| Index and columns | Y | Index name `ts`, tz UTC midnight, sorted, unique. Columns are `open, high, low, close, volume` (float64) then `fetched_at` (`timestamp[ms, tz=UTC]`) and `source_rev` (string). Snappy compression confirmed. |
| OHLC sanity (clean) | Y | `low <= min(open, close)` and `high >= max(open, close)` on every row of all 20 series. No NaN. Minimum price is 0.3936 (SOL), well above 0.01. Volume >= 1 everywhere. |
| Edge set intended defects | Y | See table below. |
| SPEC-001 layout | Y, with notes | `<source>/<SYMBOL_FS>/meta.json`, `gen-0001/bars.parquet`, `gen-0001/gen.json`. `meta.json` keys are exactly `current_gen`, `tf`, `schema`. `gen.json` keys are `basis, created, events, first, last, sha256` (SPEC-001 Section 4 lists `created, basis, first, last, sha256`, and the fixture requirement adds an empty `events`). See F3 for `basis`. |
| Anchors and acceptance bands | Y | Generator asserts them and ran green: SPY first bar 274.3097 and last 444.458, BTC first 14476.3872 and last 91316.3118, SOL first 0.5298 and last 14.8407, XLE last 90.2088. |

### Edge set verified (independent of the generator)

| Symbol | Rows | Verified |
|---|---|---|
| ZZNEW | 967 | First 2021-03-01, last 2024-12-31 |
| ZZGONE | 1132 | First 2018-01-02, last 2022-06-30 |
| ZZGAP | 497 | Missing exactly 2019-07-08..12 and 2019-07-15 (six sessions) |
| ZZDUP | 504 | One duplicated timestamp, 2019-03-12. The second row has close 119.0287 (= 117.8502 x 1.01) and high raised to match. |
| ZZTZ | 503 | Index tz `America/New_York`, first stamp `2018-01-01 19:00-05:00`. `gen.json` first/last are the UTC dates `2018-01-02` / `2019-12-31`. |
| ZZBAD | 503 | Exactly one `high < low` row (2019-05-15) and one `close == 0.0` row (2019-05-16) |
| ZZFLAT | 300 | Every column constant (nunique = 1), volume 1,000,000 |
| ZZUP | 300 | `close == 100 + 0.5 t` for all t, `open_0 = 99.75`, open_t = close_{t-1} |

## Does the data support goldens and gate tests?

Measured on the committed data (own computation, not the generator's):

- **Crash window (2020-02-19 to 2020-03-23):** SPY max drawdown 28.2%, window return -27.5%. Breadth over the 17 ETFs above the 200-day SMA drops to a minimum of 2 in 2020.
- **Bear window (2022-01-03 to 2022-10-12):** SPY max drawdown 24.3%, return -9.7%. TLT -14.8% and IEF -6.3% over the window (bonds fall with stocks), and the 2022 SPY-TLT daily correlation is -0.53. Breadth reaches 0 (maximum 16, mean 5.4 in 2022). The "bonds fall with stocks" intent holds in level terms even though the daily correlation is negative.
- **Momentum ranking changes:** The top-3 trailing-126-session-return set changes in 66.2% of consecutive month-ends (78 month-ends, 49 distinct leader sets). SPEC-005 requires >= 50% and quotes 70% for its reference build. 12-month top-3 changes 64.8%. Eleven or more ETFs lead at some point, and the leader counts are spread (XLE 24 months, XLV 22, QQQ 19, IWM 18, and down to SPY 2), so ranking-sensitive tests are non-trivial.
- **200-day SMA gate:** ETFs above their 200-day SMA ranges from 0 to 17, so both the "fallback to IEF" branch (G2) and the fully invested branch are exercised. SPY is below its 200-day SMA 24.7% of the time after warm-up.
- **G1 (RSI(2) < 10 and price above 200-day SMA, SPY and QQQ):** 116 qualifying signal days on SPY and 94 on QQQ (220 and 214 RSI-only). That is plenty for "many trades" and for the stop to fire (SPY full-sample max drawdown 39.7%).
- **G3 (crypto trend, 20-day over 50-day SMA and price above 50-day SMA):** 119 flips on BTC, 104 on ETH and 97 on SOL, with the rule on about 47-48% of days. That exercises entries and exits and 10+10 bps costs. SOL's late listing (2020-04-10) is present.
- **Crypto regimes:** crypto winter exists. BTC fell about 60% across calendar 2022 and ETH about 83%. Max BTC drawdown is 80.0% (peak 180,772 on 2022-02-04), and ETH and SOL show 91% and 85% drawdowns from prior peaks. BTC/ETH correlation is 0.74. The 2020-10-01 to 2021-04-14 rally is real for ETH (1.41x) and SOL (1.13x from a 2020-10-01 start), but BTC ends the window at 0.985x, with an intra-window high of 1.21x (see F1).
- **G4 kill-switch path:** whether the full fund actually trips the 20% kill switch cannot be confirmed from price data alone. Mixed-fund drawdown depends on the engine. The ingredients are present (a 28% equity crash, an 80% crypto drawdown, a 24% bear, bonds falling with equities). I recommend the G4 author asserts in a precondition that the kill switch trips (or the golden will pass vacuously on the kill-switch branch). This is a G4 test-design note, not a fixture defect.

## Findings (ranked)

### F1 (Low-Medium): The "crypto rally 2020-10-01..2021-04-14" is weak for BTC, so a crypto trend golden may show little BTC alpha in that window
Spec parameters (`D_C` override of +0.0035 per day on the window, `make_fixtures.py:171-174`, times the BTC `g_c` of 1.0 and `beta` 0.6 on a market factor that is flat to slightly negative) produce BTC 0.985x over the window and a 1.21x intra-window high. This is a faithful implementation of the spec, whose bands do not constrain it (the BTC band is on the final ratio and drawdown only). Consequence: crypto-trend (G3) results on BTC will not show a clean rally-then-winter story, and a reviewer reading the G3 JSON should not expect one. The spec wording promises a rally, so either accept this or have the PM raise `dC` in a fixture-change note. No change needed for Wave 1.

### F2 (Low): The generator does not self-check the edge set, only the clean set
`check_bands` (`:337-394`) asserts clean-universe bands, anchors and row counts. `edge_universe` (`:256-328`) asserts nothing: a regression such as a wrong drop date for ZZGAP, a lost duplicate for ZZDUP, or a defect that accidentally becomes valid would produce a "successful" regenerate with `--write` and pass `--check` (which compares against whatever is committed). Only the T4 manifest test (`tests/test_fixtures_manifest.py`) would catch it later. Adding the T4 edge assertions as `assert`s in `build()` would make T3 "done when" self-contained (the spec says the generator asserts the clean bands, so this is a hardening note, not a spec violation).

### F3 (Info, known PM decision): `basis = "synthetic_seed_20180102"` versus SPEC-001's `"synthetic"`
`make_fixtures.py:430` writes `f"synthetic_seed_{SEED}"`. SPEC-001 section on fixtures (line 257) says `basis: "synthetic"`, and SPEC-001 Section 4 shows `adj_close_as_of_<date>` for real data. SPEC-005 wins (PM decision). Consequence to carry forward: the `MarketData` reader and the validators must accept the `synthetic*` prefix and not do an equality check against `"synthetic"`. SPEC-001's own tests (AC 14) should assert the prefix, not equality. Also, any code that parses `basis` for `adj_close_as_of_` must treat `synthetic*` as a no-restatement basis, as SPEC-001 line 251 states.

### F4 (Info, known PM decision): The persistent drift `a_t` is computed over all calendar days and then sliced
`make_fixtures.py:194-197`. For equities this means the AR(1) decay (0.99 per calendar day) runs over weekend and holiday days that the series skips, so the per-session effective decay is about 0.986 rather than 0.99. That shortens momentum persistence slightly for ETFs versus crypto. Consequence: it is also why the measured top-3 change rate is 66%, a bit below the spec's 70% reference figure, but still above the 50% floor. SOL's drift state at its listing date is already evolved (`a_all[pos]`) rather than starting from 0, which is consistent with "a symbol's start date never shifts its draws".

### F5 (Low): `--write` has no output-directory option and always deletes the destination trees
`main()` passes the committed `FIXTURES` path (`:508`), and `write_tree` calls `shutil.rmtree(root)` (`:416-417`). Anyone running `--write` locally replaces the committed fixtures with whatever their numpy / pandas / pyarrow produce, and `--check` compares only DataFrames (`rtol=1e-9`), not bytes. The fixture policy (PM-approved note) is in SPEC-005, but not enforced in the script. An `--out DIR` option (default the committed path, refuse to overwrite without `--force`) would make audits and "regenerate into a temp copy" trivial. This audit had to copy the script and rely on `ROOT = Path(__file__).parent.parent`.

### F6 (Low): The byte-level sha256s depend on the pyarrow / pandas versions
Parquet bytes include pandas schema metadata and writer-version strings. Determinism holds in this environment (two consecutive `--write` runs and an independent run all matched the committed manifest), and CI never regenerates (the committed files are the fixtures), so a library upgrade cannot change a test input. The risk is only for a human who regenerates after upgrading pyarrow and gets a different manifest despite identical data; `--check` (frame comparison) is the right tool and exits 0. No action.

### F7 (Info): Measured values versus the spec's reference build
SPY crash drawdown 28.15% (reference 28.2%), SPY bear drawdown 24.26% (24.3%), BTC ratio 6.76x (6.8x), ETH 6.45x (6.5x), SOL 29.68x (29.7x), BTC drawdown 80.0% (80%), total bytes 2,200,253 versus about 2.2 MB. Top-3 change rate 66.2% versus the stated 70%: the only visible deviation, and it is explained by measurement convention (month-end sampling and the 126-session lookback). The draw order is confirmed by the exact anchor match (`check_bands` anchors tolerance 1e-3 passes).

## Test gaps and recommendations

- Add the edge-defect assertions of T4 (F2) to `build()`, or confirm `tests/test_fixtures_manifest.py` (T4) covers all eight, including `ZZTZ` tz and `ZZDUP` second-row values.
- Consider an `--out` flag (F5).
- Add a G4 precondition that the kill switch trips on these fixtures (otherwise the kill-switch branch is untested by the golden).
- When the reader tests are written (SPEC-001 AC 3 and 14), assert the `synthetic*` prefix, not equality (F3).

## Re-audit needed?
Yes, only if the PM approves a fixture change (for example raising the crypto rally drift, F1), a draw-order change, a library upgrade that alters parquet bytes, or a change to `basis` / layout keys.
