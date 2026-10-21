# Audit 003-wave2: execution code (orders, costs, ledger, portfolio, config)

- **Auditor:** quality-lead
- **Date:** 2026-10-20
- **Code:** `src/tradebot/sim/orders.py` (210), `sim/costs.py` (57), `portfolio/models.py` (171), `portfolio/ledger.py` (185), `portfolio/portfolio.py` (148), `portfolio/config.py` (169), `risk/config.py` (50). `portfolio/money.py` (267) checked for use only.
- **Tests:** `tests/sim/` (test_costs, test_orders), `tests/portfolio/test_money.py`, `tests/unit/test_money_handcalc.py`: 708 passed. `uv run ruff check .`: clean. mypy: not run (`uv run mypy` fails to spawn, the tool is not installed in the env). **No test imports `portfolio.ledger`, `portfolio.models`, `portfolio.portfolio`, `portfolio.config` or `risk.config`.**
- **Against:** SPEC-003 §2, §3, §8, §9, §10, invariants I1-I18; ADR-0001 D3 (`entry_asof`), D7 (TEXT Decimal), §5 addendum; CLAUDE.md money rules.
- **Verdict: PASS-WITH-NOTES**

The fold arithmetic is correct. Cash effects, proportional cost-basis reduction, realized P&L, the dense seq, replay == live and the `entry_asof` reset all held in probes and a 300-trial fuzz. There is no blocker. There are four majors that can produce wrong money or wrong stops once Wave 3 builds on this code (F1-F4), and the ledger has no tests at all (F5), so every ledger mutant I tried survives. I recommend the PM not start SimBroker/engine work on the ledger until F1-F4 are fixed and the minimum test set in "Test gaps" is in.

## Scope

In scope: the files listed above and their tests, against SPEC-003 and ADR-0001. Not in scope: `money.py` internals (audited in 003-T1), the engine, SimBroker, risk checks, rebalance and kill switch (Wave 3/4, not written). Known PM-list items are not re-reported: unquantized `slippage_cost`, a FILLED order not requiring fill fields, and the I4/I7 transfer-pair checks needing engine context.

## Evidence

- `uv run pytest tests/sim tests/portfolio tests/unit/test_money_handcalc.py -q`: 708 passed.
- `uv run ruff check .`: all checks passed.
- Probe 1 (scratchpad `probe1.py`), what held:
  - Full exit then re-entry: `entry_asof` was kept on an add and a trim, and reset to the new fill date on re-entry. The position dict was emptied on exit.
  - Hand check: 3@100 + 2@110 gives basis 520.00. Selling 1 at 120 reduces basis to 416.00. Selling the remaining 4 at 120 gives realized 64.00 (480 - 416).
  - Selling the exact holdings, overselling, selling an unknown sleeve and selling with no position behave correctly (the last three raise `LedgerError`).
  - Cash exactly 0: a further 0.01 is rejected, spending exactly all cash is accepted. A transfer larger than cash is rejected.
  - Selling 7 shares in six pieces: the sum of basis reductions equals the original basis (233.31) and nothing is left over.
  - Fuzz, 300 trials of 60 random events over 2 sleeves and 3 symbols (equity and crypto, random fees):
    - `replay(events)` equals live state.
    - `Portfolio.verify` (I1, I2, I3, I5, I6, I18) passes every time.
    - A JSON round trip of every event is byte-stable.
    - Result: 0 failures.
- Probe 2, what failed: see the findings.
- Mutation run (real mutants on a copy of `src`, `PYTHONPATH` override): see the table.

## Checklist (Y / N / n.a. with file:line)

| Item | Result | Evidence |
|---|---|---|
| Lookahead | n.a. | No signals in scope. I8 (`fill_asof > created_asof`) is not enforced by the `Order` model (F9) |
| Survivorship | n.a. | |
| Costs both directions | Y (functions) | `costs.py:18-51`; I9 rounding directions pinned by `tests/sim/test_costs.py`. Not yet wired into a fill path |
| Accounting invariants tested | N | I1-I7 and I18 are implemented in `Portfolio.verify` (`portfolio.py:123-148`) but nothing tests it (F5) |
| Determinism / golden pinned | N | No golden. The ledger fold is deterministic under the default decimal context only (F2) |
| Decimal + explicit rounding | Partly | Rounding is explicit through `money`. But `ledger.py:49` and `portfolio.py:95,108,114` do arithmetic outside `money_context` (F2). Floats are coerced, not rejected, in ledger and snapshot models (F4) |
| Benchmark reported | n.a. | |
| Offline tests | Y | No network in the touched tests |
| Restart/idempotency | n.a. | `EngineState` not built yet. Overlay state is not in the ledger and is not replayable (F1) |

