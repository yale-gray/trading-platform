# Audit: fractional shares (`EngineConfig.equity_qty_quantum`, default 1e-6)
Date 2026-10-22. Auditor: quality-lead (Lean). Scope: uncommitted diff (money, ledger, portfolio, engine, sizing, checks, limits, broker, backtest `config_hash`, devconsole sandbox, spec 003 edits, golden re-pin note). Full suite on the diff: 1411 passed (portfolio, risk, sim, golden, devconsole, unit, properties).

**Verdict: PASS-WITH-NOTES.** No blockers, no majors. Two test-coverage gaps (mutation survivors) and two minors.

## 1. Whole-share assumptions
- Buy DOWN: `money.py:214-218` `buy_qty` floors via `_quantize(..., ROUND_DOWN)` on `qty_quantum(...)`; used by `sizing.py:105`, `limits.py:113`, `broker.py:354`, `sandbox.py:93`. OK.
- Sells are validated, not rounded: `check_sell_qty` (`money.py:221`), `checks.py:275`, and the ledger fold (`ledger.py:191`, `check_qty_grid`). OK.
- Cash shrink at fill: `broker.py:339-357` uses the config quantum for the guess and the walk-down step. OK, but untested (see 4).
- R6 min size: `checks.py:~255` uses `q2(qty*ref)` on the grid-rounded qty; R3/R4/R5 shrink all pass `eq_q`. OK.
- I4 in verify/fold: `portfolio.py:248` and `ledger.py:191` use `state.equity_qty_quantum`. OK.
- No `Decimal(float)` in the diff. The quantum field rejects floats (`config.py:101`) and must be a power of ten in (0, 1] (`config.py:106-113`). Math runs in `money_context()` (sizing, limits, broker shrink, ledger notional). OK.
- Minor: `devconsole/views/cost_sandbox.py:97` help text still says "Stocks and ETFs trade in whole shares". Stale UI copy.
- Minor: `SHARE_Q` and `floor_shares` (`money.py:36,195`) are now unused by production code. They are harmless dead API.

## 2. Back-compat
- An old `EngineState` JSON without the field loads with the default, and a whole-share ledger replays on the 1e-6 grid and continues fractionally. Covered by `test_old_engine_state_without_the_field_restores_and_continues`.
- An old `PortfolioDef` JSON loads with the default. Covered.
- `Ledger()` / `replay()` with quantum `None` enforces the 8 dp grid, so crypto-grade only. That is lenient but sane: qty > 0 and 8 dp are still enforced, and the engine always passes a quantum (`engine.py:155,185`).
- Live vs replayed ledger: `PortfolioEngine` requires `state.config == config` (`engine.py:147`), so a changed quantum fails loudly as `StateCorruptionError`. `verify()` replays with `state.equity_qty_quantum` (`portfolio.py:197`). No other `Ledger(` / `replay(` call exists in `src`, so no path where they disagree. Note: the safety net is the engine's own config pass; a bare `Portfolio()` has quantum `None`.

## 3. Goldens
`git diff` on `tests/golden/G1-G4.json` shows exactly one changed line each, `config_hash`. The harness pins quantum 1 (`harness.py:119`). The re-pin note lists old and new hashes and is PM-approved. `config_hash` payload includes `str(eng.equity_qty_quantum)` (`backtest/engine.py:168`); removing it fails G1-G4. PASS.

## 4. Mutation probe (scratch copy; the repo is untouched)
Caught: quantum ignored in `buy_qty` (6+ failures); ignored in `target_quantity` only; ROUND_DOWN to HALF_UP; `check_sell_qty` grid removed; ledger fill grid removed (2 failures); I4 in `verify` using None (1 failure); R5 `qty_within_notional` missing quantum (golden + test); `config_hash` field removed (G1-G4).
**Survived (gaps, not defects):**
- **N1** `broker.py:354` `_shrink_buy` guess rounded with whole shares, and `broker.py:351` step = 1. All broker shrink tests use quantum "1" (`test_broker_gaps.py:37`). Add a fractional shrink test: cash 1 cent short at 1e-6.
- **N2** `engine.py:155` `Ledger()` with no quantum, and `portfolio.py:197` `replay()` with no quantum. Nothing asserts an engine rejects an off-grid fill at the engine level. A property or scenario test with a hand-injected off-grid fill would pin the wiring.

## 5. Precision edge (run against the ledger)
- BUY 1e-6 at price 1e-8: notional q2 = 0.00 and the ledger rejects it ("zero-notional BUY", `ledger.py:162`). Such a position cannot be created, which is correct. R6 `min_notional` also rejects it earlier.
- A 1e-6 residual (left after a partial sell) at price 100 (proceeds 0.0001 -> 0.00): the full exit is accepted, and the partial zero-proceeds SELL is rejected (`ledger.py:144`). The residual is always exitable, because `check_target_weights` full-exit sells skip the min-size check. A 3e-6 residual also exits whole.
- Off-grid sell 5e-7 is rejected (oversell message, so the error text is slightly misleading but safe).

## Recommendation
Merge. Follow-ups: N1 and N2 tests, and the stale sandbox help text.
