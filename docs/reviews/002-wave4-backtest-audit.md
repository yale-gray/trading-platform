# Audit 002: Wave 4 backtest, validation gate and golden set

- **Auditor:** quality-lead
- **Date:** 2026-10-21
- **Code:** `src/tradebot/backtest/{driver 341, trades 206, metrics 103, engine 395, validation ~980}.py`; small fixes in `sim/broker.py` and `risk/killswitch.py` (no git in the workspace, so not diffed; covered by `tests/sim/test_broker_gaps.py` and the full suite)
- **Tests:** `tests/{backtest,golden,gate,integration,properties,lookahead,strategy,sim,devconsole}`; full suite 2041 passed (about 132 s with mutants running; 74 s `not slow and not golden` plus 82 s `slow or golden` run back to back); `uv run ruff check .` clean; mypy not run
- **Against:** SPEC-002 sections 6-8 and ACs, SPEC-005 sections 3-7, ADR-0001 D3-D6 and D10, CLAUDE.md hard rules 2 and 4
- **Verdict: PASS-WITH-NOTES**

No lookahead, wrong-number or missing-cost blocker was found. The golden values were recomputed independently and match. Four majors remain: the blend is not static, walk-forward window boundaries are unpinned, a few gate thresholds are unpinned, and the CI budget is exceeded. None needs a re-audit unless F1 is "fixed" by changing the blend, which moves G4's pinned benchmark.

## Scope
The Wave 4 uncommitted files listed above, plus the dev console backtest view (skimmed: it only calls `run_with_benchmark`, no logic of its own). Not in scope: the data and strategy engines (audited earlier).

## Evidence
- **Lookahead, end to end**
  - `BarDriver.step` processes the engine bar (fills at this open) before it evaluates signals (`driver.py` step order). Orders created at t fill at the next `process_bar`.
  - First G4 fills: signal 2019-01-02, fill 2019-01-03.
  - `tests/lookahead/test_backtest_prefix.py` compares ledger and equity for G1-G4 on truncated bars. That is a strong, exact check.
  - Features are precomputed causally. Truncation and poison tests exist for indicators and strategies.
- **OOS and walk-forward**
  - `make_is_view` truncates to `< cut`, and `DataView.__post_init__` raises if any frame reaches the cut.
  - `walk_forward` and `sensitivity_sweep` accept only a locked view. `run_validation` ends the IS config at `is_end`.
  - `run_oos` is the only reader of full bars and evaluates each hash once. A second hash for the same strategy sets `peeking`, which fails gate 1.
  - `test_holdout_discipline.py` poisons the OOS bars and asserts nothing changes.
- **Metrics (spec 7)**
  - `metrics.py` matches every formula: ddof=1 std, `rf_p`, sortino DD over all N periods, CAGR with 365.25-day years independent of P, max drawdown from the running peak, and NaN guards.
  - Independent recompute for G4: `total_return` 0.2291905 equals final equity 122,919.05 / 100,000 - 1.
  - Hand tests in `test_metrics_handcalc.py` pin sharpe, sortino, CAGR, drawdown and exposure.
- **Golden hand audit** (script in the scratchpad, using raw fixture bars and the `ceil8` slippage and half-up fee rules from `sim/costs.py`)
  - Five G4 BUY fills match to 8 dp: EFA 97.09102125, IWM 188.56323450, XLB 64.01369085, BTC/USD 27918.32443400 with fee 8.11, ETH/USD 1309.35394590 with fee 5.22.
  - Two of the last fills match: BTC 92627.21498030 and QQQ 243.51459645.
  - The signal-before-fill ordering holds on all of them.
  - SPY benchmark: buy 321 shares at open 2019-01-03 plus 5 bps, hold to the 2024-12-31 close. The independent return is 0.42921008 against the golden 0.4292101 (the difference is cent rounding of the final equity).
  - BTC benchmark: independent 2.2675703 against golden 2.2675703.
- **Blend**
  - A static 70/20/10 hold from the same prices gives about 0.754, but the golden blend return is 0.9836.
  - A probe of the blend run shows 53 BTC buys, 42 BTC sells, 38 SPY buys and 37 SPY sells. The blend is a periodically rebalanced fund, not buy-and-hold (F1).

## Checklist

| Item | Result | Evidence |
|---|---|---|
| Lookahead (signal t, fill t+1) | Y | `driver.py` step order; `test_backtest_prefix.py:37`; `tests/strategy/test_no_lookahead.py:116` |
| Survivorship | n.a. | fixtures are a fixed universe; membership tests exist in `tests/unit` |
| Costs both directions | Y | G4 fees re-derived (8.11, 5.22); equity 5 bps and crypto 10/10 bps via `default_config` |
| Accounting invariants | Y | `tests/properties/test_engine_invariants.py` |
| Determinism / golden pinned | Y, with F4 | `tests/golden/G*.json` plus `config_hash` and `data_fingerprint`; byte-identical report test |
| Decimal + explicit rounding | Y | `trades.py` uses `money_context` and `PRICE_Q` |
| Benchmark reported | Y, with F1 | `engine.py` `run_with_benchmark` |
| Offline tests | Y | fixture provider; net guard |
| Restart/idempotency | n.a. | runner is not in this wave; `TargetBook` state round-trips (`driver.py:state/from_state`) |

