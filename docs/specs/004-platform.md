# SPEC-004: Platform: persistence, paper runner, API, dashboard, tooling

- **Lead:** platform-lead
- **Status:** approved (conforms to ADR-0001)
- **Roadmap item:** Phase 3 (SQLite + runner), Phase 4 (API + dashboard)
- **Related ADRs/specs:** SPEC-001 (data), SPEC-002 (strategy/backtest), SPEC-003 (execution/sim/portfolio/risk), SPEC-005 (quality). **ADR-0001 (accepted; wins on every conflict: D3, D7, D8, D11, D12).**

## Goal
Provide everything that runs, stores and shows the bot: a SQLite schema, a restart-safe daily-bar paper runner, a read-mostly FastAPI service and a Streamlit dashboard, plus the unified `tradebot` CLI and uv/ruff/pytest/CI tooling. The platform only *calls* trading logic from 001/002/003; it contains none.

## Non-goals
- Trading logic (signals, sizing, fills, risk, metrics math). Owned by 002/003.
- Auth, multi-user, hosting, HTTPS, Docker. v1 is local, single user, single process.
- Intraday bars, websockets/live streaming, real broker code (nothing here can place a real trade).
- Alerts/notifications and the AI strategy builder (Phase 5).

## Design

### Process model
One Python process per component, all local:
- **Runner process** (`tradebot runner start`): APScheduler `BlockingScheduler` plus the processing loop. The only writer of execution state (portfolios, orders, ledger, snapshots, checkpoints, runner state).
- **API process** (`uvicorn tradebot.api.app:app`): reads the DB; writes only strategy configs, backtest-run requests and the kill-switch acknowledgement (see below). It never creates portfolios and never resets the kill switch.
- **Dashboard** (`streamlit run dashboard/app.py`): HTTP client of the API only. It imports no `tradebot.sim/risk/portfolio/strategy` code.
SQLite runs in WAL mode (`PRAGMA journal_mode=WAL; busy_timeout=5000; foreign_keys=ON`) so the API can read while the runner writes. Backtests triggered by the API run in a `ThreadPoolExecutor` inside the API process (v1; max 1 concurrent), status tracked in `backtest_run`. Multi-user/hosting would need Postgres, a job queue, and auth; none of the code below assumes SQLite-specific SQL beyond PRAGMAs in `db/engine.py`.

### Config (pydantic-settings)
`Settings` reads `.env` with prefix `TRADEBOT_`. Existing keys: `TRADEBOT_DB_PATH`, `TRADEBOT_CACHE_DIR`. Add (the lead updates `.env.example`): `TRADEBOT_API_HOST=127.0.0.1`, `TRADEBOT_API_PORT=8000`, `TRADEBOT_API_URL=http://127.0.0.1:8000` (used by dashboard), `TRADEBOT_LOG_LEVEL=INFO`, `TRADEBOT_LOG_DIR=./logs`, `TRADEBOT_MAX_CATCHUP_BARS=400`, `TRADEBOT_EQUITY_CLOSE_DELAY_MIN=30`. No secrets exist in v1 (public data only).

### Money in the DB (ADR-0001 D7)
All money **and quantity** columns are `TEXT` holding the canonical `str(Decimal)` of the in-memory value, using SPEC-003 §2's quanta and rounding table (no second rounding layer in the platform). This matches the `EngineState` JSON one to one. There are no `*_cents`, `qty_e8` or `price_e6` columns anywhere. Nothing in v1 aggregates money in SQL; sums and ordering happen in Python on `Decimal`. Dimensionless ratios the engine produces (drawdown) are also stored as `TEXT` Decimal fractions (`"0.0825"`). Integer columns remain only for ids, `seq`, counts and `target_weight_bp` (a configuration weight, not money). Metrics copied from 002 results (`total_return`, `sharpe`, etc.) stay float (analytics, not money).

### DB tables (SQLModel; all timestamps UTC, timezone-naive ISO in SQLite)
`id` is an integer PK unless stated. `created_at` is on every table (omitted below).

**strategy_config** (strategy definitions, versioned immutably)
| column | type | notes |
|---|---|---|
| id | int PK | |
| name | str | e.g. `core_etf_momentum` |
| version | int | unique with name; edits create a new row |
| market | str | `equity` or `crypto` (calendar selection) |
| schema_json | JSON | the 002 strategy schema, serialized verbatim (`StrategySpec.model_dump_json()`) |
| schema_hash | str | sha256 of canonical JSON; used for dedupe |
| is_template | bool | |

**backtest_run**
| id | int PK | |
| strategy_config_id | FK | |
| status | str | `queued/running/done/failed` |
| params_json | JSON | start, end, universe, fees/slippage overrides, oos_split, mode (`single`/`walk_forward`/`sensitivity`) |
| data_fingerprint | str | hash of input data manifest from 001 (reproducibility) |
| started_at / finished_at | datetime | |
| error | text null | |
| result_json | JSON null | full `BacktestResult` from 002 (metrics, benchmark comparison, OOS/IS, walk-forward, sensitivity) |
| total_return, cagr, sharpe, sortino, max_drawdown, n_trades | float/int null | denormalized from result_json for list/sort **[002: metric names]** |
| benchmark_symbol | str | SPY or BTC/USD |

**backtest_equity_point** (run_id, ts, equity, benchmark_equity, drawdown) with composite PK (run_id, ts). Floats are fine (analytics). **[002]** supplies the series.
**backtest_trade** (id, run_id, symbol, side, entry_ts, exit_ts, qty, entry_px, exit_px, pnl, fees, sleeve_key) **[002/003]**.

**portfolio**
| id | int PK | |
| name | str unique | `paper_fund`, `bench_spy`, `bench_btc` |
| kind | str | `fund` or `benchmark` |
| initial_cash | TEXT Decimal | `"100000.00"` for the fund |
| base_ccy | str | `USD` |
| status | str | `active/halted/archived` |
| started_at | datetime | portfolio inception bar |
| peak_equity | TEXT Decimal | copied from the engine snapshot each bar (003 owns peak tracking; the platform only stores it) |
| config_json | JSON | sleeve weights, risk limits, rebalance rule **[003]** |

