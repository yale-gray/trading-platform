# Audit 006: Wave 6 part 1 (ADR-0001 A12-A18)

- **Auditor:** quality-lead
- **Date:** 2026-10-22
- **Code:** uncommitted tree (27 modified files, ~630 insertions): `api/jobs.py`, `api/routers/backtests.py`, `cli.py`, `backtest/{engine,driver}.py`, `risk/{killswitch,config}.py`, `portfolio/{engine,models}.py`, `runner/bootstrap.py`, `data/{symbols,universe}.py`, `data/universes/*`, `strategies/{index_*,stocks_momentum}.yaml`, `dashboard/views/portfolio_detail.py`
- **Tests:** full suite 2197 passed (after golden re-pin; before it, only the 4 golden `config_hash` checks failed); `ruff check .` clean; mypy not installed in the env (not run)
- **Against:** ADR-0001 A12-A18, research notes `index-strategies-2026-10.md`, `sp500-stocks-2026-10.md`, CLAUDE.md hard rules
- **Verdict: PASS-WITH-NOTES**

The numerics are sound: the kill-switch pause/re-arm is correct, `_bars_at` is numerically identical, and goldens moved only in `config_hash`. There is no blocker. Two majors need fixing before the smart preset is run for real: the survivorship label is missing from every user-facing stock result (F1), and the smart preset's SPY-overlap cap interaction (F2). The runner/DB re-arm mirror gap (F3) should be fixed with them.

## Scope
The five areas in the request. Not in scope: strategy quality or profitability, Alpaca, the devconsole.

## Evidence
- `uv run pytest -q`: 2197 passed. `uv run ruff check .`: clean.
- Golden comparison: I backed up the old `G1-G4.json`, regenerated them with the approved flow, and compared key by key. The only differing path is `/config_hash` in all four. All metrics, `fills_sha256`, `ledger_sha256` and the benchmark blocks are identical (ledger sha: G1 0813ba16..., G2 b640bb21..., G3 e10e46b9..., G4 2151a81d...).
- Regeneration ran against the current code, which includes the new `_bars_at`. The old pins predate that change, so this is an end-to-end proof that `_bars_at` is numerically identical on the 4 fixtures.
- Re-pin performed: note `docs/reviews/golden/2026-10-22-kill-switch-pause-field.md` (Approved-by: PM 2026-10-22, with the new `golden-sha256` line), then `pytest tests/golden --update-golden --golden-note=<note>`. `pytest tests/golden` afterwards: 12 passed (including `test_golden_change_note`).
- Probes: `normalize_symbol` on ~15 inputs (see section 5).

## Checklist
| Item | Result | Evidence |
|---|---|---|
| Lookahead | Y | `_bars_at` only maps ts to a row; no signal-path change (`driver.py:280-296`) |
| Survivorship | N (label missing, F1) | `universe.py:66-75` docstring only; `date_added` honoured only by `DatedUniverse`, which the yaml does not use |
| Costs both directions | n.a. (unchanged) | goldens unchanged |
| Accounting invariants | Y | `tests/properties/test_engine_invariants.py`, `test_killswitch_rearm.py` |
| Determinism / golden | Y | goldens re-pinned, ledger sha unchanged |
| Decimal | Y | killswitch uses `money_context`; caps are Decimal; `strategy_max_weight` uses `Decimal(repr(float))` |
| Benchmark reported | Y | CLI prints benchmark total return and cagr per column (`cli.py:~383`) |
| Offline tests | Y | suite passes under the offline guard |
| Restart / idempotency | Y with notes (F3, F4, F5) | `engine.py:236-285`, `killswitch.py from_state` |

