# Audit 003-wave3: Wave 3 engine (risk, SimBroker, portfolio engine, rebalancer, evaluator)

- **Auditor:** quality-lead
- **Date:** 2026-10-21
- **Code (uncommitted working tree, 3,869 lines):**
  - `src/tradebot/risk/`: `sizing.py` 100, `limits.py` 143, `checks.py` 269, `stops.py` 134, `killswitch.py` 112, `config.py` 50
  - `src/tradebot/sim/broker.py` 350
  - `src/tradebot/portfolio/`: `engine.py` 602, `rebalance.py` 302, `state.py` 81, `ledger.py` 492, `models.py` 293, `portfolio.py` 254, `config.py` 215
  - `src/tradebot/strategy/`: `engine.py` 432, `signals.py` 40
- **Tests:** `tests/risk/` (98 cases), `tests/sim/` (134), `tests/portfolio/` (682, incl. `test_scenarios.py` 8 scenarios, present and passing), `tests/unit/test_{fills,risk,money,indicators}_handcalc.py`, `tests/lookahead/` (55), `tests/strategy/` (124). Wave 3 directories: 1,252 passed. Whole suite: 1,782 passed. `ruff check src tests`: clean. mypy: not installed in `.venv`, not run.
- **Against:**
  - SPEC-003 §1-§10, I1-I18, AC1-AC11
  - SPEC-002 §1-§2a, §5 and the evaluator semantics
  - ADR-0001 D3, D5, D7, D8, D10 and the §5 addendum (A5: a trip drops the pending plan)
  - CLAUDE.md hard rules
- **Verdict: PASS-WITH-NOTES**

I found no blocker in the code. Across 208 randomized engine runs (52 seed/mode pairs, each run twice or more) (including trips, resets, rebalances, stops and restores), every invariant I could check from outside held. Runs were byte-identical across restore, ambient decimal context and `PYTHONHASHSEED`. Nothing fills on its creation bar, and stops are close-based and fill at the next open.

The verdict is not a clean PASS for two reasons:

1. **Two real Major code defects.** Both are latent: a compiled strategy reused across `prepare()` calls gives wrong `bars_held`, and a flat target does not cancel a pending BUY.
2. **The tests are far weaker than the code.** Of 167 mutants, **91 survive** (76 killed). After removing 14 equivalent or redundant mutants, 77 genuine survivors remain, an effective kill rate of about 50%. The surviving gaps include safety controls: kill-switch stickiness, the running peak, restore of a tripped state, and the whole rebalance retry path. The evaluator has no semantic tests at all, only causality tests.

Merge is acceptable for the code. Wave 4 (`BarDriver`) should not be treated as trustworthy until F1 and F2 are fixed and the G1-G6 tests land. Those tests are small and cheap; scratch recipes for all of them exist (see Evidence).

## Scope

In scope: every file listed above, all Wave 3 tests, `tests/portfolio/test_scenarios.py` (landed), and the lookahead tests that touch the evaluator.

Not in scope:
- `portfolio/money.py` (audited in 003-T1) and `sim/orders.py`/`sim/costs.py` (only exercised through mutants CO01-CO06)
- `BarDriver`, `TargetBook`, `backtest/*` (Wave 4, not yet written)
- the runner and platform
- data layer and indicator math
- `portfolio/config.py`, `ledger.py`, `models.py` and `portfolio.py` beyond how the engine uses them (re-audit of Wave 2 findings)

Everything below was done on throwaway scripts and a scratch copy. The repo was not modified except this report.

## Evidence

- `pytest tests -q`: 1,782 passed. Wave 3 dirs (`unit risk sim portfolio strategy lookahead`): 1,252 passed in about 19 s. `ruff check src tests`: clean. `src/tradebot/{sim,risk,portfolio}` have no `float` outside the `Bar`/`weights` boundary, no wall-clock, no `random`/`uuid`, no network import (grep).
- **Randomized engine fuzz** (`scratchpad/probe/fuzz.py`, `fuzz2.py`). Each run is 260 bars over SPY/QQQ/IEF/BTC/ETH:
  - **Inputs:** missing bars (3%), missing vol20/atr14 (5%), weekends for equities, random target weights, stops (ATR mult 2.5) on mean_reversion and crypto_trend, monthly rebalances, optional crash window, random reset after a trip.
  - **Checks after every `process_bar` and `submit_target_weights`:** `verify()`; I1-I4 and I18 recomputed independently; I6/I7 (via `verify`); I8 (fill_asof > created_asof, fill price vs open); I9; I11; I12 trip equality; I13 ratchet per holding episode; I14; at-most-one STOP SELL per position; no oversold pending SELLs; I10 at acceptance (including the crypto 30% cap); prefix stability of the ledger.
  - **Scale:** seeds 0-39 (fuzz) and 100-111 (fuzz2), each with and without crash.
  - **Variants:** every seed also ran a variant with ambient `prec=5` and `export_state`/JSON/restore every 7 bars, compared byte for byte to the uninterrupted run.
  - **Result:** 0 failures. Runs contained about 400-600 FILLs, 22-30 STOP_TRIGGERED, 5-8 REBALANCE_PLANNED and one trip+reset in the crash runs.