## Findings (ranked)

| # | Severity | file:line | Issue | Required fix |
|---|---|---|---|---|
| F1 | major | `portfolio/portfolio.py:28,41,53-55` | The stop overlay (`high_water_close`, `stop_level`) is keyed by (sleeve, symbol) and is never cleared when the position goes flat. The docstring says "cleared at flat on read", but the code does not do that. Probe: set (500, 400) on SPY, sell everything, buy 1 share again, and `positions()` returns the old `high_water_close=500, stop_level=400` on the new position. A re-entry would inherit a stale ratcheted stop and could be stopped out at once. That breaks SPEC-003 §7 ("reset when the position returns to flat") and I13 reasoning. | Drop the overlay entry when the symbol leaves `state.positions`. Better: make the overlay part of the position state (the fold) so replay and `EngineState` restore preserve it and flat clears it by construction. Add a test (F5). |
| F2 | major | `portfolio/ledger.py:49,93,114,115`; `portfolio/portfolio.py:95,108,114,119-120` | Arithmetic runs in the caller's ambient decimal context, not `money_context()` (SPEC-003 §2: "every function runs inside `localcontext(prec=28)`"). Probe: under an ambient `prec=3`, selling 1 of 3 shares bought at 33.33333333 gives `realized_pnl=6.70` and leaves `cost_basis=66.7` (off the cent grid), against the correct 6.67 and 66.67. The same code gives different ledgers depending on a library's global context, which breaks determinism (I15) and the cent grid. The `q2(...)` call is protected but the multiply and divide inside it are not. | Wrap `sell_reduction`, `apply`, `complete` and the snapshot divisions in `money_context()`. Add a test that runs the fold under `localcontext(prec=3)` and under ROUND_DOWN and compares to the default result. |
| F3 | major | `portfolio/ledger.py:99-115` with `models.py:98-100` | A BUY's FILL and its FEE are separate events, appended one at a time. If the FILL fits in cash but the FEE does not, the FILL is already committed and the FEE raises. Probe: sleeve with 500.00, FILL 500.00 succeeds, FEE 0.50 raises `LedgerError`, and the ledger keeps the orphan position with no fee. The fold has no way to roll back. SPEC-003 §3 says shortfall shrinks qty, so the engine must pre-check `notional + fee <= cash`, but the ledger API does not enforce that. A single missed pre-check yields fee-free positions. | Add an atomic `Ledger.append_fill(fill_fields, fee)` (or a batch append that validates all events against a copy of the state, then commits). The engine should use only that. Test: fill fits, fee does not, ledger unchanged. |
| F4 | major | `portfolio/models.py:65-73,112-116,138-143,148-170` | Float handling is inconsistent with `Order`. `Order` and `OrderRequest` reject floats (`orders.py:83-89`). `LedgerEvent`, `Position`, `PositionSnapshot`, `SleeveSnapshot` and `PortfolioSnapshot` use plain `Decimal` fields, so pydantic lax mode silently converts floats: `amount=1.5`, `qty=0.1, price=3.0`, `Position(qty=0.5)` and `PortfolioSnapshot(drawdown=0.1, exposures={"a": 0.3})` are all accepted. I17 holds only because pydantic converts them to `Decimal`; nothing guards it, and a float from the data layer would turn into a wrong-looking decimal such as `0.1` without any error. | Reuse the `Dec` annotated type from `orders.py` (or `strict=True`) on every money, quantity and price field in these models. Same for `Position` and the snapshots. Add tests that each field rejects float, and that `model_dump_json` / `model_validate_json` round trips with canonical text (D7). |
| F5 | major | `tests/` (missing) | No test imports `ledger`, `models`, `portfolio` or `config`. Mutants that flip the SELL cash sign, flip the realized-P&L sign, or drop the cent rounding of the proportional basis reduction all survive with 708 passing (table below). A wrong sign in the ledger would report fake profits, which is the worst failure for this project. | Add the minimum set in "Test gaps" (G1) before Wave 3 depends on the ledger. |
| F6 | minor | `portfolio/models.py:94` | A FILL with `amount == 0.00` is accepted. Probe: BUY 1E-8 BTC at 60000.12345678 gives notional 0.00, a position with qty 1E-8 and cost_basis 0.00. Repeating this buys crypto for free, and the sell side then books realized 0.00. The engine's R6 minimum size should block it, but the ledger is the last line of defence. | Reject FILL events with `amount <= 0` (or amount below one cent) in `_check_shape`. |
| F7 | minor | `portfolio/models.py:88-100`, `ledger.py:104-115` | Not enforced in the ledger: price on the 1e-8 grid, qty grid (whole shares for equity, 8 dp for crypto, I4), a second FILL for the same `order_id` (I14, probe: duplicate fill accepted), a FEE whose `order_id` matches a FILL, at most one FEE per order, and `stage == OPEN_FILL` for fills. Probe: qty 1E-9 and price 1.0000000001 accepted. | Enforce price/qty grids (needs an asset-class hint, or check 8 dp max and let the engine check the equity grid), a set of seen fill `order_id`s in `LedgerState`, and the FEE linkage. |
| F8 | minor | `portfolio/models.py:74` | `metadata: dict[str, str]` is mutable inside a frozen event. Probe: `ev.metadata["k"] = "HACK"` changes the event held in the ledger. I7 says events are never mutated. Annotation events only, no cash effect, but a prefix-hash test would catch it too late. | Store metadata as an immutable mapping (tuple of pairs or `MappingProxyType` copy), or deep-copy on read. |
| F9 | minor | `sim/orders.py:117-152` | Beyond the known "FILLED needs fill fields" item: I8 (`fill_asof > created_asof`) is not checked, and REJECTED/CANCELLED/EXPIRED orders may carry `qty_filled > 0`. Fix together with the known item. | Add a status-consistency validator in the same change. |
| F10 | minor | `portfolio/ledger.py:69-71` | `DEPOSIT` is accepted at any time, for any new sleeve id, and with amount 0.00 (probe). SPEC-003 §9 says "initial funding only in v1". I6 treats deposits as capital, so a late deposit is consistent but would hide a bug that deposits instead of transferring. | Restrict DEPOSIT to before the first trade event, or require the engine to own sleeve creation. At least reject 0.00. |
| F11 | minor | `portfolio/portfolio.py:74,87` | `snapshot` accepts a zero or negative close (probe: close -5 gives equity 1290.00). It raises `TypeError` for a float close, which is fine but accidental. | Reject non-positive or non-Decimal closes with `LedgerError`/`BadPriceError`. |
| F12 | minor | `portfolio/portfolio.py:123-148` | `verify` checks the snapshot against itself. I1 and I2 use values the same snapshot supplied, so a snapshot with a wrong `mv` (for example a wrong rounding of `qty*close`) passes as long as it is internally consistent, and I6 uses the snapshot's own `unrealized`. I4 (grids) and I7 are not checked, and it raises `KeyError` instead of `LedgerError` for a sleeve in the snapshot that is unknown to the ledger (line 137). `verify` replays the full ledger every call (line 126), and `Ledger.state` copies on every property access (`ledger.py:152`, used at `portfolio.py:32,35,40,53,67,125`), so a per-bar verify is O(events) per bar. | Let `verify` take `closes` and recompute `mv`; map unknown sleeves to `LedgerError`; cache or expose a read-only state view; verify every N bars or at the end in long backtests. |
| F13 | minor | `portfolio/config.py:88-121,124-169` | Nothing checks that `target_weight x initial_capital` lands on cents or that the sleeve deposits sum to `initial_capital` exactly. 100000.01 x 0.5 = 50000.005. Wave 3 needs a rule (largest-remainder or last sleeve takes the remainder). `CostConfig.slip_bps` has no upper bound (`config.py:75-79`), so `slip_bps >= 10000` makes `sell_fill_price` zero or negative (`costs.py:27`) and the ledger then rejects the fill with a confusing message. `EngineConfig.costs` is a mutable `dict` inside a frozen model. | Validate capital on the cent grid and define the deposit-split rule in the engine; bound `slip_bps` below 10000; use a read-only mapping. |
| F14 | nit | `portfolio/models.py:65-73`, `ledger.py` | Decimal exponent is not canonical: `amount=Decimal("1E+2")` is accepted and persists as `1E+2`, `Decimal("100")` and `Decimal("100.00")` compare equal but serialize differently, so byte-identical `export_state` (I15) depends on every producer using the same exponent. | Canonicalize cents with `q2` in a validator (after-validation normalize), or compare text in tests. |
| F15 | nit | `sim/costs.py:18-57` | Cost functions do not validate inputs (negative qty or open price, negative bps). They are only reachable through validated config today. Known item (unquantized `slippage_cost`) not repeated. | Optional guard or doc note. |
| F16 | nit | `portfolio/ledger.py:46` | Selling in an unknown sleeve reports "exceeds holdings" rather than "unknown sleeve". | Message only. |