## 1. Kill switch
- **Pause counting is correct.** The trip bar sets `bars_paused=0` (`killswitch.py` observe). Each later bar adds 1. The `pause_bars`-th later bar re-arms, sets `peak = that bar's equity`, and returns before the trip test, so there is no trip test on the re-arm bar. A mutant that tested the trip on the re-arm bar would trip immediately only if equity <= 0.8*equity, which is impossible. So the structure, not a test, guarantees this, and `test_pause_then_rearm_books_event_and_resets_peak` covers the counts.
- **Exits allowed during the pause.** `allows_buy` only blocks BUYs. `submit_orders` refuses BUYs only (`engine.py:622`). Trailing stop exits still go through `_step_stops`. A BUY on the re-arm bar is accepted: `killed` is already False at step E and rebalance step F can plan. Strategies on monthly schedules stay flat until their next decision bar, because `_last_rebalance_month` advances while frozen. That is a behaviour note, not a bug (F6).
- **Persistence / back-compat.** `to_state` adds `pause_bars` and `bars_paused`. `from_state` defaults them (`bars_paused=0`, `pause_bars` from config), and `test_state_saved_before_a18_restores` covers that. **Behaviour change:** a live portfolio that was already tripped before A18 will now auto re-arm 20 bars after the restore. That is the intended A18 semantics, but the PM should know before deploying to a running paper portfolio (F5).
- **Restore cross-checks.** The `killed` flag must match the last of the TRIPPED/RESET/REARMED events. `bars_paused` must be 0 when not killed and `< pause_len` when killed. The peak floor comes from the last kill event's metadata. All three event types carry `peak_equity` (RESET at `engine.py:649`, REARMED at ~`:432`). Good. The `bars_paused` check uses the stored `pause_bars`, so a later config change does not corrupt an old state. See F4 for one false-positive.
- **Runner/DB mirror gap (F3).** `db/repo.py:481-519` `mirror_kill_switch` handles only `KILL_SWITCH_TRIPPED` and `KILL_SWITCH_RESET`. After an automatic re-arm:
  - `row.tripped` and `peak_equity` are updated, because they are copied from the snapshot.
  - `tripped_at_bar` and `trigger_drawdown` are left stale, and `acknowledged_at`/`ack_note` stay as they were.
  - No `kind="rearm"` row is written to `kill_switch_event`.
  - The API still returns the old `tripped_at_bar` (`api/routers/portfolios.py:71`), so a client could show "tripped at ..." for a switch that is armed.
  - The dashboard only prints `tripped_at_bar` when `tripped` is true (`portfolio_detail.py:~290`), so it is not wrong today, but the audit log lacks the re-arm.
  - The runner can finish the pause in a tick that also writes the checkpoint, so nothing else is lost.

  **Fix spec:** in `mirror_kill_switch` add a branch `if "KILL_SWITCH_REARMED" in events:` that sets `tripped_at_bar=None`, `trigger_drawdown=None` and `acknowledged_at=None`/`ack_note=None` (the review flag is moot once armed), and adds `KillSwitchEvent(kind="rearm", note=str(bar_ts.date()))`. Update the model docstring to `trip / ack / reset / rearm`. Add a test in `tests/platform/test_runner_killswitch.py` that runs a tripped fixture for `pause_bars` more ticks and asserts all of the above plus that the API returns `tripped_at_bar: null`.

## 2. `_bars_at`
`driver.py:244-248, 283`. The new `{ts: position}` dict gives `i` for every symbol/ts, `-1` when absent, equal to `Index.get_indexer([ts])[0]`. The two differ only on a non-unique index (`get_indexer` raises; the dict keeps the last position). Frames come from the cache or `quality.py`, which drop duplicates and enforce a unique index (`cache.py:140`), so the difference cannot occur. `Timestamp` hashing is tz-consistent (UTC throughout). Proven end to end by the unchanged ledger shas on G1-G4 (fixture frames, 8-10 symbols). A direct equivalence unit test is not present (F8, nit).

## 3. Golden re-pin
Done, as above. Only `config_hash` changed because `RiskConfig.kill_switch_pause_bars` joined the hashed config.