- **Ambient context probe** (`ambient.py`): a fresh engine built and run under 5 rounding modes x precisions {3, 7, 60} with narrow Emax/Emin and traps: identical `export_state` JSON in all 15 combinations. `PYTHONHASHSEED` 1, 2 and 12345: same sha256 of `export_state` (386 ledger events).
- **Hand probes** (`p1`-`p12`):
  - **Pipeline and exposure:** SELL under missing vol or price, cross-sleeve pending BUYs (SPY in core + mean_reversion: 200 + 50 shrunk to the 250-share room), I10 drift (SPY at 25.01% after a fill; no force-sell, further adds blocked).
  - **Stops and kill switch:**
    - stop equality, ratchet and lock/re-arm;
    - a trip at exactly 80,000.00 (a price scan shows 80,000.00 trips, 80,000.01 does not);
    - idempotent repeated target call; stickiness after recovery and restore.
  - **Rebalance and restore:** retry/INCOMPLETE (3 bars, `REBALANCE_INCOMPLETE`, transfers net to 0.00); 22 state-tamper variants (below).
  - **Evaluator:** rotation, IEF fallback, the NaN gap and `prepare` reuse.
- **Mutation run** (scratch copy `scratchpad/mut`, never the repo): 167 mutants plus 5 combination mutants, against `tests/{unit,risk,sim,portfolio,strategy,lookahead}`. Mutants live in `scratchpad/mutants.py`. Survivors were checked individually and several were confirmed to change behaviour with a direct probe.
- Files, all under `/private/tmp/claude-501/-Users-yalegray--claude-spares-spare-zQEjyS/6ecda440-b68a-41ac-8d83-34777a2ff655/scratchpad/`: `probe/fuzz.py`, `probe/fuzz2.py`, `probe/p1..p12.py`, `probe/ambient.py`, `mutants.py`, `runmut.py`, `combo.py`, `fuzzmut.py`, `applyrun.py`. They are throwaway but good starting points for the T12 hypothesis tests. The PM may want them copied to a tracked location.

## Checklist (Y / N / n.a. with file:line)

| Item | Result | Evidence |
|---|---|---|
| Lookahead: nothing fills on its creation bar | Y (engine); broker-level guard unpinned | engine rejects `asof <= last` (`engine.py:278`), broker filters `created_asof < asof` (`broker.py:261`); fuzz I8 clean; mutant BR01 survives (G4) |
| Lookahead: fills at the bar's own open only | Y | `broker.py:271`; BR06 (fill at close) killed by `tests/unit/test_fills_handcalc.py` |
| Lookahead: stops close-based, fill next open | Y | `stops.py:59`, `engine.py:385-410`; probe p3: trigger d6, STOP fill d9 at open x (1 - 5bps) |
| Lookahead: evaluator uses data <= t only | Y | `strategy/engine.py:99-102,152-158,336`; `tests/lookahead/test_strategy_truncation.py` (truncation and future poisoning, with and without `held`) |
| Survivorship | n.a. here | handled by NaN-means-ineligible (`strategy/engine.py:359,362`); universe membership is 001/driver |
| Costs both directions | Y | `costs.py:20-27`, `broker.py:288-313` (fee booked on BUY and SELL); CO01-CO06, BR09-BR11, BR13 killed |
| Accounting invariants tested | Partial | `verify()` and scenario tests hold; no engine-level property tests until T12; see G2 |
| Determinism / golden pinned | Y by probe; N pinned | fuzz, ambient and hash-seed probes pass; no test pins a long-run hash or ambient context at engine level (G8) |
| Decimal + explicit rounding | Y | every module runs in `money_context()`; ambient probe passes |
| Benchmark reported | n.a. | Wave 4 |
| Offline tests | Y | no network/env/key import under `sim`/`risk`/`portfolio`; `test_broker_bar_atomicity.py:68` asserts the import |
| Restart / idempotency | Y by probe; N pinned | restore-and-continue byte-identical in fuzz; engine-level tests cover only one scenario (G2) |