### Money used correctly (`money.py` usage)

`costs.py` and `models.py` call `ceil8`, `floor8`, `q2`, `q2_half_up` with the SPEC-003 §2 modes, and `orders.py` serializes with `to_text` / parses with `from_text`. Fee uses HALF_UP, notional HALF_EVEN, buy price CEILING, sell price FLOOR, all through `money_context`. Cost functions are right; the leaks are in the ledger and portfolio arithmetic (F2).

### Order state machine and Wave 3 SimBroker

The edge table (`orders.py:62-76`) matches SPEC-003 §3 exactly (NEW to PENDING or REJECTED; PENDING to FILLED, REJECTED, CANCELLED or EXPIRED; terminal states final). The I14 quantity chain is enforced on every transition and through `model_validate` (not `model_copy`), so a bad update raises. `NEW` is transient and `OrderIdGenerator` is deterministic with a persistable counter.

It is enough for SimBroker once F9 and the known fill-fields item are done. A cash-shrunk fill can be expressed as `FILLED` with `qty_filled < qty_accepted`, or by lowering `qty_accepted` before filling; the broker should pick one and test it. Open points for Wave 3, none blocking:
- `reason` is free text (SPEC-003 lists machine-readable codes such as `SHRUNK_CASH`, `MAX_ASSET_SHRUNK`). Make it an enum or a constants module so 005 can assert on it.
- `OrderRequest.qty` is not grid-checked, because the order has no asset class. The broker must call `check_sell_qty` / `buy_qty` before creating an order.
- Reserved cash and pending-bar count are not on the `Order`; derive them from `ref_price`, `created_asof` and the engine clock.