## 4. Smart preset
Layout: `bootstrap.py:96-163`. Index sleeve 50% (SPY 25%, QQQ 25%), stocks 40% (top 15 equal weight, so 2.67% per name), crypto 10% (vol-scaled). Caps: 0.30 per asset, 0.15 crypto.
- Normal state fits: SPY and QQQ at 25% sit under 0.30, and crypto at 10% sits under 0.15. Drift tolerance of 5 points on the indexes is reasonable.
- **SPY overlap (F2).** `strategies/stocks_momentum.yaml:18` has `fallback: {symbol: SPY}`. `strategy/engine.py:~388` gives SPY `empty/k` of the sleeve. If few stocks pass the 200-day filter (a bear market, exactly when it matters), up to 40% of the account goes to SPY. The cap is enforced across sleeves (`limits.py symbol_exposure`, which sums all sleeves) and clips BUYs to the room. With the index sleeve already holding about 25%, the stocks sleeve can add only about 5% SPY before clipping. The rest of the stocks sleeve then sits in cash. No rule is broken and nothing exceeds the cap, but the fallback does not behave as documented, and the sleeves compete for room in processing order, so which one gets clipped depends on sleeve order. The honest outcome is "cash when the market filter fails", which is defensible but accidental. A higher cap (about 0.65) would defeat the purpose of the per-asset cap.
  **Recommendation:** use a stocks variant with no fallback (stocks sleeve holds cash on empty slots), for example a `stocks_momentum_cash.yaml` or a `smart_definition` override that strips the fallback. Keep `fallback: SPY` for single-sleeve backtests, where `strategy_max_weight` already returns 1. Then document "empty slots = cash". Add a test with a bearish fixture proving the SPY exposure stays <= 0.30.
- A18 interacts here too: if the kill switch trips, SPY and QQQ BUYs are paused with everything else, which is fine.
- Other: `vol_scaled=False` for index and stocks, `True` for crypto (matches the notes). A missing strategy file raises `RunnerError` naming the path (tested in `test_smart_preset.py`). The $10,000 account and 0.5/0.4/0.1 weights sum to 1.

## 5. Symbols
- `data/symbols.py`: `BRK.B`, `BRK-B`, `brk.b` normalise to `BRK.B`; `to_yahoo` gives `BRK-B`. Rejected as before: `BRK.`, `A.B.C`, `AAPL.`, `SPY.BB`, `BRK_B`, `BTC` (the crypto base list and regex are unchanged). The regex now accepts any `ROOT.X` or `ROOT-X` share-class form (for example `ABC-B`); an unknown class share fails later at the provider, not silently. Crypto `BTC-USD` still routes to crypto because that branch runs first. Tests added in `tests/data/test_symbols.py:82-91`.
- Cache path: `_fs_symbol` only replaces `/`, and `.` is a safe filename character, so `BRK.B` caches cleanly.
- YAML quoting: every ticker in `stocks_momentum.yaml` is a quoted string, so `ON` (and any `NO`/`Y`/`Q`/`P` style word that YAML 1.1 could coerce) stays a string. Not enforced by a test (F9, nit).
- `sp500.csv` includes future-dated, odd tickers (`MRSH`, `SKYD`, `VMRK`, `VYLR`, `HONA`, `FDXF`). These come from the committed 2026-10-21 snapshot. The yaml omits FISV and HUBB for data quality failures, and the comment says so.

## 6. Research honesty
Present only in code comments and docs: `universe.py:66-75`, `sp500.meta.json`, `stocks_momentum.yaml:8`, ADR A13. **Absent from every place a user sees stock results (F1):** the CLI `backtest run` output, the backtest API payload/report JSON, `dashboard/views/portfolio_detail.py` (the Stocks section, `_sections`), and the smart preset's portfolio description. `DatedUniverse` honours `date_added`, but `stocks_momentum.yaml` lists a fixed universe, so even that partial mitigation is not used by the backtest. Results will therefore overstate stock-momentum performance without any on-screen caveat.

