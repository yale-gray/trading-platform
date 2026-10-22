# Audit 004: Wave 5 platform (db, runner, api, cli, dashboard)

- **Auditor:** quality-lead
- **Date:** 2026-10-21
- **Code:** `src/tradebot/{db,runner,api}/` (about 3.2k lines), `src/tradebot/cli.py` (553), `dashboard/` (about 650)
- **Tests:** `tests/platform/` 100 passed in 34.7 s; `ruff check .` clean; mypy not run
- **Against:** SPEC-004 (incl. ACs), SPEC-005 runner restart section and T19, ADR-0001 D8 and section 5 (A1, A3, A4), CLAUDE.md hard rules
- **Verdict: PASS-WITH-NOTES**

No blocker. Idempotency, restart and runner == backtest hold, and I re-verified them independently with a throwaway probe test (deleted afterwards). The notes are minor input-validation gaps, an architectural wart in `reset-from`, and checkpoint growth.

## Scope
All Wave 5 uncommitted code and `tests/platform/`. Not in scope: data, engine, backtest internals (audited earlier); real APScheduler timing.

## Evidence
- `uv run pytest tests/platform -q`: 100 passed. `ruff`: clean.
- Probe 1 (fund + SPY + BTC benchmarks, fixture 2019-01-02..2019-12-31, one `Runner.tick`): for all 3 portfolios the persisted `ledger_digest` equals `events_digest` of an independent `run_backtest` ledger, and final equity matches to the cent (SPY 117389.63, BTC 206696.69, fund 109170.51).
- Probe 2 (stale runner): runner A holds a context at 2019-12-31; runner B advances the DB to 2020-01-02; A's next tick gets `RunnerError: stale context ... (another writer?)` for all 3 portfolios, and ledger counts are unchanged (14/15/701).
- API on the probe DB: money fields are decimal strings (`"116955.00"`); `/portfolios`, `/equity` (367 points, with benchmark), `/positions` all sensible. `dashboard.client.ApiClient` over the TestClient transport returned the same equity string.
- Routes: exactly 3 non-GET (POST `/strategies`, POST `/backtests`, POST `/portfolios/{id}/kill-switch/ack`). No order or portfolio-create route (`test_api.py:151`).
- Offline e2e: CLI `db init` -> `portfolio create` -> `runner tick` is covered by `test_cli.py:131,168` (monkeypatched offline). My probe covered runner -> API -> dashboard client directly. No break found.

## Checklist
| Item | Result | Evidence |
|---|---|---|
| Lookahead | n.a. (runner has no signal code; `BarDriver.step` is the only place that decides) | `tick.py:221` |
| Costs both directions | n.a. (engine, same code as backtest; ledger digest equal) | probe 1 |
| Accounting invariants | Y | `restore_context` runs `verify()` plus ledger count and digest cross-check `bootstrap.py:180-189` |
| Determinism / ledger equality | Y | probe 1; `test_runner_catchup.py:259` |
| Decimal | Y (TEXT columns, `to_text`/`from_text`; sums done in Python) | `repo.py:48-55` |
| Benchmark | Y (`benchmark_equity` on the fund's curve) | `tick.py:265,329` |
| Offline tests | Y | tests use the `offline=True` fixture cache |
| Restart/idempotency | Y | see below |

## Idempotency and restart
- One `BEGIN IMMEDIATE` transaction per portfolio per bar (`engine.py:49-51`, `tick.py:251-317`) covers ledger, orders, snapshots, kill-switch mirror, portfolio/sleeve rows, checkpoint, run_log and marker. Y.
- Fault points: `after_driver` and `before_commit` raise before commit and roll back everything; `after_commit` leaves a complete bar. `test_runner_idempotency.py:130,172` check the final fingerprint against an uninterrupted run. Y. The subprocess `os._exit` test is present and marked slow (`:363`).
- Double run: the marker guard (`tick.py:253`), the unique run_log and checkpoint keys, and the ledger `seq` primary key. Y. Test `:76,:223`.
- Stale second runner: the marker check inside the write transaction (`tick.py:257`) plus the flock (`lock.py`). Verified in probe 2. Y.
- Catch-up: `due_dates` is a gap-free ascending prefix (`clock.py:52`). The cap is enforced before any processing (`tick.py:147`). A missing bar stops the sequence (`:210`). Y.
- TargetBook: written in the checkpoint (`tick.py:250`) and restored (`bootstrap.py:190`). Y.
- Restore cross-checks: marker == checkpoint bar, ledger count, ledger digest, `verify()`, config mismatch -> `StateCorruptionError`. Y. Tests `:280,:315`.
- After any failure the context is dropped and restored from the DB (`tick.py:157,163`). Y.

## Kill switch
- The API ack only sets `acknowledged_at` (`repo.py:521-532`), and a later trip clears it (`:503`). It never resets. Test `test_api.py:128`.
- CLI reset holds the flock for the whole operation (`cli.py:235-254`). The reset writes one ledger event, one `reset` audit row and a replaced checkpoint at the same bar in one transaction (`bootstrap.py:212-248`). Test `test_runner_killswitch.py:50` asserts `[trip, ack, reset]` and a clean `restore_context`, and the runner continues after it. Y.

## Findings (ranked)
| # | Severity | file:line | Issue | Required fix |
|---|---|---|---|---|
| F1 | minor (design) | `cli.py:288-372` | `reset-from` is a 85-line transaction in the CLI that reaches into `restore_context`, `engine.cancel_order`, `repo.*` and writes the marker directly. Logic outside the runner package, so the marker/checkpoint invariants are maintained in two places. Behaviour is correct: it cancels PENDING orders with ledger events, writes a checkpoint labelled DATE-1 with unchanged state and TargetBook, advances the marker, logs an `outcome="reset"` run_log row, and there is no key collision because the label is greater than the last bar. | Move it to `runner.bootstrap.reset_from(db, name, date, now)` and have the CLI call it, like `reset_kill_switch`. Do it now; it is cheap and keeps one writer of the marker. |
| F2 | minor | `cli.py:320-323`, `tick.py:329-335` | `reset-from` is per portfolio. If only the fund is reset, the benchmark portfolio is still cap-refused; the fund's `benchmark_equity` then silently becomes NULL for the post-gap bars (no snapshot at that ts). No warning. Also no check that DATE is not in the future, and the retained TargetBook is not cleared (a pending rebalance plan from before the gap survives). | Print a hint to reset benchmarks too (or add `--all`); reject DATE after today; document or clear the stale plan (needs execution-lead's view of TargetBook semantics). |
| F3 | minor | `routers/backtests.py:56-62`, `jobs.py:30` | POST `/backtests`: override values are not validated (`{"initial_capital": "abc"}` returns 202 and fails later as a `failed` row); `end` in the future is accepted. Errors are recorded, nothing is unsafe. | Type the overrides (Decimal > 0, rf in a range) and cap `end <= today`. |
| F4 | minor | `schemas.py:136-139`, `routers/strategies.py:42-56` | POST `/strategies`: `name` has no min/max length or charset (a 10k-char name with a valid schema would be stored); schema validity itself is enforced by `load_strategy`, market is cross-checked. | `Field(min_length=1, max_length=80, pattern=...)`. |
| F5 | nit | `routers/backtests.py:121` | Backtest trades report `slippage=0` hard-coded, which is misleading (backtests do charge slippage). | Return null or the real value. |
| F6 | nit | `tick.py:265` | A missing benchmark snapshot gives `benchmark_equity=None` silently. | Log at warning. |

