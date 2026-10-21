# ADR-0001: Architecture and module contracts

- **Status:** accepted
- **Date:** 2026-10-20
- **Decider:** PM
- **Input from:** SPEC-001 (data), SPEC-002 (strategy/backtest), SPEC-003 (execution/portfolio/risk), SPEC-004 (platform), SPEC-005 (test strategy)

## Context
The five leads wrote their specs in parallel. Each spec is sound on its own terms, but they disagree at the seams: the execution interface, timestamps, fixtures, money storage, and who computes which indicators. This ADR fixes those seams. **Where a spec and this ADR disagree, this ADR wins.** Each spec gets the amendments listed in §4 before its tasks are dispatched.

## 1. Architecture

```
            ┌────────────── tradebot.data (001) ──────────────┐
            │ MarketData.get_bars / latest_bar / calendars    │
            └───────────────┬─────────────────────────────────┘
                            │ completed daily bars (float OHLCV, UTC)
            ┌───────────────▼─────────────────────────────────┐
            │ tradebot.backtest.driver.BarDriver (002)        │  ← the ONE per-bar loop
            │  builds Bar(+vol20, atr14) → engine.process_bar │
            │  → strategy.evaluate → TargetBook → submit      │
            └──────┬───────────────────────────────┬──────────┘
                   │                               │
     tradebot.strategy (002)          tradebot.portfolio.PortfolioEngine (003)
     schema / indicators / evaluate   sim / risk / ledger / kill switch
                   │                               │
     run_backtest (002) ──uses BarDriver──┐  ┌──uses BarDriver── runner (004)
                                          ▼  ▼
                          results / EngineState → SQLite (004) → API → Streamlit
```

**Principle:** there is **one code path** for strategy evaluation, execution and the per-bar loop. Backtesting and paper trading differ only in where the bars come from and where the results are stored.

## 2. Decisions

### D1. Bar time convention
- Bars are labeled by **bar open date at 00:00 UTC** (SPEC-001). Equities use the session date; crypto uses the UTC day. Use `bar_close_time()` for the moment a bar becomes known.
- Data and strategy use tz-aware `pd.Timestamp`. Execution uses `asof: date = ts.date()`. The conversion happens **only in `BarDriver`**.
- Only completed bars are ever returned. Missing sessions are not forward-filled.

### D2. Price basis
**Fully adjusted OHLC** per SPEC-001: the factor `Adj Close / Close` is applied to open, high, low and close alike, so fills and marks share one basis. This includes dividends, so the SPY benchmark is total-return. That is fairer, and SPEC-003 Q3 is resolved this way. Forward chain-linking is accepted, and rebases happen only between experiments.

### D3. Execution interface: SPEC-003's `PortfolioEngine` is canonical
- SPEC-002's proposed `ExecutionPort` is **dropped**.
- **`process_bar(asof, bars)`** replaces `on_open` + `on_close`. It runs steps A–F in one call: fill at the open, mark at the close, transfers, stops, snapshot and kill switch, then the rebalance decision. This matches the order in SPEC-002's loop.
- **`submit_target_weights(sleeve_id, weights, asof)`** replaces `on_signals`. Strategies emit target weights, never orders.
- **`TargetBook`** (new, owned by 002, file `backtest/driver.py`) translates a `SignalSet` into target weights:
  - **Book contents:** the book holds the weights of the sleeve's `long` intents from its latest **decision bar**. Any symbol not marked `long` on a decision bar has target 0.
  - **When the driver submits:** it calls `submit_target_weights` for a sleeve only (a) on that sleeve's decision bars, and (b) on every sleeve on a bar where `BarResult.rebalance_planned` is true.
  - **Why only those bars:** submitting every bar would cause daily drift trades in monthly sleeves.
  - **Rules sleeves:** they are daily, so they submit daily, and their intents are complete. A held symbol always gets an explicit long or flat; a flat symbol gets long only when its entry fires.
  - **Stop-lock re-arm:** "absent means 0" re-arms SPEC-003's stop lock naturally. This resolves SPEC-003 Q4: the lock lasts until the strategy goes flat once.