### Portfolio.snapshot

Weights, exposures and drawdown are computed correctly in the probes: drawdown 0.0133333333 for equity 1480 vs peak 1500, sleeve weights 0.662162 and 0.337838, exposure 0.121622. Peak is `max(passed peak, equity)` and defaults to the current equity (the spec says "peak starts at initial capital", so the engine must pass the initial capital on bar 1). Equity <= 0 and peak <= 0 degrade to zero weights and zero drawdown (no division error). Division is outside `money_context` (F2) and the sleeve weights are not forced to sum to exactly 1 (rounding, acceptable). `mv` is `q2(qty x close)` per position, as the spec requires.

## Mutation and test-strength review

Real mutants: a copy of `src` with one `sed` change, run against the in-scope tests only (`tests/sim tests/portfolio tests/unit/test_money_handcalc.py`, 708 tests).

| Mutation | Caught by | Verdict |
|---|---|---|
| `sell_cash_in`: `notional - fee` changed to `+` | `tests/sim/test_costs.py` (sell worked examples) | Caught |
| `buy_cash_out`: `+` changed to `-` | `tests/sim/test_costs.py` | Caught |
| `fee`: `q2_half_up` changed to `q2` | `test_costs.py:97` (tie test) | Caught |
| `buy_fill_price`: `1 + slip` changed to `1 - slip` | `test_costs.py` | Caught |
| `sell_fill_price`: `floor8` changed to `ceil8` | `test_costs.py` | Caught |
| `slippage_cost`: `abs()` removed | `test_costs.py:117` | Caught |
| `LEGAL_TRANSITIONS`: FILLED gets an outgoing edge | `test_orders.py` transition table | Caught |
| I14: `qty_filled > qty_accepted` loosened by 1 | `test_orders.py:208` | Caught |
| Ledger SELL cash: `cash + amount` changed to `cash - amount` | none | **Survives** |
| Ledger realized: `amount - reduce` changed to `amount + reduce` | none | **Survives** |
| Ledger basis reduction: `q2(...)` removed (off-grid basis) | none | **Survives** |
| By reading only: BUY adds `ev.amount` to basis changed to subtract; fees folded into basis; `del book[...]` on full exit removed; `entry_asof` taken from the wrong event; `ev.amount > cash` changed to `>=`; seq density check removed; I6 formula sign flipped in `verify` | none (no test touches the module) | All survive |