## Growth risk (item 7)
Measured on the fixture, 3 portfolios, daily bars, `checkpoint_keep=30` plus first-of-month retention:
- Year 1: DB 13.1 MB (WAL included); 11.4 MB of it is `state_json`. Fund checkpoint size grows with the ledger: 5 KB at bar 1, 297 KB at about 670 events, 307 KB at 691 events (about 440 B per event). Benchmark checkpoints stay at 5 KB. The fund logs about 690 events per year.
- Year 3 (linear extrapolation, about 2,100 events, about 0.9 MB per fund checkpoint, 30 newest plus 36 monthly): about 45 to 55 MB total. Write amplification is about 0.9 MB per bar by then, plus a full ledger digest at every restore.
- Recommendation: fix later, not now. It is harmless at this scale. Schedule the change (checkpoint without the ledger and rebuild it from `ledger_event` rows, or checkpoint every N bars) before the ledger passes about 5k events (roughly year 7 on this trade rate, earlier with a busier strategy). Add a size line to `docs/ROADMAP.md`.

## Suite time (item 8)
Platform tests total 34.7 s. Over 2 s: `test_runner_equals_backtest_over_a_year` (7.5 s, already slow), `test_runner_equals_backtest_over_one_month_with_restarts` (2.29 s, `test_runner_catchup.py:232`, not marked). Mark `:232` slow. Near 1.5 to 1.7 s and already slow or borderline: `test_scheduler.py::test_runner_process_catches_up_and_exits_cleanly_on_sigint` (slow), `test_catch_up_after_five_missed_sessions...` (1.57 s). Setup of the API fixture (about 1 s once) is paid per module and is fine.

## Mutation and test-strength review (by reading only)
| Mutation | Caught by | Verdict |
|---|---|---|
| Drop the stale-context check `tick.py:257` | `test_runner_idempotency.py:255` (and probe 2) | Caught |
| Skip `after_commit` marker write | `:130` | Caught |
| Ack resets `tripped` | `test_api.py:128`, `test_runner_killswitch.py:50` | Caught |
| Drop TargetBook from the checkpoint | `test_runner_equals_backtest_*` with restarts | Caught (monthly rebalance path) |

## Test gaps
- **G1 (minor):** `reset-from` has a CLI test (`test_cli.py:276`) but none asserting that a pending order is cancelled and that the next tick equals a run that skipped the gap; add it with the F1 move.
- **G2 (minor):** no test for POST validation edges in F3/F4.
- SPEC-005 T19 item 6 (`test_restated_bar_raises`) is not part of the runner tests; I found no restatement check in `src/tradebot/runner`. It may be covered by the data layer's rebase rules (earlier audit); confirm.

## Re-audit needed? No
Unless F1 is implemented (then re-run `tests/platform/test_cli.py` and `test_runner_killswitch.py` only).