- **`held` for `evaluate()`** is derived by the driver from `engine.snapshot()`. SPEC-003 adds **`entry_asof: date`** to each position snapshot. It is set on a fill from flat and cleared at flat.
- **Closed trades, equity series and exposure** are **pure functions of the ledger and snapshots**: `backtest/trades.py`, owned by 002. `exit_reason` is mapped from `OrderOrigin`:
  | `OrderOrigin` | `exit_reason` |
  |---|---|
  | `STRATEGY` | `signal` |
  | `STOP` | `stop` |
  | `REBALANCE` | `rebalance` |
  - SPEC-003 adds no trade-tracking API.

### D4. Indicators for sizing and stops
- `vol20` and `atr14` are computed with **`tradebot.strategy.indicators.vol` and `.atr`**, using SPEC-002's formulas: sample std with ddof=1 of simple returns, and Wilder ATR.
- They are attached to each `Bar` **by `BarDriver`**, not by `evaluate()`.
- SPEC-003 never reimplements them.

### D5. Stops
- Stop parameters are declared in the **strategy spec**: the `stops: [atr_trailing]` field (SPEC-002). The driver copies `mult` into `SleeveConfig.trailing_stop_atr_mult`.
- v1 supports one ATR period (14) per engine.
- Trigger semantics follow SPEC-003: **`close <= stop_level`**, close-based, with the exit filled at the next open.

### D6. Engine clock and annualization
- The engine clock is **the union of all dates on which any symbol has a bar**. Crypto trades on weekends, and equity orders wait for that symbol's next bar (SPEC-003 Q1 accepted).
- Metrics use **P = 252** for equity-only runs and **P = 365** for any run containing crypto, with calendar-day sampling (SPEC-002).

### D7. Money
- In memory: `Decimal`, using **SPEC-003 §2's quanta and rounding table**. That table is the single source of truth, and SPEC-005's hand-worked cases must be re-checked against it.
- Equities trade in whole shares; crypto in 1e-8 units. No shorting and no leverage.
- **In SQLite, money and quantities are stored as `TEXT` Decimal strings** (canonical `str(Decimal)`). SPEC-004's integer `*_cents / qty_e8 / price_e6` columns are replaced. The reasons:
  1. They match `EngineState` JSON one to one.
  2. No second rounding layer is introduced.
  3. The API returns decimal strings anyway, and nothing aggregates money in SQL.

### D8. Persistence and the runner
- The runner commits **one transaction per portfolio per bar** containing:
  - `BarResult.new_ledger_events`
  - order updates
  - the snapshot
  - an `export_state()` checkpoint
  - the `run_log` row (unique on portfolio and bar)
  - the `last_processed_bar_ts` advance
- On start, it restores from the latest checkpoint and calls `verify()`.
- Normalized tables exist for the API to read. The **ledger plus checkpoint is the source of truth.**
- Catch-up **replays missed bars in order**, capped by `MAX_CATCHUP_BARS` (SPEC-004 accepted).
- Benchmarks are ordinary single-sleeve portfolios on the same path.
- A tripped kill switch also **freezes rebalance** (SPEC-003 Q5 accepted).
- Resets go through a CLI command only, which calls `reset_kill_switch`. The API "ack" endpoint only acknowledges; it **does not reset**.

### D9. Fixtures: synthetic and committed
The repo is **public**, so we do **not** commit Yahoo or exchange data snapshots, which carry licensing risk.
- **Committed test fixtures** are **synthetic, seeded** series covering:
  - **Symbols:** all 17 ETFs plus BTC, ETH and SOL
  - **Dates:** 2018-01-02 to 2024-12-31. SOL starts on 2020-04-10, so the late-listing case is tested.
  - **Calendar:** real XNYS sessions for equities, every day for crypto
  - **Price paths:** regime-switching random walks with a crash period, a bear period and injected edge cases, written by `scripts/make_fixtures.py`
- They are stored in **SPEC-001's parquet cache layout**, so tests exercise the real cache read path.
- Budget: ≤ 2.5 MB total, with a sha256 manifest.
- Golden backtests (SPEC-005 G1–G4) pin results on these synthetic fixtures.
- **Real-data backtests** run locally from the user's own cache and are git-ignored. They are for research, not for CI.
- Regenerating fixtures or updating goldens requires a PM-approved note (SPEC-005 §5).