The `sim/` suite is strong. The `portfolio/` ledger, models, snapshot and config suite is nonexistent, so the mutation score there is 0.

## Test gaps

- **G1 (major, add now): minimum ledger/portfolio set** in `tests/portfolio/test_ledger.py` and `tests/portfolio/test_portfolio.py`, built on hand-computed numbers, no random data:
  1. Fold of each event type with exact cents. Example: DEPOSIT 1000.00; BUY 3 @ 100 and 2 @ 110 gives basis 520.00; SELL 1 @ 120 gives cash +120.00, basis 416.00, realized 16.00; SELL the remaining 4 @ 120 gives realized 64.00 and no position left. FEE reduces cash and raises `fees`. TRANSFER_OUT/IN move cash and do not change equity. Annotations change nothing.
  2. A SELL where the proportional reduction needs rounding (3 shares, basis 100.00, sell 1), and a multi-step trim whose reductions sum exactly to the original basis.
  3. `entry_asof`: set on first buy, unchanged on add and trim, cleared at flat, new value on re-entry (I18).
  4. Rejections: oversell, sell with no position, unknown sleeve, buy or fee or transfer above cash, cash exactly 0 (allowed to 0.00, one cent more rejected, I3), non-dense seq, and a failed append leaving state unchanged.
  5. Replay == live state for a mixed event list (I5), and a JSON round trip of every event (D7).
  6. I17: every Decimal field in `LedgerEvent`, `Position` and the three snapshot models rejects float.
  7. `Portfolio.snapshot` worked example: mv rounding, unrealized, sleeve weights, exposures, crypto fraction, drawdown against a given peak, missing close raises.
  8. `Portfolio.verify` negative tests: tamper with `cash`, `mv`, `entry_asof`, `qty` and confirm each raises the intended invariant (I1, I2, I3, I5, I6, I18).
  9. Ambient-context test: the fold gives identical results under `localcontext(prec=3)` and `ROUND_DOWN` (guards F2).
  10. Overlay cleared after exit and re-entry (guards F1).
- **G2 (major, with the fix for F3):** atomic fill + fee: a fee that does not fit leaves the ledger unchanged.
- **G3 (minor):** `tests/portfolio/test_config.py`: weights must sum to exactly 1, duplicate sleeve ids, missing cost class, float rejection, `default_config()` equals 50/20/20/10 and the STRATEGY §6 cost schedule, bps bounds.
- **G4 (minor, Wave 3/4 as planned):** Hypothesis property test over random event sequences checking I1-I7 and replay equality (my fuzz probe can be the seed); the I7 prefix-hash and transfer-pair checks need the engine.

## Re-audit needed? yes

Trigger: fixes for F1-F4 (F5 tests and G2 land with them), and a quick re-check before the Wave 3 engine/SimBroker audit. F6-F14 can ride along with Wave 3 but F6 and F7 should be in before the engine writes its first FILL. The mandatory Wave 3 audit must also check the I4/I7 transfer pairing, the fill/fee atomic path and the `entry_asof` hand-off to `BarDriver`.

## Re-audit (fix pass)

**Verdict: PASS.** No blocker and no major is open. Full suite (1386 passed, 1 skipped) re-run by me. All mutants were run on a scratch copy of `src` and `tests` only. Deferred items (F7, F10-F14, config tests, I4/I7 transfer pairing, overlay persistence in `EngineState`) are not re-reported.