## 7. Single-sleeve caps (`api/jobs.py:150-205`)
- Rotation without fallback: `1/top_n`. With fallback: `1`. Both correct, because the fallback symbol can receive every empty slot.
- Rules: numeric weight capped at 1, `equal` gives `1/len(universe)`. Correct.
- `index_hold` (weight 0.5, two names): cap 0.5.
- `Decimal(1)/Decimal(3)` truncates at 28 digits, and the order sizing floors to the grid, so a target of exactly 1/3 can never exceed the cap through rounding. No crypto cap in single-sleeve backtests (default 1) and the kill switch can be turned off (`Decimal(1)` threshold). Overrides are validated at the API (`routers/backtests.py:49-58`): booleans must be bool, fractions finite and in (0, 1].
- Note: `killed_on` (`backtest/engine.py:108`) reports only the **first** trip. With A18 re-arm a run can trip several times, so the "kill-switch on" column understates it. Minor (F7).

## Findings (ranked)
| # | Severity | file:line | Issue | Required fix |
|---|---|---|---|---|
| F1 | major | `cli.py:~346-390`, `api/jobs.py`, `dashboard/views/portfolio_detail.py:133`, `runner/bootstrap.py:96` | No survivorship label wherever stock results are shown (CLI, API, dashboard, smart preset description) | Add the fixed label "current S&P 500 constituents (survivorship-biased)" to the CLI table footer, the backtest payload/report notes, the dashboard Stocks section header caption and the smart-preset description; one test per surface |
| F2 | major | `strategies/stocks_momentum.yaml:18`, `bootstrap.py:137` | SPY fallback plus index SPY (25%) exceed the 0.30 cap, so BUYs are clipped, order-dependently, leaving idle cash | Smart preset uses a no-fallback stocks variant (cash); keep SPY fallback for single-sleeve runs; add a bear-market test |
| F3 | major | `db/repo.py:481-519` | Auto re-arm is not mirrored: stale `tripped_at_bar`/`trigger_drawdown`, no `rearm` event | Spec in section 1 |
| F4 | minor | `portfolio/engine.py:283` | `peak < last_equity` false-positives while killed: equity can exceed the old peak during a pause (needs +25% in under 20 bars, plausible in crypto), so restore raises `StateCorruptionError` and wedges the runner | Apply the `peak < last_equity` check only when `not killed`; add a restore test with a high last_equity mid-pause |
| F5 | minor | `killswitch.py from_state` | A portfolio tripped before A18 auto re-arms 20 bars after restore | PM decision: accept, or give legacy tripped states a very large `pause_bars`; mention in the release note |
| F6 | minor | `portfolio/engine.py` rebalance step F | Monthly-schedule sleeves stay flat after a re-arm until the next decision bar (up to a month) | Info; document, or have the engine force a decision on the re-arm bar |
| F7 | minor | `backtest/engine.py:108` | `killed_on` is first trip only | Add `kill_trips` count or list of dates to the report |
| F8 | nit | `tests/backtest` | No direct unit test for `_bars_at` vs `get_indexer` | Add a small equivalence test including a missing ts |
| F9 | nit | `strategies/` | No test that tickers in YAML are strings | Add: every `universe` entry of every strategy is `str` |

## Mutation and test-strength review (by reading)
| Mutation | Caught by | Verdict |
|---|---|---|
| `bars_paused >= pause_bars` changed to `>` | `test_pause_then_rearm_books_event_and_resets_peak` | Caught |
| Trip test applied on the re-arm bar | structure (return before test) | Equivalent |
| Re-arm peak = old peak instead of equity | same test | Caught |
| `from_state` drops `bars_paused` | `test_state_round_trips_mid_pause_and_after_rearm` | Caught |
| `mirror_kill_switch` skips re-arm clearing | none | Survives (F3) |
| `_bars_at` dict built from wrong index | goldens | Caught |

## Test gaps
- **G1 (major):** runner mirror on REARMED (F3).
- **G2 (minor):** restore mid-pause with `last_equity > peak` (F4).
- **G3 (minor):** smart preset in a bear fixture, SPY exposure <= 0.30 (F2).
- **G4 (minor):** survivorship label surfaces (F1).

## Re-audit needed? Yes, light
Triggers: F1, F2, F3 and F4 changes. A diff review is enough; the goldens are unaffected unless the kill-switch or fallback defaults change (a change to the default `kill_switch_pause_bars` would need a new golden note).