### D10. Strategy semantics (SPEC-002 open questions)
- **Rotation:** `filter_mode: pre_rank`, filtering by the 200-day SMA first and ranking after. The IEF fallback is **unconditional**.
- **Trade-count gate:** ≥ 30 closed round trips over the **full** history. The out-of-sample count is reported alongside.
- **Data history gate:** measured on the **strategy's calendar** (the union of its universe), not on each symbol. SOL's shorter history is allowed, and the NaN-means-ineligible rule handles it.
- **Default gate thresholds** (SPEC-002 proposals, accepted and PM-tunable via `GateConfig`):
  - Walk-forward: ≥ 60% of windows have Sharpe > 0 and drawdown ≤ benchmark.
  - Sensitivity collapse: Sharpe < 0.5× base, or drawdown > 1.5× base, or the return flips sign.
- **Risk-free rate:** 0%.
- **Mixed-portfolio report:** shows SPY, BTC and a static blend.

### D11. Ownership of shared files
- **platform-lead owns** `pyproject.toml`, `.github/` and the unified **`tradebot` CLI** (`src/tradebot/cli.py`). Other areas expose functions, and platform wires the subcommands, including `data refresh/verify/rebase`.
- **Dependency requests** from 001, 002, 003 and 005 are applied by **one platform task (Wave 1)**, so pyproject edits never collide.
- **Test infrastructure is owned by quality-lead / SPEC-005:**
  - `tests/conftest.py`
  - the network-blocking guard
  - the hypothesis profiles
  - the injected `Clock` protocol (`tradebot/clock.py`)

### D12. Process and tooling
- **CI:** Ubuntu only, Python 3.12.
- **Lint:** `ruff check` plus `ruff format --check`.
- **Tests:** pytest with the hypothesis `ci` profile.
- **Coverage gates:** per SPEC-005 §9, enforced once each area lands. The gate does not apply while a module is empty.
- **Mutation testing:** deferred until after Phase 3.
- **Strategy cards:** fall back to backtest metrics, clearly labeled "backtest", until a paper portfolio has a year of history.
- **Portfolio creation:** CLI only.

## 3. Build order (dispatch waves)
Tasks within a wave can run in parallel when their file lists don't overlap.

| Wave | Contents | Gate to next |
|---|---|---|
| 1. Foundations | 004 tooling task (all deps, ruff, CI) · 005 test infra (conftest, net guard, Clock, fixture generator + synthetic fixtures) · 003 T1 money · 002 T2 indicators · 001 calendar + interfaces | CI green, fixtures committed |
| 2. Data + schema | 001 cache/providers/validation · 002 T1 schema · 003 T2–T5 config, orders, costs, ledger | unit tests green |
| 3. Engine | 003 T6–T10 (risk, stops, kill switch, SimBroker, rebalancer, `PortfolioEngine`) · 002 T3 evaluate | quality-lead audit (mandatory: money/fills) |
| 4. Backtest | 002 `BarDriver` + `TargetBook` + `trades.py` (new, per D3), T4 metrics, T5 engine, T6 validation · 003/005 property tests · goldens G1–G4 | audit + goldens pinned by PM |
| 5. Platform | 004 DB, runner, scheduler, API, dashboard | restart/idempotency tests green |

## 4. Spec amendments required before dispatch
| Spec | Amendment |
|---|---|
| 001 | Fixtures become synthetic (D9), and the committed real-data snapshot is dropped. The CLI moves to platform (D11). Confirm the full-adjustment basis is applied to open, high, low and close. |
| 002 | Remove `ExecutionPort`/`make_execution`. Add `BarDriver`, `TargetBook` and `trades.py` per D3/D4. Use `PortfolioEngine` signatures and `asof: date` at the boundary. Stop trigger uses `<=`. |
| 003 | Add `entry_asof` to position snapshots (D3). `vol20`/`atr14` come from `strategy.indicators` via the driver (D4). Persistence is TEXT Decimals (D7). |
| 004 | Money columns become TEXT Decimal (D7). Transactions are per portfolio per bar, with an `export_state` checkpoint (D8). Kill-switch ack does not reset; reset is CLI only. Add a unified CLI, and one Wave-1 deps task covering every spec's dependencies (D11). |
| 005 | Fixtures become synthetic parquet in the cache layout, 2018–2024 (D9). Re-check §6 hand-worked numbers against SPEC-003's rounding table. CI is Ubuntu only. Mutation testing is deferred. |