## Findings (ranked)

| # | Severity | file:line | Issue | Required fix |
|---|---|---|---|---|
| F1 | major | `engine.py:371` (blend), `_bench_config` | SPEC-002 section 7 says "static blend (sleeve class weights, cash included)". The code runs a 70/20/10 fund through the engine, which rebalances (about 170 fills). G4 pins `blend` at 0.9836 against 0.754 for a true static blend. The rebalanced blend is the harder benchmark, so gate 3 is not flattered, but the label and spec are wrong, and the sign-off number depends on it. | PM decision: either (a) keep it, rename it `rebalanced_blend` and amend the spec, or (b) build the blend as a weighted sum of the SPY and BTC hold curves plus cash. Option (a) is recommended: it is conservative, and no golden re-pin is needed beyond the key name. Gate 3 should use SPY and BTC as well as the blend. |
| F2 | major | `validation.py:392-393` | The walk-forward window arithmetic is unpinned. Mutants `te_lo = s+train-1` (test overlapping train) and `tr_hi = s+train` (train reaching into test) both survive. IS containment is tested, but train/test disjointness is not. | Add a test that the windows from `walk_forward` satisfy `train_end < test_start` and `test_end < next test_start`, and that the dates equal the exact calendar indices. Add anchored mode too. |
| F3 | major | `validation.py:141,343`, `GateConfig` | Gate 2 thresholds are unpinned. `wf_sharpe_frac` 0.6 -> 0.5 and `Sharpe > 0` -> `>= 0` both survive. D10 values are only checked by reading. | Add boundary tests: 3 of 5 windows positive passes, 2 of 5 fails (60%); a zero Sharpe window is not positive. Assert `GateConfig()` equals the D10 numbers (10y, 5y, 4 windows, 0.6, 0.6, 1.0, 30, 0.5, 1.5, 90). |
| F4 | major | CI budget | See the split recommendation below. | Mark and split. |
| F5 | minor | `driver.py:312` | The decision-bar warmup bound `i >= warmup_bars - 1` is unpinned (off-by-one mutant survives). It does not leak data, it only shifts the first trade by one bar. | Add a test that the first decision is exactly at index `warmup_bars - 1`. |
| F6 | minor | `metrics.py:80` | `rf_p` is not tested with `rf != 0` (linear `rf/p` survives). Spec 7 uses the compounding form. | Hand-calc test with `rf_annual=0.04`. |
| F7 | minor | `validation.py:517-519` | `_bounds` treats a whole-valued float parameter (for example `stop_mult: 3.0`) as an integer and uses floor/ceil instead of exact 0.8v and 1.2v (spec section 8). It is invisible today because `stop_mult` is 2.5. The "low uses ceil" mutant also survives. | Decide integer-ness from the declared parameter type or `parameters` source, or document it. Add a test with a float 2.0 and an int 5. |
| F8 | minor | `validation.py:644-646` | The freeze is advisory because the caller can freeze right before the call. The cache key is `(sid, hash)` and ignores `cut`. | Include `cut` in the key; note in the docs that the structural guard is `DataView`. |
| F9 | minor | `validation.py` `collapse_reasons` | The drawdown and sign tests still apply when base Sharpe <= 0 (the spec calls the sweep informational then). `informational` is reported but not used in `passed`. | Align the code and spec text. |
| F10 | nit | `engine.py` `_align` | `fillna(fill)` is unpinned. It only matters if a benchmark starts later than the fund. | None. |
| F11 | info | `tests/properties/test_engine_invariants.py:360`, `test_risk_properties.py:235` | The I10 "0.05 slack" is `Decimal("0.05")` currency units (5 cents), not 5 percentage points. It is acceptable for rounding on a 100k portfolio. | None. A relative tolerance such as `1e-9 * equity` would be tidier. |

## Benchmark fairness
Same window, same start (warmup is enforced, so the benchmark and fund both start on `cfg.start`), and the same costs and capital. Both fill at the first bar after start (2019-01-03 in G4). `_bench_config` relaxes the asset cap, the crypto cap and the kill switch (they are fund policy, not market costs). That is acceptable, and the mutants "bench caps kept" and "bench kill kept" are caught. The benchmark is aligned onto the fund's index by forward-fill, and P is the fund's P. The blend issue is F1.