## Findings (ranked)

No blocker. Two majors, then minors.

| # | Severity | file:line | Issue | Required fix |
|---|---|---|---|---|
| F1 | **Major** (latent until Wave 4) | `strategy/engine.py:259-262, 264, 275-304, 397-399` | `prepare()` overwrites `self.calendar` (line 286) and `self._flags_cache` (303). `_bars_held` (398) and `is_decision_bar` (261) read that instance state instead of the `Features` they are given. Reusing one compiled object across runs, windows, OOS split or walk-forward silently changes results. Probe p8: `mean_reversion` evaluated on full-history features, then `prepare()` on a 300-bar-shorter window, then re-evaluating the first features: `bars_held` 2 became 302, flipping `hold` to `exit condition`. `is_decision_bar` also answers for the last prepared calendar. The existing truncation tests recompile per call, so they cannot see this. `evaluate` is documented as pure (SPEC-002 §1). | Make `evaluate`/`_bars_held`/`is_decision_bar` depend only on the `Features` passed in (use `feats.calendar`, `feats.decision_flags`); make `prepare` side-effect free, or document and enforce one compiled object per run. Add a reuse test (prepare A, prepare B, evaluate A equals fresh). Until fixed, the BarDriver must create a fresh `compile_strategy` per run and per window. |
| F2 | **Major** | `risk/checks.py:173-179, 186-196` (with `engine.py:442-488`) | A flat or lower target does not cancel or net a pending BUY. Level-triggered "hold exactly this" breaks whenever a BUY is still pending because its symbol had no bar (up to `max_pending_bars`). Probe p12: BUY 200 SPY pending (no SPY bar next day), strategy submits `{}`: the call returns no orders and no rejection, the BUY stays pending and fills later, and the sleeve holds SPY it asked to be flat in. For a monthly sleeve the position can stay until the next decision. It also fills under an active stop lock. | In `submit_target_weights`, when `target_qty < projected` and the sleeve has pending BUYs for the symbol, cancel (or shrink) those BUYs first, then recompute. Test: pending BUY + `{}` leaves no BUY. Mutants CK13/CK14/CK17/CK18 all survive today and would be killed by it (G6). |
| F3 | Minor (judgement call, safe direction) | `strategy/engine.py:416` vs `:413`, docstring `:9-11`, SPEC-002 §2 | Hold-while (omitted `exit`) exits when entry is not confirmed True. A held symbol whose feature goes NaN (data gap) is flattened. Probe p9: crypto_trend with one BTC bar dropped exits that day. SPEC-002 §2 reads "`not` of a NaN comparison is also False", which by the letter holds. An explicit `exit` that is unknown holds (`_true(...)` False, line 413), so the two forms disagree. **Judgement: safe.** Exiting on unknown is the conservative, cash-ward direction, and the stop and `max_hold` backstop the opposite case. Cost: a data blip costs a crypto round trip of about 40 bps (10 fee + 10 slip each way) plus churn. | Record the deviation in an ADR-0001 D10 addendum or SPEC-002 §2a. Decide whether the 001 data-quality gate makes mid-series gaps impossible. Pin the behaviour with tests (SE05, SE11, SE12 survive). |
| F4 | Minor | `portfolio/engine.py:164-228` | Restore verifies cash, positions (incl. `entry_asof`), `next_seq`, the order book and ledger replay, but not the other checkpoint fields. Probe p7 (22 tamper variants): ACCEPTED silently were `next_order_n` lowered (the next orders overwrite `O00000001`/`O00000002` in the book, and `verify()` fails only afterwards), `next_transfer_n` lowered (duplicate transfer id raises mid-`process_bar`), `kill_state.killed` flipped either way, `kill_state.peak_equity`, an order's `fill_price`/`fee` vs its FILL, `last_close`, `last_processed_asof` moved backward, `pending_waited`, `stop_state`/`stopped_out`, `last_rebalance_month`. `killed` is derivable from the `KILL_SWITCH_TRIPPED/RESET` annotations, so a checkpoint saying `killed=False` after a trip silently re-enables BUYs. Corruption of the ledger, cash, positions or `qty_filled` IS caught (`StateCorruptionError` or `ValidationError`). | Cross-check on restore: `next_order_n > max(order id)`, `next_transfer_n >= max(transfer id)`, `killed` vs the last TRIPPED/RESET event, `last_processed_asof >= max(ledger asof)`, order `fill_price`/`fee` vs FILL. Add tamper tests (G2). |
| F5 | Minor | `risk/stops.py:55-59`; `engine.py:290` | `atr14 == 0` is accepted (`to_ratio(a, PRICE_Q)` allows 0). Then `candidate == high_water_close`, and `close <= stop_level` is true on any close at the high-water mark, including the fill bar. Probe p4: `atr14=0` on the fill bar produced an immediate STOP exit. Real ATR is rarely 0, but a flat series or a sub-1e-8 ATR rounds to it. | Treat `atr14 <= 0` as `None` (level unchanged), or reject it at the engine boundary. Add a test. |
| F6 | Minor | `portfolio/engine.py:454-459` vs `risk/checks.py:119-122`, `engine.py:468` | `submit_target_weights` re-arms stop locks (`note_target`) before the call can still raise (`ValueError` for a symbol outside the sleeve's asset classes, or an unknown symbol). Probe p12: a failed call still cleared the lock. | Compute `note_target` only after `check_target_weights` succeeds (or on a copy). |
| F7 | Minor (PM policy) | `risk/checks.py:124-150` | R1 `NO_DATA`/`NO_VOL_DATA` also suppresses reductions of a held symbol whose target stays above 0: nothing is sold or trimmed (probe p1: trim SPY with `vol20=None` produced `NO_VOL_DATA`, no SELL). **Exits are never vetoed.** A target of 0 sells with `vol20=None` and with no bar at all (stale mark), and under a tripped switch (probes p1, p4, and CK28 killed). The spec exempts R0 and R3-R5 only, so this conforms. But a rebalance sell-down is delayed by a vol blip and can end `REBALANCE_INCOMPLETE`. | Decide: let reductions use the stale reference price and skip the vol requirement (a trim does not need sigma). Test either way (CK25, CK27 survive). |
| F8 | Minor | `portfolio/engine.py:492-497` | Public low-level `submit_orders` enforces only the kill-switch BUY block. No R3/R4/R5, stop lock, or min-size. SPEC-003 §3 and §6 say risk checks run in `submit_target_weights`; the low-level path is documented for the rebalancer, stops and tests, but it is public. The kill-switch guard on it is untested (EN11 survives). | Restrict BUYs to `MANUAL` origin with a docstring warning, or route BUYs through the same checks. Add an I11 test for the low-level path. |
| F9 | Nit | `sim/broker.py:246-251, 298-299`; `risk/checks.py:222-247` | (a) `process_bar` does not check `bars` key == `Bar.symbol` or OHLC consistency (data layer's guarantee). (b) A zero-quantity SELL is rejected with `INSUFFICIENT_CASH`. (c) One symbol can get both `CASH_SHRUNK` (accepted 0) and `BELOW_MIN_SIZE` rejections (probe p10). (d) Initial DEPOSITs are stamped `asof = date.min` (`engine.py:85,156-162`), so ledger `asof` is not monotonic; `backtest/trades.py` must tolerate it. | none, info only; consider a `NO_CASH` reason and one rejection per symbol. |
| F10 | Info | `portfolio/engine.py:277-359` | Inputs are validated before any mutation, but steps C-F can raise after step A has run (for example a transfer-id collision). The engine is then half-processed and a retry gets `StaleBarError`. ADR D8 covers it (the runner restores from the last committed checkpoint), but the runner must treat any exception inside `process_bar` as "discard engine, restore". | Record in the runner contract (004). |

### Items checked and found correct

- **Order A-F (spec §1):** SELLs fill before BUYs (`broker.py:262`), each in `order_id` order. Transfers run at step C of the bar after the plan. Stops at D, snapshot and kill switch at E, rebalance decision at F. Seeding the month on day one avoids a day-one plan (`engine.py:335-336`).
- **Risk pipeline R0-R6:** order and shrink-vs-reject are as specified (`checks.py:210-247`). R3 and R4 use portfolio equity at the last close. Pending BUY notionals of all sleeves accumulate within the call, so SPY in two sleeves is capped at 25% across both (probe p2: 200 then 50 shares). Pending SELLs are not credited. **I10 semantics:** the cap holds at acceptance (limit minus at most one share); fill slippage and later drift may exceed it. Probe: SPY 25.01% after fill, 28.6% after a +20% move, no force-sell, further BUYs blocked `MAX_ASSET`.
- **Stops:**
  - **Ratchet (I13):** hwc starts at the fill-bar close; the ratchet never loosens when ATR rises; ATR `None` leaves the level and still tests it.
  - **Trigger and lock:** triggers at exact equality (`close <= stop`). A pending STOP is never duplicated (probes p3 and fuzz2). The lock holds through target > 0, re-arms on a flat submit, and survives restore (`ST11` killed).
  - **Fills:** full-position STOP SELL; fills next open with slippage (89.955 on a 90 open).
- **Kill switch:**
  - **Trip and stickiness:** trips at exactly 20.00% (equity 80,000.00; 80,000.01 does not trip). It is exact integer-cents arithmetic (`equity <= peak*(1-t)`). The peak is running (probe p11: peak 324,987.50, then a trip at -23%). It is sticky (no auto-reset after recovery) and `tripped_now` fires once.
  - **Effects on pending work:** the trip cancels pending BUYs and drops the plan (`engine.py:329-332`). Rebalance is frozen (`rebalance.py:169,232`; `engine.py:339`).
  - **Reset:** `reset_kill_switch` raises when not tripped, sets peak to the last observed equity, books `KILL_SWITCH_RESET` and never re-trips on the same equity.
- **Rebalancer:**
  - **Transfers:** atomic OUT+IN via `append_transfer` (`rebalance.py:266`), net to 0.00 per `transfer_id` (probe: nets `{0.00}`), largest-remainder splits.
  - **Retry and sizing override:** retry for 3 step-C bars then `REBALANCE_INCOMPLETE` and drop (probe p10), partial transfers pro-rata (15.01 / 30.02 / 30.02 of 75.05 cash), `sizing_equity` override used while pending. Cash-short givers move `min(surplus, cash)`.
  - **After completion:** probe p10 ended core 0.4999 / cash 0.1000 / crypto 0.2000 / mean-rev 0.2000.
- **Evaluator:**
  - **Rotation:** rank ties are deterministic; `pre_rank` filters first (eligible set 14 of 16 on 2021-05-03); the IEF fallback is unconditional (the shipped YAML has no `require`, per ADR D10). 0 eligible gives IEF 1.0; held-not-selected gives flat.
  - **`bars_held`:** 0 on the fill bar, +1 per calendar bar (`:397-399`). Causality is covered by truncation and future-poisoning tests.

## Mutation and test-strength review

Real mutant run on a scratch copy. Test selection: `tests/{unit,risk,sim,portfolio,strategy,lookahead}`, `pytest -x`. A mutant is **killed** when any test fails. Results: **76 killed, 91 survived, 0 harness errors**. One kill was a tmp-dir collision from parallel runs; EN17 was re-run alone and is genuinely killed. Five combination mutants were added for redundant guards.

### Killed (76, grouped; representative killing test)

| Group | Mutants killed | Killed by |
|---|---|---|
| Fill pricing, fees, slippage (`costs.py`, `broker.py`) | CO01-CO06 (buy floor, sell ceil, slippage sign x2, fee half-even, notional floor); BR06 (fill at close), BR07, BR08, BR09-BR11 (fee/slip zeroed), BR13 (equity costs for crypto), BR15, BR16 | `tests/unit/test_fills_handcalc.py` (worked examples), `tests/sim/test_costs.py:99,111`, `tests/sim/test_broker_book.py` |
| Risk pipeline `checks.py` / `limits.py` / `sizing.py` | CK01 R0 off, CK02 stop lock off, CK03/CK06 zero-room, CK04 R3 shrink off, CK07 R4 shrink off, CK08 R5 shrink off, CK11/CK22 min-size off, CK12, CK16, CK19, CK20 sub-share, CK23, CK24 sizing-equity ignored, CK26, CK28 exits blocked when killed; LM01-LM05, LM07, LM09, LM10; SZ01-SZ05 | `tests/risk/test_checks.py` (262, 270, 203, 224), `tests/risk/test_sizing.py`, `tests/unit/test_risk_handcalc.py` (cap, tilt and AC2 cases) |
| Stops | ST01 `<` vs `<=`, ST02 no ratchet, ST03, ST05, ST06, ST07 re-arm always, ST08 never re-arm, ST09, ST10, ST11 lock not persisted | `test_risk_handcalc.py:223,249`, `test_scenarios.py::test_stop_out_fills_next_open_then_lock_until_flat_then_rearms` |
| Kill switch | KS01 strict `<` (trip at equality), KS04 reset keeps flag, KS05 reset keeps peak, KS06, KS08 | `test_risk_handcalc.py:281,294`, `test_scenarios.py::test_kill_switch_*` |
| Engine wiring | EN03 pending BUYs kept on trip, EN07, EN08, EN16 stops skipped, EN17 observe skipped, EN18, EN22, EN23 plan not persisted, EN25, EN26, EN33, EN34 | `test_scenarios.py`, `test_risk_handcalc.py`, `tests/sim/test_broker_book.py::test_engine_restore_at_bar_15_is_byte_identical` |
| Rebalance | RB01 month trigger, RB04 surplus sign, RB05 sizing override, RB16 pro-rata | `test_risk_handcalc.py:329`, `test_scenarios.py::test_month_boundary_rebalance...` |
| Combos | kill-freeze (EN04+RB08), all four kill guards, stale-bar guards (EN15+BR08) | `test_scenarios.py::test_kill_switch_blocks_buys_...`, `test_broker_book.py` |

### Survivors: 91, classified

"Genuine" means the mutant changes behaviour (checked by reading, and for CK17, KS02, CK25, RB12 by running a probe on the mutated source) and no test notices.

| Mutant | Area | Verdict | What it means / the missing test |
|---|---|---|---|
| **SE01-SE24 (all 24)** | `strategy/engine.py` evaluator | **Genuine x23, Equivalent x1 (SE20)** | The evaluator has no semantic tests. Survivors include: rank direction (SE01), `pre_rank` off (SE02), fallback weight (SE03), top-k+1 (SE04), hold-while unknown holds (SE05), `bars_held` off-by-one (SE06), schedule flag inverted (SE07), `>` as `>=` (SE08), `>=` as `>` (SE09), cross strictness (SE10), `not` of unknown (SE11), `any/all` unknown (SE12), held-not-selected not flattened (SE13), `reindex().ffill()` (SE14), weight=1.0 (SE15), exit priority (SE16), fallback `require` ignored (SE17), entry check inverted (SE18), not-in-universe (SE19), `pos` read (SE21), `post_rank` (SE22), `force_decision` (SE23), NaN scores (SE24). SE20 is equivalent: `i<1` is already covered by the NaN return for index -1. |
| KS02 | peak never raised | **Genuine** | Verified on mutated source: peak stays 100,000 and a 23% drop from a 324,987 peak does not trip. Only trips from initial capital are tested. |
| KS03 | re-trip every bar | **Genuine** | Repeated `KILL_SWITCH_TRIPPED` events and cancels. |
| KS07, KS10 | killed flag / peak lost on restore | **Genuine** | AC5 "survives export_state/restore" is untested. |
| KS09 | not sticky | **Genuine** | `killed = trip` auto-resets on recovery. No test recovers after a trip (SPEC-003 §7 "manual reset only"). |
| EN_KS (threshold) | config threshold ignored | **Genuine** | No non-default `kill_switch_drawdown` test. |
| EN02 + RB07 (combo) | pending plan survives a trip and transfers while killed | **Genuine** | Survives as a pair (singly each is shadowed by the other). Needs a pending incomplete plan at the trip bar. |
| EN10, EN11 | reset guard; low-level BUY while killed | **Genuine** | No test for reset-when-not-tripped or the low-level BUY guard (I11). |
| EN24, ST13, EN36, EN32, EN35 | restore loses stop state / levels / overlay / wait counters / transfer counter | **Genuine** | The restore tests do not run with live stops, locks, pending orders or a second rebalance. My fuzz (restore every 7 bars) kills EN24, EN35, KS07, KS09. |
| EN27, EN28, EN29, EN30, EN31 | `verify()` and restore corruption checks off | **Genuine** | `StateCorruptionError` appears in no test (grep: 0). No engine-level tamper tests. |
| EN01 | no month seeding | **Genuine** | Day-one plan (a `REBALANCE_PLANNED` on bar 1 and a sizing override) is not asserted against. |
| EN05 + EN06 (combo) | dup-STOP guard and qty-netting | **Genuine** | Survives as a pair. With both off, a pending STOP plus a repeat trigger would hit the broker's oversell check and crash `process_bar`. Needs a bar gap on the stopped symbol. |
| EN12, EN13, EN19, EN20 | submit `asof` unchecked; weights sum > 1 unchecked; stale vol kept; episode reset off | **Genuine** (EN20 low reachability) | Missing guards on the call contract. |
| RB02, RB03, RB10 | transfer attempts 2/4 and `>` | **Genuine** | The 3-attempt retry boundary is unpinned. |
| RB06, RB14 | give-capped-by-cash off; pool = max | **Genuine** | The cash-short giver path is untested (probe p10 reaches it). |
| RB09, RB11, RB12, RB13 | no remainder cents; complete on `and`; moved_in/out not tracked | **Genuine** | RB12 verified: it moves extra cents on retries. No multi-attempt or cents-residual test. |
| RB07, RB08 (singly) | transfer freeze / plan-while-killed guard off | Redundant pair | Covered: the pair (EN04+RB08) is killed. RB07 survives only with EN02 (see combo). |
| RB15 | `REBALANCE_INCOMPLETE` not added to `TransferResult.events` | **Equivalent at engine level** | The ledger annotation is still booked (my mutant only dropped the return list). `TransferResult.events` itself is untested. |
| RB17 | skip `q2` on target | Equivalent | `_cents` re-quantizes downstream. |
| BR02 | BUYs fill before SELLs | **Genuine** | Sells-first (spec step A) is unpinned: no test has a SELL and a cash-limited BUY pending together. |
| BR03, BR04 | expiry `>` / never expire | **Genuine** | `EXPIRED` appears only in the state-machine table (`test_orders.py:42,123`), never through the broker. |
| BR05, BR14 | affordability `<=` boundary; cash-shrink off | **Genuine** | The `SHRUNK_CASH` path at fill time (spec §3) is untested. The realistic trigger is a gap-up open on a full-cash BUY. |
| BR01 | fill on creation bar | **Genuine at broker level, unreachable via the engine** | `SimBroker` is public; add a unit test that an order created at `asof` is not filled by `process_bar(asof)`. |
| BR12 | sell clamp to holdings | Equivalent | `submit_orders` already guarantees it. |
| CK17, CK18 | projection ignores pending BUYs / SELLs | **Genuine** | CK17 verified: a repeated `submit_target_weights` call double-buys (second call returns 200 + 99 shares instead of nothing). The spec §4 idempotency property is untested. |
| CK13, CK14 | sell clamp / exit qty ignore pending | **Genuine** | Needs a pending BUY plus a reduce (F2). |
| CK25 | NO_VOL_DATA symbols not blocked | **Genuine** | Verified: a held symbol whose vol is missing is liquidated. This is the "vol blip must not liquidate" property. |
| CK21 | allowed-asset-class check off | **Genuine** | BTC weight in an equity sleeve is not asserted to raise. |
| CK10, CK27 | min-size `<` boundary; reductions blocked when killed | **Genuine** (minor) | Boundary and killed-trim cases unpinned. |
| CK05, CK09, CK15 | shrink `>=`; avail `<0`; BUYs listed before SELLs | Equivalent (log/ordering only) | Differ only in the rejection record or reason, or in list order that the broker re-sorts. |
| LM06 | reserved cash omits the fee | **Genuine** | Equity fee is 0; no crypto cash-limited case. |
| LM08 | reserved cash = 0 | **Genuine** | SPEC-003 §3 "two pending BUYs cannot over-commit a sleeve" is untested (I3 still holds via fill-time shrink). |
| LM11 | crypto exposure ignores held positions | **Genuine** | The 30% cap is only tested against pending BUYs, not against filled crypto holdings. |
| ST12 | stop candidate rounding mode | **Genuine** (minor) | Stop-level rounding at the 8th decimal is unpinned. |
| ST04 | zero floor on the candidate | Equivalent | A negative level can never trigger on a positive close; only the displayed `stop_level` differs. Still, SPEC-003 §7 says "floor at 0": pin with a test. |
| EN04 | plan-while-killed guard | Redundant | Shadowed by `plan_rebalance`'s own guard (RB08); the pair is killed. |
| EN09, EN14, EN15, EN21 | lock set over held symbols; stale-mark stop test; stale-bar guard; plan overwrite | Equivalent / shadowed | EN15 is shadowed by the broker's own stale-bar check (pair EN15+BR08 killed). EN21 is unreachable (attempts end in 3 bars). |

Counts: 91 survivors = 77 genuine (23 evaluator + 54 engine/risk/broker/rebalance/kill) + 14 equivalent or redundant. The per-area breakdown is below. Effective kill rate = 76 / (167 - 14) = 50%.

| Area | Total | Killed | Genuine survivors | Equivalent / redundant |
|---|---|---|---|---|
| risk/checks + limits + sizing | 44 | 30 | 11 (8 in checks, 3 in limits) | 3 (CK05, CK09, CK15) |
| stops | 13 | 10 | 2 (ST12, ST13) | 1 (ST04) |
| killswitch | 10 | 5 | 5 | 0 |
| broker + costs | 22 | 15 | 6 | 1 (BR12) |
| rebalance | 17 | 4 | 10 (RB07 counted with the EN02 combo) | 3 (RB08, RB15, RB17) |
| engine (incl. config wiring EN_KS) | 37 | 12 | 20 | 5 (EN04, EN09, EN14, EN15, EN21) |
| evaluator | 24 | 0 | 23 | 1 (SE20) |
| **Total** | **167** | **76** | **77** | **14** |

## Test gaps

**Required before Wave 4 treats the engine as safe:**

- **G1 (major, kill switch):** pin the controls that stop the bot losing more than 20%.
  - Stickiness: after a trip, equity recovers above 80% of peak; assert still killed, one TRIPPED event only (KS03, KS09).
  - Running peak: equity rises 50%, then falls 20% from the new peak; trips (KS02).
  - Restore: `export_state`/`from_state` of a tripped engine keeps `killed` and `peak` (KS07, KS10; spec AC5).
  - Config wiring: threshold 0.10 (EN_KS).
  - Reset guard: reset when not tripped raises (EN10).
  - Low-level BUY while killed raises (EN11).
  - Incomplete plan pending at the trip bar: dropped, no transfers (EN02+RB07).
  - Files: `tests/risk/test_killswitch.py` plus an engine scenario.
- **G2 (major, restore and verify):** extend `test_restore_mid_scenario...`:
  - Restore with a live stop and lock, a pending order with a wait counter, a pending plan, a second rebalance after restore, and a tripped switch.
  - Assert byte-identical JSON against an uninterrupted run.
  - Add engine-level tamper tests asserting `StateCorruptionError` (cash +1, dropped FILL, qty_filled, entry_asof; F4 variants). Kills EN24/27-32/35/36, ST13.
- **G3 (major, rebalance):**
  - Retry/INCOMPLETE: giver with no cash, 3 attempts, event on the 3rd, plan dropped, sizing override ends.
  - Partial multi-attempt transfers with no double-move (RB12/13).
  - Cents remainder and one-sided residual (RB09, RB11).
  - Attempts boundary 2/3/4 (RB02/03/10).
  - Month seeding: no plan on day one (EN01).
  - Files: `tests/portfolio/test_rebalance.py`.
- **G4 (major, broker):**
  - A SELL and a cash-limited BUY pending together, SELL first (BR02).
  - A BUY unaffordable at fill after a gap-up: `SHRUNK_CASH` quantity, and an exact-cash boundary (BR14, BR05).
  - Expiry after exactly `max_pending_bars` absent bars, and one bar earlier does not expire (BR03, BR04).
  - An order created at `asof` is not filled by `process_bar(asof)` (BR01).
- **G5 (major, evaluator semantics):** `tests/strategy/test_evaluate.py`:
  - Rotation: rank order and tie-break, `pre_rank` vs `post_rank`, top-k slots, fallback weight, held-not-selected flat, fallback `require`.
  - Rules: entry only when flat, exit priority, hold-while on NaN (pin F3 either way), `bars_held` on fill, +1 and exit at `>=`, `pos` operand.
  - Schedule flags and `force_decision`.
  - Operator table `gt/ge/lt/le/eq`, `crosses_*`, three-valued `not/any/all`.
  - Compile-once, prepare-twice reuse (F1).
  - Kills SE01-SE24 except SE20.
- **G6 (major, risk pipeline):**
  - Idempotent repeated call (CK17, CK18).
  - Cash reservation across two pending BUYs, with crypto fee (LM06, LM08).
  - R4 cap against held crypto positions (LM11).
  - A held symbol with missing vol must not be liquidated (CK25).
  - A pending BUY with a flat or lower target (F2; CK13, CK14).
  - Min-size boundary and killed-reduction (CK10, CK27).
  - A weight outside allowed asset classes (CK21).

**Should add:**

- **G7 (minor):**
  - ATR=0 and negative stop level floor (F5, ST04).
  - Stop rounding at the 8th decimal (ST12).
  - A STOP trigger while a strategy SELL or a prior STOP is pending, on a bar gap (EN05, EN06).
  - Failed-call atomicity (F6).
- **G8 (minor, determinism):**
  - Pin a long-run `export_state` hash (golden, PM-approved note).
  - Engine-level ambient-context test.
  - `PYTHONHASHSEED` subprocess test.
  - My fuzz scripts do this today but are not in the tree.
- **G9 (info):** AC8 property tests (hypothesis, I1-I18 over random paths, T12 in Wave 4) are not present yet. `fuzz.py` and `fuzz2.py` implement most of them and can seed T12.

## Re-audit needed? Yes

Trigger:

1. F1 and F2 fixed (or an explicit PM waiver for F2).
2. Tests G1-G6 added.
3. A re-run of the same mutant set (`scratchpad/mutants.py`, `runmut.py`, `combo.py`), expecting every genuine survivor above killed and the equivalents unchanged.

I will also re-audit when `BarDriver` lands (the `held`/`entry_asof`, `TargetBook` and `rebalance_planned` wiring), and on any change to `risk/*`, `portfolio/engine.py` or `rebalance.py`.