## 5. Addendum: decisions from the amendment round (2026-10-20)

**A1. TargetBook is persisted.**
- **Why:** the book is state. A runner that restarted without it would submit empty weights on the next rebalance bar and liquidate the sleeves.
- **What to persist:** `TargetBook.state()` is stored inside the same per-bar `engine_checkpoint` row (a `target_book_json` column), and is passed back to `BarDriver(..., book=restored)`.
- **Who:** 004 adds the column; 002 defines `state()` and `from_state()`.

**A2. Registries.**
- `tradebot.strategy.indicators.INDICATORS: dict[str, Callable]` maps every public indicator name to its function. It is part of 002 T2 (Wave 1).
- `tradebot.strategy.schema.STRATEGIES` maps strategy ids to the built-in specs. It is part of 002 T1 (Wave 2).
- SPEC-005's lookahead-completeness meta-test iterates both.

**A3. Duplicate-order protection.**
- This comes from three things together:
  - the unique `run_log(portfolio_id, bar_ts)` row
  - the single transaction per portfolio per bar
  - SPEC-003's deterministic order ids (`O{n:08d}`), restored from the checkpoint
- There is no separate idempotency key. SPEC-005's key-based test becomes "re-running a committed bar raises or no-ops, and the ledger is unchanged".

**A4. Runner and restart tests** live in `tests/platform/` with the `restart` marker, following SPEC-004. Fault points are `after_driver`, `before_commit` and `after_commit`.

**A5. Small calls.**
- Volume stays unadjusted for splits in v1, since no v1 strategy reads volume.
- A kill-switch trip drops any pending `RebalancePlan` (SPEC-003).
- `exit_reason` gains `manual`.
- SPEC-002 restricts stops to ATR(14), one per strategy.
- `.gitignore` is owned by platform-lead. SPEC-005 T2 may append `.hypothesis/` once.

**A6. Wave 1 dispatch order.**
1. 004 T1 (deps, lock, CI). Everything else needs the packages.
2. In parallel:
   - 005 T1 clock
   - 005 T2 conftest and guards
   - 005 T3 fixtures
   - 001 T1 types and interfaces
   - 001 T3 calendar
   - 003 T1 money
   - 002 T2 indicators
3. In parallel: 003 T11a money tests, 002 T7a indicator tests, 005 T4 fixture-manifest test.
4. Reviewer on every task. Quality-lead audit on 003 T1 (money is mandatory) and 005 T3 (fixtures are mandatory).

## 6. Addendum: Wave 3 decisions (2026-10-21)

**A7. Hold-while on unknown data.** A rules strategy with no explicit `exit` exits when its entry condition is not *confirmed* true. NaN/unknown counts as "not confirmed". This moves toward cash on data gaps, which is the safe direction, at the cost of an occasional extra round trip. An explicit `exit` that evaluates to unknown holds. SPEC-002 §2 is read accordingly.

**A8. Missing volatility on held symbols.** A full exit is never blocked. A partial **trim** of a vol-scaled holding with no `vol20` is rejected (`NO_VOL_DATA`) until vol data exists, per SPEC-003 §5–6.

**A9. Risk caps are measured at order acceptance** (SPEC-003 I10). Slippage and later drift can leave a realised weight slightly above 25%/30%, and that is not force-sold.

**A11. Blend benchmark is monthly-rebalanced**, not static. It runs through the same engine and costs as the fund. That makes it the harder bar, so gate 3 is not flattered. It stays named `blend` (no golden re-pin).

**A10. Lean process from Wave 4** (owner decision): see CLAUDE.md "Lean mode".

## Consequences
- **One loop (`BarDriver`) and one engine** mean a backtest result and a paper result for the same bars must match exactly. SPEC-005 can test that directly.
- **Synthetic fixtures** keep the public repo clean and the goldens stable, but CI never sees real market data. Real-data sanity checks are a manual, local step before any strategy passes the gate.
- **Submitting targets only on decision and rebalance bars** keeps monthly sleeves from churning. As a result, vol-scaled weights drift between decisions, which is intended.
- **TEXT Decimal storage** costs a little disk space and SQL aggregation, but nothing in v1 needs either.