## Gate correctness
- Thresholds match D10 and spec section 8 by reading `GateConfig`: 10y/5y, at least 4 windows, 0.6/0.6, drawdown multiple 1.0, at least 30 trades, floor 0.5, drawdown 1.5x, 90 days. Tests catch `>=` vs `>` for gate 3, gate 4 and the sensitivity multiples. Gate 2 fractions are unpinned (F3).
- Walk-forward windows: 350/150/150 and 365/180/180 are test-only shrinks to fit the 7-year fixtures, and the shrunk configuration cannot pass gate 1 anyway (about 6y of history against 10y).
- **Recommendation for real data:** keep `WalkForwardConfig.defaults()` at 756/252/252 and 548/182/182 as the only production defaults. The arithmetic works at the minimum history: 10y equity gives about 1764 IS bars, which is 4 windows; 5y crypto gives about 1277 IS bars, which is 4 windows. Shrinks must be explicit arguments in tests and must never enter `GateConfig` or the CLI default. `run_validation` should raise (or mark gate 2 failed with a clear reason) when IS bars are below `train + 4*test`, not silently produce fewer windows. Name the shrunk configs in the test support as `FIXTURE_WF_*`.

## Golden sign-off
**YES.** `docs/reviews/golden/G-initial.md` is confirmed for G1-G4 as first-run pinned values: seven G4 fills were re-derived exactly from raw bars, the fees are correct, and the SPY and BTC benchmark returns are recomputed independently. Conditions:
1. Record that the G4 `blend` entry is a rebalanced fund (F1). If the PM chooses option (b), that key changes and needs a new note.
2. The G1-G3 benchmark policy (caps relaxed) is as described in the note.
3. Replace "awaiting the quality-lead hand audit" in the note with "signed off by quality-lead, 002 audit". The audit does not edit that file.

## Mutation and test-strength review
A real mutant run on a scratch copy of `backtest/*.py` (37 mutants defined; the test set was `tests/backtest`, `gate`, `integration`, `lookahead/test_backtest_prefix.py` and `golden`, with `-x`).

| Mutation | Caught by | Verdict |
|---|---|---|
| sharpe ddof 0; sqrt(P) removed; sortino divides by N-1 | `test_metrics_handcalc.py` | Caught |
| CAGR 365 days; drawdown peak = first value; win rate `>= 0`; time in market `>= 0`; crypto P = 252 | `test_metrics*.py` | Caught |
| split fraction; gate 3 `>`; gate 3 drawdown multiple; min trades `>`; sensitivity floor and drawdown multiple | `tests/gate/*` | Caught |
| IS view includes the cut; peeking disabled; OOS re-run; freeze not required; OOS start dropped | `test_holdout_discipline.py` | Caught |
| submit-only-on-decision; submit every bar; start strict; net PnL ignoring fees; exposure x2 | `test_driver.py`, `test_trades.py` | Caught |
| benchmark caps kept; benchmark kill switch kept; blend weights; benchmark window shortened | gate, golden G4, e2e tests | Caught |
| `wf_sharpe_frac` 0.5 | nothing | **Survives** (F3) |
| walk-forward `Sharpe > 0` -> `>= 0` | nothing | **Survives** (F3) |
| walk-forward test window overlaps train | nothing | **Survives** (F2) |
| walk-forward train reaches into test | nothing | **Survives** (F2) |
| `_bounds` low uses ceil | nothing | **Survives** (F7) |
| decision warmup off by one | nothing | **Survives** (F5) |
| `rf_p` linear instead of compounding | nothing | **Survives** (F6) |
| `_align` `fillna` 0 | nothing | **Survives** (F10, near-equivalent) |

A "gate 3 sharpe equal passes" test and a "101% of benchmark drawdown" test exist, so the gate 3 boundaries are good.

## Test gaps
- **G1 (major):** window disjointness and exact calendar indices for `walk_forward` (anchored and rolling) in `tests/gate/test_validation_gate.py`.
- **G2 (major):** a `GateConfig()` defaults test against D10, and gate 2 boundary tests.
- **G3 (minor):** warmup first-decision index in `tests/backtest/test_driver.py`; `rf != 0` in `test_metrics_handcalc.py`; whole-valued float and integer `_bounds` cases.
- **G4 (minor):** a static-blend reference test, once F1 is decided.

## Suite runtime and CI split
The default job `-m "not slow and not golden"` ran 2008 tests in 74 s (while other jobs were running), and `-m "slow or golden"` ran 33 tests in 82 s. Recommended:
1. PR job: `uv run pytest -m "not slow and not golden"` (inside the 90 s budget with margin; add `-n auto` if `pytest-xdist` is accepted as a dev dependency).
2. A second job, on merge to main and nightly: `uv run pytest -m "slow or golden"` with the 20 s default timeout overridden where the tests already set markers.
3. Keep `test_golden_change_note.py` and `test_golden_matches` in the PR job only if they stay under 20 s in total. The note-listing test is cheap, so it should not be marked slow. G4 alone takes 10.5 s.
4. The slowest tests are `test_tuner_never_receives_a_bar_past_the_split...` (13 s), `G4` (10.5 s) and the page test at 7 s. Share `h.load_bars()` and `run_golden` across modules by caching them at session scope.

## Re-audit needed? No
Re-audit only if F1 option (b) is chosen (G4 benchmark re-pin) or if F2/F3 fixes change `validation.py` logic rather than just adding tests.