### Majors

| Finding | Status | Evidence |
|---|---|---|
| F1 stop overlay survives flat and re-entry | Fixed | `ledger.py:48` and `:105-125`: the fold records `opened_seq[(sleeve, symbol)]` on the first BUY from flat and drops it at flat. `portfolio.py:44-72`: an overlay entry applies only if its tag equals the current `opened_seq`, otherwise it is deleted. Mutant "overlay tag check removed" is caught (`test_overlay_is_cleared_when_position_goes_flat_and_reentry_starts_fresh`). The mutant that skips `opened_seq.pop` survives, but it is equivalent: re-entry overwrites the entry, and the lookup runs only for open positions. |
| F2 ambient decimal context | Fixed | Every fold step, `sell_reduction`, `append_fill`, `snapshot`, `verify`, the `LedgerEvent` amount check and all `sim/costs.py` helpers run in `money_context()`. `money.py` uses a fully specified context (prec 28, HALF_EVEN, traps set), not a copy of the caller's. Mutant "money_context removed from `apply`" is caught (the ambient-context test). |
| F3 fill and fee not atomic | Fixed | `Ledger.append_fill` (`ledger.py:211-275`) validates FILL plus FEE on a state copy (`_commit_many`) and commits both or neither. The low-level `append` refuses FILL and FEE (`:184-187`); the mutant that lifts this is caught. Mutants "validation on live state instead of the copy" and "FEE never booked" are caught. Zero-notional BUY is rejected (mutant caught). |
| F4 `Dec` type duplicated | Fixed | A single strict `Dec` lives in `portfolio/models.py:30-34` (rejects float and int, parses canonical text). `sim/orders.py` imports it, so `Order` and `OrderRequest` now reject floats. |
| F5 no ledger/portfolio tests | Fixed | `tests/portfolio/test_{ledger,portfolio,models}.py` exist. The three original ledger mutants are caught: SELL cash sign, realized sign (both `complete` and the running total) and `q2` removed from the basis reduction. Further mutants all caught: BUY basis sign, `>` to `>=` on cash, `del book[...]` removed, `entry_asof` wrong, fees not summed, zero-notional guard removed, and `money_context` removed. |

### Other fixes verified

- `slippage_cost` is now `q2` (`costs.py:56-57`).
- A FILLED `Order` requires `fill_asof`, `fill_price`, `fee` and `qty_filled > 0`, and `fill_asof > created_asof` (`orders.py` `_check_quantities`).
- `OrderReason` is an enum.
- `LedgerEvent.metadata` is frozen (`FrozenDict`, including copy and pickle).
- The `sim/` mutant set from the first audit still gets 8 of 8 caught.

### Remaining findings (none blocking)

| ID | Sev | Where | Finding |
|---|---|---|---|
| R1 | minor | `ledger.py:150-156` | `Ledger.__init__(events)` replays through `_commit`, which accepts raw FILL and FEE events. That is needed for replay and is fine. But a caller who builds a `Ledger` from a hand-made list can create a FILL with no FEE, and nothing checks that a FILL's fee is present. Wave 3 should check that `fee == 0` or a FEE event with the same `order_id` follows. I7 is already deferred, and this is the replay-side half of it. |
| R2 | nit | `portfolio.py:46-47`, `:107` | `ledger.state` returns a full copy and is called per access in `positions`, `cash` and `sleeve_ids`. This is O(n) per call and not a correctness issue. Watch it when the engine calls it per bar per symbol. |
| R3 | nit | `ledger.py:121-123`, `models.py` | A SELL whose notional rounds to 0.00 (a tiny crypto qty) is allowed. It closes lots at zero proceeds, and realized equals minus the basis reduction. Intended, but the engine should size against a minimum notional. |
| R4 | nit | `models.py` `Position` | `qty` has no precision grid check (cent grid on `cost_basis` only). This is fine because qty comes from validated fills. |

### Required before the Wave 3 engine audit

Wave 3 still has to deliver the deferred items, in particular I4/I7 transfer pairing, overlay persistence, and the `entry_asof` hand-off to `BarDriver`. I will check these in the mandatory engine audit.