**sleeve** (a strategy allocation inside a portfolio)
| id | int PK | |
| portfolio_id | FK | |
| key | str | `core`, `meanrev`, `crypto_trend`, `cash` ; unique with portfolio_id |
| strategy_config_id | FK null | null for the cash sleeve |
| target_weight_bp | int | 5000/2000/2000/1000 |
| market | str | `equity` / `crypto` (selects the calendar and bar clock) |
| cash | TEXT Decimal | sleeve cash copied from the engine snapshot each bar (the ledger remains the source of truth) |
| enabled | bool | |

**order** (updated from the engine's order updates in the per-bar transaction; field names and state machine follow 003)
| id | int PK | |
| portfolio_id, sleeve_id | FK | |
| symbol | str | |
| side | str | `buy/sell` |
| order_type | str | `market` only in v1 |
| qty | TEXT Decimal | |
| origin | str | `STRATEGY/STOP/REBALANCE` (003 `OrderOrigin`) |
| status | str | `pending/filled/cancelled/rejected` |
| signal_bar_ts | datetime | bar t that produced the order |
| fill_due_bar_ts | datetime null | bar when it may fill (that symbol's next bar; see D6) |
| reason | str | strategy tag / `rebalance` / `stop` / `kill_switch` |
| risk_note | text null | veto/shrink explanation from risk |
| idempotency_key | str UNIQUE | deterministic, taken from 003's `Order` (proposed `f"{portfolio_id}:{sleeve_id}:{symbol}:{signal_bar_ts}:{side}:{origin}"`) |

**ledger_event** (append-only; with the checkpoint, the source of truth) **[003: event types]**
| id | int PK | |
| portfolio_id, sleeve_id | FK | |
| ts | datetime | bar ts of the event |
| type | str | `fill/fee/deposit/rebalance_transfer/adjustment` (003) |
| order_id | FK null | |
| symbol | str null | |
| qty | TEXT Decimal null | signed |
| price | TEXT Decimal null | |
| fee, slippage | TEXT Decimal | |
| cash_delta | TEXT Decimal | signed |
| event_json | JSON | the event exactly as emitted in `BarResult.new_ledger_events` (lossless restore and `verify()`) |
| seq | int | monotonic per portfolio; unique (portfolio_id, seq) |

**position_snapshot** (portfolio_id, sleeve_id, ts, symbol, qty, avg_cost, mark_px, market_value, highest_close, entry_asof) composite PK (portfolio_id, sleeve_id, ts, symbol); all but `entry_asof` are TEXT Decimal; `entry_asof` is a date (ADR D3). Written each processed bar from the engine snapshot; stored for fast dashboard reads.

**equity_snapshot** (portfolio_id, sleeve_id nullable, ts, equity, cash, drawdown, benchmark_equity null), all TEXT Decimal; PK (portfolio_id, sleeve_id, ts). `sleeve_id` NULL means total portfolio.

**engine_checkpoint** (portfolio_id, bar_ts, state_json, ledger_seq, created_at) PK (portfolio_id, bar_ts). `state_json` is `PortfolioEngine.export_state()` serialized (Decimal strings, 003's `EngineState` JSON). One row is written per processed bar in the bar transaction. The runner prunes to the last `CHECKPOINT_KEEP` rows (default 30) plus the first row of each month. Restore uses the latest row.

**kill_switch_state** (one row per portfolio) **[003]**
| portfolio_id | PK/FK | |
| tripped | bool | |
| tripped_at_bar | datetime null | |
| trigger_drawdown | TEXT Decimal null | measured drawdown fraction at trip |
| peak_equity | TEXT Decimal | peak at trip |
| acknowledged_at | datetime null | human review flag set by the API ack (STRATEGY.md section 5); informational only |
| ack_note | text null | |
Mirrors the engine's kill-switch state (003 owns it; copied each bar). The API ack changes only `acknowledged_at`/`ack_note`. Plus **kill_switch_event** (id, portfolio_id, ts, kind `trip/ack/reset`, note) as audit log; `reset` rows are written only by the CLI `reset-kill-switch` command.

**runner_state** (idempotency anchor; ADR D8: per portfolio)
| portfolio_id | PK/FK | one row per portfolio |
| last_processed_bar_ts | datetime null | the last bar fully committed (advances in the bar transaction) |
| last_run_at | datetime | |
| last_status | str | `ok/error/data_unavailable` |
| last_error | text null | |
| consecutive_failures | int | |

**run_log** (portfolio_id, bar_ts, started_at, finished_at, outcome, n_orders_created, n_fills, detail_json) with UNIQUE (portfolio_id, bar_ts); defence in depth against double-processing.

**runner_heartbeat** (single row: ts, pid, version).

### Runner design

**Bar clocks**
- Equity-only portfolios (e.g. `bench_spy`): bar date D is *complete* once `now >= close(D) + EQUITY_CLOSE_DELAY` (default 30 min) where `close(D)` comes from the XNYS calendar (handles early 13:00 ET closes on e.g. Black Friday). Only sessions listed by the calendar count as bars.
- Crypto-only portfolios (e.g. `bench_btc`): bar date D (UTC day) is complete once `now >= (D+1 day) 00:00 UTC + 10 min` (let the exchange finalize the candle).
- Mixed portfolios (the fund) follow the engine clock (ADR D6, the union of all dates on which any symbol has a bar): date D is due when every market that has a bar on D is complete. On an XNYS session day that means the crypto rule, so the fund advances once a day at about 00:10 UTC.

**Exchange calendar library: `exchange_calendars`** (XNYS). It is the maintained successor of Quantopian's `trading_calendars`, gives sessions, `session_close`, early closes and holidays, and is the engine underneath `pandas_market_calendars`, which only adds a wrapper layer we don't need. Pure-Python wheels, offline, deterministic (holiday data ships with the package, so tests need no network). Pin a minor version since holiday tables change per release. `pandas_market_calendars` is the fallback if the API proves awkward.

**Processing a bar (ADR D8: one transaction per portfolio per bar)**
For portfolio P and the next unprocessed completed bar t (the first bar after `runner_state.last_processed_bar_ts`):
0. **Restore (once per process, and again after any failed bar):** load the latest `engine_checkpoint` row, rebuild the `PortfolioEngine` from `state_json`, then call `verify()` against the `ledger_event` rows. A `verify()` failure sets `last_status=error`, skips the portfolio and never trades. The in-memory engine is always discarded after a rollback and rebuilt from the checkpoint, so memory can never run ahead of the DB. A first-ever bar builds a fresh engine from `portfolio.config_json`.
1. **Load bars** through t from SPEC-001 `MarketData` (`refresh=False` after a tail `refresh`). If a bar that should exist at t is unavailable, set `last_status=data_unavailable`, leave `last_processed_bar_ts` unchanged and retry on the next tick.
2. **Drive:** call SPEC-002 `tradebot.backtest.driver.BarDriver` for bar t. The driver builds the `Bar`s (with `vol20`/`atr14`), calls `engine.process_bar(asof, bars)` (fill at the open, mark at the close, transfers, stops, snapshot, kill switch, rebalance), runs `strategy.evaluate` and `TargetBook`, and calls `submit_target_weights`. It returns a `BarResult`. **The runner re-implements none of fill, mark or decide.** Backtests and paper runs therefore share one code path and differ only in the bar source and the sink.
3. **Persist, in one `BEGIN IMMEDIATE` transaction:** (a) append `BarResult.new_ledger_events` to `ledger_event`; (b) insert/update `order` rows from the engine's order updates; (c) write `position_snapshot` and `equity_snapshot` (sleeve and total) and mirror `kill_switch_state`; (d) insert the `engine_checkpoint` row from `export_state()`; (e) insert the `run_log` row (unique on portfolio and bar); (f) set `runner_state.last_processed_bar_ts = t`. **COMMIT.**
A crash before COMMIT leaves nothing behind and the next start restores from the previous checkpoint. A crash after COMMIT means the next start sees `last_processed_bar_ts = t` and moves on. The `run_log` UNIQUE, `order.idempotency_key` UNIQUE and `ledger_event (portfolio_id, seq)` UNIQUE constraints turn an accidental double-run into an IntegrityError that is caught, logged and rolled back, never a second trade. Because the kill switch also freezes rebalance (ADR D8), the runner needs no kill-switch logic of its own. A signal on bar t is filled by the engine at that symbol's next bar (t+1 for daily series), never on t (CLAUDE.md rule 2).
**Test seam:** `tick` accepts an optional `fault_hook(point: str)`, called at `after_driver`, `before_commit` and `after_commit`, active only in tests (SPEC-005).

**Portfolio ordering within a tick:** process due bars in ascending bar timestamp, then by portfolio id. There is no portfolio-level rebalance step in the runner; the rebalance happens inside `process_bar`.

**Benchmark portfolios** `bench_spy` and `bench_btc` are ordinary `portfolio` rows (`kind=benchmark`) with one sleeve whose strategy is a "buy at first bar, hold" config from 002, run through `BarDriver` exactly like the fund (ADR D8). Each fund `equity_snapshot` stores the matching benchmark equity so the dashboard can chart relative performance without calculating anything.

**Catch-up decision: replay missed bars in order.** If the machine was off for N days, the runner processes every missed bar sequentially through `BarDriver` using historical bars (fill at that bar's real open), not skip. Reasons: (1) Skipping would leave pending orders orphaned and strategy state (SMA windows, 5-day exits, trailing-stop highs, monthly rebalance) inconsistent, so the paper fund would diverge from what the backtester would have produced for the same period, which defeats the "same code as backtest" guarantee. (2) The equity curve and drawdown stay continuous, so the 3-month forward test and benchmark comparison are valid. (3) It is lookahead-safe because each step only sees data up to its own bar. (4) Daily bars make replay cheap. Safeguards: replayed bars are flagged `catchup: true` in `run_log.detail_json` for audit; if the gap exceeds `MAX_CATCHUP_BARS` (400) the runner refuses, sets `last_status=error`, and requires the human-run CLI `tradebot runner reset-from PORTFOLIO DATE`. A portfolio started for the first time begins at "the latest completed bar" (no backfill) unless `tradebot portfolio create --start DATE` is given.

**Scheduler jobs (APScheduler 3.x, `SQLAlchemyJobStore` not used; jobs are re-registered at startup, state lives in `runner_state`)**
| job id | trigger | does |
|---|---|---|
| `startup_catchup` | once at start | `tick(now)` which replays any missed bars |
| `equity_close` | cron Mon-Fri 16:35 `America/New_York`, `misfire_grace_time=6h`, `coalesce=True` | `tick(now)` filtered to equity-only portfolios |
| `equity_retry` | interval 15 min, only active 16:35-23:59 ET Mon-Fri (guard inside the job) | retry if the daily bar was not yet available from yfinance |
| `crypto_close` | cron daily 00:10 `UTC`, `misfire_grace_time=6h`, `coalesce=True` | `tick(now)` filtered to portfolios containing crypto (the mixed fund included) |
| `crypto_retry` | interval 15 min | same retry for crypto |
| `heartbeat` | interval 5 min | write `runner_heartbeat` (single row table: `ts`, `pid`, `version`) for the dashboard status badge |
Every job body is simply `tick(now)`; `tick` computes "due bars" per portfolio from calendar + `runner_state`, so the cron timings are only nudges and *correctness never depends on a job firing on time*. A process-level lock (`max_instances=1`, plus a `runner.lock` file with `fcntl.flock`) prevents two runners. Holidays: on days with no bar for a portfolio's union calendar, `tick` finds no due bar and no-ops. DST is handled by cron in the `America/New_York` tz.

### API (FastAPI, `/api/v1`, JSON, no auth, bound to 127.0.0.1)
Responses use pydantic models in `api/schemas.py`. Money is returned as the stored decimal strings (`"100000.00"`, passed through from the TEXT columns) so the UI never touches floats of money; metrics are floats. Time series are `list[Point]` for easy plotting.

```python
class Money(RootModel[Decimal]): ...          # serialized as string "12345.67"

class PortfolioSummary(BaseModel):
    id: int; name: str; kind: Literal["fund","benchmark"]; status: str
    equity: Money; initial_cash: Money; total_return_pct: float
    max_drawdown_pct: float; kill_switch_tripped: bool
    last_processed_bar: datetime | None; started_at: datetime

class SleeveBreakdown(BaseModel):
    key: str; strategy_name: str | None; target_weight_pct: float
    actual_weight_pct: float; equity: Money; return_pct: float; n_positions: int

class PositionOut(BaseModel):
    sleeve_key: str; symbol: str; qty: Decimal; avg_cost: Decimal
    mark_price: Decimal; market_value: Money; unrealized_pnl: Money; weight_pct: float

class TradeOut(BaseModel):                     # one fill [003]
    ts: datetime; sleeve_key: str; symbol: str; side: str; qty: Decimal
    price: Decimal; fee: Money; slippage: Money; reason: str

class EquityPoint(BaseModel):
    ts: datetime; equity: Money; drawdown_pct: float; benchmark_equity: Money | None

class KillSwitchOut(BaseModel):   # read-only view; there is no API reset
    tripped: bool; tripped_at_bar: datetime | None; drawdown_pct: float | None
    threshold_pct: float; acknowledged_at: datetime | None; note: str | None

class PortfolioDetail(PortfolioSummary):
    sleeves: list[SleeveBreakdown]; kill_switch: KillSwitchOut
    benchmarks: list[PortfolioSummary]

class StrategyCard(BaseModel):                 # NexusTrade-style perf card
    strategy_id: int; name: str; version: int; market: str
    return_1y_pct: float | None; max_drawdown_pct: float | None; sortino: float | None
    benchmark_symbol: str; benchmark_return_1y_pct: float | None
    source: Literal["paper","backtest"]; as_of: datetime | None

class StrategyConfigIn(BaseModel):  name: str; market: str; schema_: dict
class StrategyConfigOut(StrategyConfigIn): id: int; version: int; created_at: datetime

class BacktestRunIn(BaseModel):
    strategy_id: int; start: date; end: date | None = None
    mode: Literal["single","walk_forward","sensitivity"] = "single"
    overrides: dict = {}                       # fees/slippage etc. [002]
class BacktestRunSummary(BaseModel):
    id: int; strategy_id: int; status: str; started_at: datetime | None
    finished_at: datetime | None; total_return_pct: float | None
    sharpe: float | None; sortino: float | None; max_drawdown_pct: float | None
    n_trades: int | None; benchmark_symbol: str
class BacktestRunDetail(BacktestRunSummary):
    params: dict; result: dict | None          # 002 BacktestResult as dict
    equity: list[EquityPoint]; trades: list[TradeOut]; error: str | None

class RunnerStatus(BaseModel):
    alive: bool; last_heartbeat: datetime | None
    sleeves: list[dict]                        # key, last_processed_bar, last_status, last_error
class Health(BaseModel): status: str; db_ok: bool; version: str
```

| Method + path | Request | Response | Notes |
|---|---|---|---|
| GET `/health` | | `Health` | |
| GET `/runner/status` | | `RunnerStatus` | derived from `runner_state` + heartbeat |
| GET `/portfolios` | | `list[PortfolioSummary]` | |
| GET `/portfolios/{id}` | | `PortfolioDetail` | |
| GET `/portfolios/{id}/equity?from=&to=&sleeve=` | query | `list[EquityPoint]` | includes benchmark and drawdown, precomputed at write time |
| GET `/portfolios/{id}/positions` | | `list[PositionOut]` | latest snapshot |
| GET `/portfolios/{id}/trades?limit=&offset=&symbol=&sleeve=` | query | `Page[TradeOut]` | from ledger fills |
| GET `/portfolios/{id}/orders?status=` | query | `list[OrderOut]` | pending orders **[003]** |
| GET `/portfolios/{id}/kill-switch` | | `KillSwitchOut` | |
| POST `/portfolios/{id}/kill-switch/ack` | `{note: str}` | `KillSwitchOut` | records human review only (`acknowledged_at`, `ack_note`, a `kill_switch_event` row). **It does not reset the switch** (ADR D8); reset is CLI only: `tradebot portfolio reset-kill-switch` |
| GET `/strategies` | | `list[StrategyConfigOut]` | |
| POST `/strategies` | `StrategyConfigIn` | `StrategyConfigOut` | validates with 002's pydantic schema; new version on name clash |
| GET `/strategies/{id}` | | `StrategyConfigOut` | |
| GET `/strategies/cards` | | `list[StrategyCard]` | 1-year return, max DD, Sortino; paper if >=1y of snapshots else latest backtest, flagged by `source` |
| POST `/backtests` | `BacktestRunIn` | `BacktestRunSummary` (202) | queues thread job |
| GET `/backtests?strategy_id=` | | `list[BacktestRunSummary]` | |
| GET `/backtests/{id}` | | `BacktestRunDetail` | |
Portfolio creation is by the CLI (`tradebot portfolio create`) in v1 (ADR D12), not the API, to keep POST surface small and avoid the API creating trading state. Rule: the API never calls broker/sim code and exposes no order-placing endpoint.

### Dashboard (Streamlit, `dashboard/`)
Multipage app, all data via an `ApiClient` (httpx) with `st.cache_data(ttl=60)`:
1. **Portfolios** (`app.py`): table from `/portfolios`, runner heartbeat badge, kill-switch warning chips.
2. **Portfolio detail** (`pages/1_Portfolio.py`): equity curve vs SPY/BTC benchmarks (Plotly, rebased to 100), drawdown area chart, positions table, trade log (paged), sleeve breakdown (stacked bar of target vs actual weights), kill-switch panel with ack button (the panel states that a reset needs the CLI command `tradebot portfolio reset-kill-switch`). Sidebar portfolio select.
3. **Strategies** (`pages/2_Strategies.py`): NexusTrade-style cards (`StrategyCard`): 1-year return, max drawdown, Sortino, with the benchmark return beside it and a "paper/backtest" label. Plain-language tooltips for each term (owner is new to investing).
4. **Backtests** (`pages/3_Backtests.py`): list of runs, run form, detail view with metrics table, equity vs benchmark, in-sample/out-of-sample split and walk-forward table.
Every page shows the "simulated money, not financial advice" banner. No page computes returns, drawdown, or sizes; it only formats API values (percent formatting, rebasing a series to 100 is presentational and permitted).

### Logging
`logging` + `dictConfig` in `tradebot/logging_config.py`: console plus rotating file `logs/runner.log` (5 x 5 MB), JSON-ish key=value lines with `portfolio`, `sleeve`, `bar_ts` fields on every runner log record.

### Interfaces consumed from other specs (per ADR-0001)
```python
# 001 data
MarketData.get_bars / get_bars_many / latest_bar / refresh / verify / rebase   # completed daily bars only; refresh(symbols)->QualityReport map
MarketData.data_fingerprint(...)          # recorded in backtest_run.data_fingerprint

# 002 strategy/backtest
tradebot.backtest.driver.BarDriver        # THE per-bar loop; runner and run_backtest both use it; returns BarResult
tradebot.backtest.run_backtest(...) -> BacktestResult
tradebot.backtest.trades                  # closed trades / equity / exposure as pure functions of ledger + snapshots
compute_card_metrics(equity, benchmark)   # platform must NOT reimplement

# 003 execution (PortfolioEngine is canonical)
PortfolioEngine.process_bar(asof, bars) -> BarResult   # BarResult.new_ledger_events, order updates, rebalance_planned
PortfolioEngine.submit_target_weights(sleeve_id, weights, asof)
PortfolioEngine.snapshot() / export_state() / verify() / reset_kill_switch(...)
# restore-from-state constructor (name TBD by 003, assumed PortfolioEngine.from_state(state)) must exist
```
The runner owns DB reads and writes. The engine and driver are pure with respect to the DB, so the same code is unit-tested without SQLite.

### Unified CLI (`src/tradebot/cli.py`, ADR D11)
`[project.scripts] tradebot = "tradebot.cli:main"`. **argparse**, not typer: it is stdlib (no dependency to pin), SPEC-001 already specified argparse, the tree is a plain two-level command set, and subcommand handlers import lazily so `tradebot --help` and the data commands work without the api/runner/dashboard groups. Other areas expose functions; platform wires them. `main(argv=None) -> int` so tests call it directly.
| Command | Does |
|---|---|
| `tradebot data refresh [--symbols S ...]` | `MarketData.refresh`, prints the `QualityReport` table |
| `tradebot data verify [--symbols S ...]` | `MarketData.verify`; nonzero exit on errors |
| `tradebot data rebase SYMBOL [--yes]` | `MarketData.rebase`; prints the new generation id; asks for confirmation unless `--yes` |
| `tradebot db init` | create the DB, WAL pragmas, schema version table |
| `tradebot portfolio create (--preset fund\|bench_spy\|bench_btc \| --name N --config FILE) [--start DATE]` | create portfolio, sleeves, runner_state, seed strategy configs (portfolio creation is CLI only) |
| `tradebot portfolio list` | table of portfolios, last bar, kill-switch state |
| `tradebot portfolio reset-kill-switch PORTFOLIO --note TEXT [--yes]` | **the only reset path**: restores the engine from the latest checkpoint, calls `reset_kill_switch`, then in one transaction writes a new `engine_checkpoint`, `kill_switch_state`, and a `kill_switch_event(kind=reset)`. Refuses while the runner lock is held |
| `tradebot runner start` | scheduler (same as `python -m tradebot.runner`) |
| `tradebot runner tick [--once] [--now ISO]` | process due bars and exit |
| `tradebot runner reset-from PORTFOLIO DATE` | human override for a gap above `MAX_CATCHUP_BARS` |
| `tradebot backtest run STRATEGY_FILE --start D [--end D] [--mode single\|walk_forward\|sensitivity]` | `load_strategy` + `run_backtest`, prints metrics (and stores a `backtest_run` row when `--save`) |
| `tradebot api serve [--reload]` | uvicorn on the configured host/port |

### Tooling plan (applied in one Wave-1 task, T1; ADR D11/D12)
Platform owns `pyproject.toml`, `.github/` and the CLI. The requests of SPEC-001, 002, 003 (stdlib only, none), 004 and 005 are all applied by T1 in a single edit, so pyproject never collides.

**Exact dependency set (T1 writes exactly these)**
| Where | Package and pin | Requested by |
|---|---|---|
| `dependencies` | `pandas>=2.2,<3` | 001, 002 |
| | `numpy>=1.26,<3` | 002 |
| | `pyarrow>=16` | 001 (parquet cache) |
| | `yfinance>=0.2.40` | 001 |
| | `ccxt>=4.3` | 001 |
| | `exchange-calendars>=4.5,<5` | 001, 004 |
| | `pyyaml>=6.0` | 002 (`safe_load` only) |
| | `pydantic>=2.7,<3` | 002, 004 |
| | `pydantic-settings>=2.3,<3` | 004 |
| | `sqlmodel>=0.0.22` | 004 |
| | `tzdata>=2024.1` | 004 |
| group `api` | `fastapi>=0.111`, `uvicorn[standard]>=0.30` | 004 |
| group `runner` | `apscheduler>=3.10,<4` (4.x is a different API) | 004 |
| group `dashboard` | `streamlit>=1.36`, `plotly>=5.22`, `httpx>=0.27` | 004 |
| group `dev` | `pytest>=8`, `pytest-cov>=5`, `pytest-timeout>=2.3`, `hypothesis>=6.100`, `httpx>=0.27` (TestClient), `ruff>=0.6` | 001, 002, 005, 004 |
Rejected: `freezegun`/`time-machine` (SPEC-005 uses an injected `Clock`), `pytest-socket`, `pytest-xdist`, `pandas_market_calendars`, `mypy` (not requested). `uv sync` installs every group through `[tool.uv] default-groups`. The lower bounds are floors chosen for current stable releases; `uv.lock` pins the exact versions. `exchange-calendars` stays below 5 because holiday tables change per release; add a known-dates test.

**Exact `pyproject.toml` (T1 replaces the file with this)**
```toml
[project]
name = "tradebot"
version = "0.1.0"
description = "Paper-trading bot and platform (simulated money only)"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "pandas>=2.2,<3",
    "numpy>=1.26,<3",
    "pyarrow>=16",
    "yfinance>=0.2.40",
    "ccxt>=4.3",
    "exchange-calendars>=4.5,<5",
    "pyyaml>=6.0",
    "pydantic>=2.7,<3",
    "pydantic-settings>=2.3,<3",
    "sqlmodel>=0.0.22",
    "tzdata>=2024.1",
]

[project.scripts]
tradebot = "tradebot.cli:main"

[dependency-groups]
api = ["fastapi>=0.111", "uvicorn[standard]>=0.30"]
runner = ["apscheduler>=3.10,<4"]
dashboard = ["streamlit>=1.36", "plotly>=5.22", "httpx>=0.27"]
dev = [
    "pytest>=8",
    "pytest-cov>=5",
    "pytest-timeout>=2.3",
    "hypothesis>=6.100",
    "httpx>=0.27",
    "ruff>=0.6",
]

[tool.uv]
default-groups = ["dev", "api", "runner", "dashboard"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/tradebot"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers --timeout=20 --ignore=tests/manual"
markers = [
    "unit: fast isolated tests",
    "property: hypothesis property tests",
    "integration: multi-module tests",
    "golden: pinned-result tests (changes need a PM-approved note)",
    "restart: runner crash/restart and idempotency tests",
    "slow: tests over 2 seconds",
]
filterwarnings = ["error"]

[tool.coverage.run]
branch = true
source = ["tradebot"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.ruff.lint.isort]
known-first-party = ["tradebot"]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["B011"]
```
Notes: the hypothesis profile (`ci`, `dev`) is registered in `tests/conftest.py`, owned by quality-lead, so pyproject sets no hypothesis option. No `fail_under` anywhere: coverage gates are enforced later by quality-lead's `scripts/check_coverage.py`, once the modules exist. `filterwarnings = ["error"]` is SPEC-005's rule; if a third-party warning breaks the smoke test, add a commented allowlist entry rather than removing the rule.

**Exact `.github/workflows/ci.yml`**
```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
jobs:
  lint-test:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      HYPOTHESIS_PROFILE: ci
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
          python-version: "3.12"
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest
```
Ubuntu only, Python 3.12 only. No secrets, no network tests, no scheduled runs, no coverage step yet.

**Run locally**
```bash
uv sync
cp .env.example .env
uv run tradebot db init
uv run tradebot portfolio create --preset fund       # also: --preset bench_spy, --preset bench_btc
uv run tradebot data refresh
uv run tradebot runner start                          # catch-up runs immediately
uv run tradebot runner tick --once                    # process due bars now and exit
uv run tradebot api serve                             # API on :8000, docs at /docs
uv run streamlit run dashboard/app.py                 # dashboard on :8501
uv run tradebot portfolio reset-kill-switch fund --note "reviewed"   # only way to reset
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

### Invariants / rules
- A (portfolio, bar) is processed at most once: guaranteed by `runner_state`, `run_log` UNIQUE, `ledger_event (portfolio_id, seq)` UNIQUE and `order.idempotency_key` UNIQUE, all in one transaction.
- `last_processed_bar_ts` only increases, and bar N+1 is never processed before bar N.
- Ledger is append-only. Ledger plus the latest `engine_checkpoint` is the source of truth; normalized tables and snapshots exist for the API to read.
- Money and quantity columns are TEXT Decimal strings; no integer minor units.
- Only the CLI `reset-kill-switch` resets the kill switch; the API ack never does.
- Runner/API/dashboard contain no code that places a real order; only `SimBroker` exists.
- Dashboard never imports trading modules and never computes metrics.
- The runner calls `BarDriver` and `PortfolioEngine`, the same code path as the backtester (imported, not copied); it contains no fill, mark or decide logic.
- All DB timestamps are UTC; bar timestamps identify the bar by its session date (UTC midnight for crypto, session date for equities).

## Acceptance criteria
1. Killing the runner mid-bar (fault hook `before_commit`) and restarting (restore from the latest checkpoint plus `verify()`) yields identical ledger, snapshots and checkpoint to an uninterrupted run; no duplicate fill.
2. Calling `tick` twice for the same time creates no new orders, fills or snapshots (idempotent).
3. With the machine "off" for 5 sessions, `startup_catchup` processes the 5 missed bars in order and the result equals a `run_backtest` of the same period through the same `BarDriver` (equity exactly equal, ADR Consequences).
4. An order created on bar t fills with `ledger_event.ts` equal to that symbol's next bar, at that bar's open price (never on t).
5. Equity-only portfolios are not processed on NYSE holidays/weekends; early-close days become due at 13:30 ET; crypto portfolios are processed daily including weekends; the mixed fund follows the union clock.
6. A gap greater than `MAX_CATCHUP_BARS` aborts with `last_status=error` and no state change.
7. If bar t is unavailable, `last_processed_bar_ts` is unchanged and the next tick retries.
8. Benchmark portfolios advance through the same path; their equity appears as `benchmark_equity` on the fund's curve.
9. Each API endpoint returns the specified schema (contract tests with TestClient on a seeded temp DB); POST `/strategies` rejects invalid schemas with 422.
10. The dashboard code imports nothing from `tradebot.sim/risk/portfolio/strategy/backtest` (enforced by a test scanning imports).
11. CI passes on a fresh clone with no network and no `.env`.
12. Settings load from env and `.env`; missing optional keys use defaults.
13. No money or quantity column is an integer or float; the schema has no `*_cents`, `qty_e8` or `price_e6`; a Decimal written to a TEXT column and read back is equal.
14. Each processed bar writes exactly one `engine_checkpoint`, one `run_log` row and one `runner_state` advance, atomically with its ledger events; a fault at any hook point leaves none of them.
15. POST `/portfolios/{id}/kill-switch/ack` leaves a tripped switch tripped; `tradebot portfolio reset-kill-switch` resets it, writes a `reset` event and a fresh checkpoint, and refuses while the runner lock is held.
16. `tradebot data refresh|verify|rebase`, `portfolio create|list|reset-kill-switch`, `runner start|tick|reset-from`, `backtest run` and `api serve` exist, and `tradebot --help` works with only the base dependencies.

## Tasks
Each tag is `[model]` and the **dispatch wave** (ADR-0001 §3). File lists are exclusive; no file appears in two tasks. `tests/conftest.py` and `src/tradebot/clock.py` (the injected `Clock`) are owned by quality-lead / SPEC-005: platform tasks import and use them, never edit them. Platform tests use their own `tests/platform/conftest.py` (T11) for DB fixtures.

### T1 [haiku] Wave 1: tooling, dependencies and CI (single self-contained task)
- **Files:** `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`
- **Depends on:** none
- **Do:**
  1. Replace `pyproject.toml` with exactly the contents under "Exact `pyproject.toml`" in the Tooling plan. This applies every runtime and dev dependency requested by specs 001-005 in one edit, using dependency groups; it also sets ruff config and pytest config (markers, `addopts`, `filterwarnings`, coverage run config). Do not add a Hypothesis profile or any `fail_under` (the profile lives in `tests/conftest.py`, owned by quality-lead).
  2. Create `.github/workflows/ci.yml` with exactly the contents under "Exact `.github/workflows/ci.yml`".
  3. Run `uv lock` and then `uv sync` to regenerate `uv.lock`.
  4. Run `uv run ruff check .`, `uv run ruff format --check .` and `uv run pytest`. If `ruff format --check` flags the existing trivial files (`tests/test_smoke.py`, `src/tradebot/**/__init__.py`), run `uv run ruff format` on exactly those files and touch nothing else.
- **Done when:** AC 11; `uv sync --locked` succeeds; the three CI commands pass locally; `uv run python -c "import pandas, pyarrow, yfinance, ccxt, exchange_calendars, yaml, pydantic, pydantic_settings, sqlmodel, fastapi, uvicorn, apscheduler, streamlit, plotly, httpx, hypothesis, pytest_cov, pytest_timeout"` succeeds and `apscheduler.__version__` starts with `3`.

### T2 [haiku] Wave 2: settings, logging and env example
- **Files:** `src/tradebot/config.py`, `src/tradebot/logging_config.py`, `.env.example`
- **Depends on:** T1
- **Do:** `Settings(BaseSettings)` with prefix `TRADEBOT_` and the keys in "Config" plus `TRADEBOT_CHECKPOINT_KEEP=30`; `configure_logging(settings)`; extend `.env.example` with every key and its default.
- **Done when:** AC 12.

### T3 [sonnet] Wave 5: DB models, engine and repositories
- **Files:** `src/tradebot/db/__init__.py`, `src/tradebot/db/engine.py`, `src/tradebot/db/models.py`, `src/tradebot/db/repo.py`
- **Depends on:** T2; 003 `EngineState`/ledger event JSON (ADR D7/D8)
- **Do:** SQLModel tables as specified (all money and quantities TEXT Decimal, plus `engine_checkpoint`, per-portfolio `runner_state`, `run_log` unique on portfolio and bar); WAL engine factory; `create_all`; repository functions for snapshots, runner_state, checkpoint write/read/prune, ledger append (with `seq`), idempotent upserts; `session_scope()` using `BEGIN IMMEDIATE`; Decimal <-> TEXT helpers. No Alembic in v1: schema version table plus `create_all`; breaking changes mean a rebuild from the ledger or a backtest rerun (document).
- **Done when:** AC 1, 2 (storage side), 13; constraints enforced in tests.

### T4 [sonnet] Wave 5: runner core (clocks, tick, restore, catch-up)
- **Files:** `src/tradebot/runner/clock.py`, `src/tradebot/runner/tick.py`, `src/tradebot/runner/state.py`, `src/tradebot/runner/bootstrap.py`
- **Depends on:** T3; `BarDriver` (002), `PortfolioEngine` (003), `MarketData` (001), `tradebot.clock.Clock` (quality-lead)
- **Do:** `due_bars(portfolio, now)` using `exchange_calendars` (XNYS) and UTC-day logic with the union clock; `tick(now, session, data, ...)` implementing steps 0-3 of "Processing a bar"; restore from latest checkpoint plus `verify()`; `fault_hook` seam; catch-up and `MAX_CATCHUP_BARS`; `bootstrap` creates portfolios and sleeves from presets for the CLI. Use the injected `Clock` for `now`; no direct `datetime.now()`. No fill, mark or decide logic.
- **Done when:** AC 1-8, 14.

### T5 [sonnet] Wave 5: scheduler entrypoint
- **Files:** `src/tradebot/runner/__main__.py`, `src/tradebot/runner/scheduler.py`, `src/tradebot/runner/lock.py`
- **Depends on:** T4
- **Do:** register the jobs in the table; `fcntl.flock` lock (the CLI reset command checks the same lock); heartbeat; graceful shutdown on SIGINT/SIGTERM; `__main__` only delegates to `tradebot.cli`.
- **Done when:** runner starts, catches up, and exits cleanly on SIGINT.

### T6 [haiku] Wave 5: API skeleton and schemas
- **Files:** `src/tradebot/api/app.py`, `src/tradebot/api/deps.py`, `src/tradebot/api/schemas.py`
- **Depends on:** T3
- **Do:** app factory, DB dependency, CORS off, pydantic models above (money fields pass the stored decimal strings through), `/health`. `src/tradebot/api/__init__.py` already exists and is untouched; the server is started by `tradebot api serve`.
- **Done when:** AC 9 (health + schema import).

### T7 [haiku] Wave 5: read endpoints
- **Files:** `src/tradebot/api/routers/__init__.py`, `src/tradebot/api/routers/portfolios.py`, `src/tradebot/api/routers/strategies.py`, `src/tradebot/api/routers/runner.py`
- **Depends on:** T6
- **Do:** GET endpoints from the table; paginate trades. Strategy cards read stored metrics produced by 002's `compute_card_metrics` (never computed in the router) and fall back to backtest metrics labelled `source="backtest"` until a paper portfolio has a year of history (ADR D12). No logic beyond mapping.
- **Done when:** AC 9, 8 (benchmark field).

### T8 [sonnet] Wave 5: backtest endpoints and job runner
- **Files:** `src/tradebot/api/routers/backtests.py`, `src/tradebot/api/jobs.py`
- **Depends on:** T6, 002 `run_backtest`
- **Do:** POST creates `backtest_run`, runs in a single-worker thread pool, persists result, denormalized columns, equity points and trades (via 002's `trades.py` outputs); failures set `status=failed`. On API startup mark stale `running` rows as `failed`.
- **Done when:** AC 9; killed API leaves no permanent `running` rows.

### T9 [haiku] Wave 5: kill-switch ack endpoint
- **Files:** `src/tradebot/api/routers/killswitch.py`
- **Depends on:** T7, 003 kill-switch semantics
- **Do:** POST ack writes `acknowledged_at`, `ack_note` and a `kill_switch_event(kind=ack)`; GET kill-switch is read-only. It must not call `reset_kill_switch` or change `tripped`.
- **Done when:** AC 15 (API half).

### T10 [haiku] Wave 5: dashboard
- **Files:** `dashboard/app.py`, `dashboard/api_client.py`, `dashboard/components.py`, `dashboard/pages/1_Portfolio.py`, `dashboard/pages/2_Strategies.py`, `dashboard/pages/3_Backtests.py`
- **Depends on:** T7, T8, T9
- **Do:** pages as described; Plotly charts; plain-language tooltips; banner; reset hint text for the kill switch.
- **Done when:** pages load against a seeded API; AC 10.

### T11 [haiku] Wave 5: platform tests (settings, DB, API, dashboard imports)
- **Files:** `tests/platform/conftest.py`, `tests/platform/test_settings.py`, `tests/platform/test_db_constraints.py`, `tests/platform/test_api_contract.py`, `tests/platform/test_dashboard_imports.py`
- **Depends on:** T3, T6-T10; `tests/conftest.py` (quality-lead, read-only for us)
- **Do:** cover AC 2, 9, 10, 12, 13, 15 (API half) using the synthetic fixture cache (ADR D9); no network; temp DB fixtures live in `tests/platform/conftest.py`.
- **Done when:** pass offline under `--strict-markers` and the 20 s timeout.

### T12 [sonnet] Wave 5: runner tests (restart, idempotency, catch-up, calendar)
- **Files:** `tests/platform/test_runner_idempotency.py`, `tests/platform/test_runner_catchup.py`, `tests/platform/test_clock.py`
- **Depends on:** T4, T5, T11
- **Do:** mark with `restart`; fault hook at each point; double tick; 5-day gap versus uninterrupted run and versus `run_backtest` equality; restore-from-checkpoint equality; holidays and early close (e.g. 2025-11-28, 2025-12-25); inject `Clock`.
- **Done when:** AC 1-8, 14. (Sonnet: state-machine tests.)

### T13 [sonnet] Wave 5: unified `tradebot` CLI
- **Files:** `src/tradebot/cli.py`, `tests/platform/test_cli.py`
- **Depends on:** T2, T3, T4; SPEC-001 `MarketData` functions; SPEC-002 `run_backtest`; 003 `reset_kill_switch`
- **Do:** argparse command tree from "Unified CLI"; lazy imports per subcommand; `main(argv=None) -> int`; `reset-kill-switch` transaction and lock check. `[project.scripts]` already exists from T1.
- **Done when:** AC 15 (CLI half), 16.

## New dependencies
All dependency edits are made by T1. This table is only the reasons; the exact set and pins are in "Tooling plan".
| Package | Reason |
|---|---|
| `sqlmodel` | schema + sessions on SQLite (PM-mandated) |
| `pydantic-settings` | typed `.env` config (PM-mandated) |
| `fastapi`, `uvicorn[standard]` | API service |
| `apscheduler` (<4) | runner scheduling (PM-mandated) |
| `exchange-calendars` | NYSE sessions, holidays, early closes, offline (also requested by 001) |
| `streamlit`, `plotly` | dashboard and interactive charts |
| `httpx` | dashboard API client; FastAPI TestClient dependency |
| `tzdata` | IANA timezone data for `zoneinfo` |
| `pandas`, `numpy`, `pyarrow`, `yfinance`, `ccxt`, `pyyaml`, `pydantic` | requested by 001/002, applied by T1 |
| dev: `pytest-cov`, `pytest-timeout`, `hypothesis` | requested by 005 (and 001/002 for hypothesis) |

## Open questions
- Exact name of the engine restore constructor (assumed `PortfolioEngine.from_state`) and of `BarDriver`'s per-bar method: 002/003 must expose them; T4 adapts to the final names.
- Strategy-card source: resolved by ADR D12 (backtest fallback, labelled until a paper portfolio has a year of history).
- Portfolio creation via API: resolved, CLI only (ADR D12).
- Peak equity and drawdown: resolved, owned by 003; the platform stores what the engine reports.

## Risks
- **Data timing:** yfinance may publish the daily bar late or revise it; mitigated by a 30-min close delay, retries and "data_unavailable" without advancing state. A revised bar after processing is NOT reprocessed (documented limitation).
- **SQLite concurrency:** runner writes while API/backtests read; mitigated with WAL and short transactions; long backtest result writes happen in a single batch.
- **Catch-up with adjusted history:** replayed fills use the provider's current adjusted opens, which may differ slightly from the real day's open; acceptable for simulation, flagged by `catchup` in `run_log.detail_json`. Forward chain-linking (ADR D2) is accepted.
- **Checkpoint growth:** one `state_json` per bar per portfolio; mitigated by pruning (`CHECKPOINT_KEEP`).
- **Calendar library drift:** holiday data changes between releases; pin below the next major and add a test on known dates.
- **APScheduler 4 rewrite** may change APIs; pinned `<4`.
- **Schema churn:** no migrations in v1, so early DB files are disposable and can be rebuilt from the ledger.
- **Dashboard drift into logic:** guarded by the import-scan test (AC 10) and review.
- **`filterwarnings = ["error"]`** may surface third-party warnings in Wave 1; handle with commented allowlist entries.
