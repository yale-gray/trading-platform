# SPEC-004: Platform: persistence, paper runner, API, dashboard, tooling

- **Lead:** platform-lead
- **Status:** approved with amendments (see ADR-0001 §4; ADR wins on conflict)
- **Roadmap item:** Phase 3 (SQLite + runner), Phase 4 (API + dashboard)
- **Related ADRs/specs:** SPEC-001 (data), SPEC-002 (strategy/backtest), SPEC-003 (execution/sim/portfolio/risk), SPEC-005 (quality). To be reconciled in ADR-0001.

## Goal
Provide everything that runs, stores and shows the bot: a SQLite schema, a restart-safe daily-bar paper runner, a read-mostly FastAPI service and a Streamlit dashboard, plus uv/ruff/pytest/CI tooling. The platform only *calls* trading logic from 001/002/003; it contains none.

## Non-goals
- Trading logic (signals, sizing, fills, risk, metrics math). Owned by 002/003.
- Auth, multi-user, hosting, HTTPS, Docker. v1 is local, single user, single process.
- Intraday bars, websockets/live streaming, real broker code (nothing here can place a real trade).
- Alerts/notifications and the AI strategy builder (Phase 5).

## Design

### Process model
One Python process per component, all local:
- **Runner process** (`python -m tradebot.runner`): APScheduler `BlockingScheduler` plus the processing loop. The only writer of execution state (portfolios, orders, fills, snapshots, runner state).
- **API process** (`uvicorn tradebot.api.app:app`): reads the DB; writes only strategy configs, portfolio creation and backtest-run requests (see below).
- **Dashboard** (`streamlit run dashboard/app.py`): HTTP client of the API only. It imports no `tradebot.sim/risk/portfolio/strategy` code.
SQLite runs in WAL mode (`PRAGMA journal_mode=WAL; busy_timeout=5000; foreign_keys=ON`) so the API can read while the runner writes. Backtests triggered by the API run in a `ThreadPoolExecutor` inside the API process (v1; max 1 concurrent), status tracked in `backtest_run`. Multi-user/hosting would need Postgres, a job queue, and auth; none of the code below assumes SQLite-specific SQL beyond PRAGMAs in `db/engine.py`.

### Config (pydantic-settings)
`Settings` reads `.env` with prefix `TRADEBOT_`. Existing keys: `TRADEBOT_DB_PATH`, `TRADEBOT_CACHE_DIR`. Add (and the lead updates `.env.example`): `TRADEBOT_API_HOST=127.0.0.1`, `TRADEBOT_API_PORT=8000`, `TRADEBOT_API_URL=http://127.0.0.1:8000` (used by dashboard), `TRADEBOT_LOG_LEVEL=INFO`, `TRADEBOT_LOG_DIR=./logs`, `TRADEBOT_MAX_CATCHUP_BARS=400`, `TRADEBOT_EQUITY_CLOSE_DELAY_MIN=30`. No secrets exist in v1 (public data only).

### Money in the DB
Money columns are stored as **integer minor units** (`*_cents` for USD; crypto quantities as `*_sat` style integers = quantity x 1e8 units, named `qty_e8`) to honour the Decimal/integer rule and avoid SQLite float drift. Exact representation of ledger amounts depends on 003; this spec assumes integer cents for cash/equity/fees and integer `qty_e8` for quantities. Prices are stored as integer `price_e6` (micro-USD) so sub-cent crypto/ETF prices survive. Marked **[003]** wherever the column set depends on 003's types.

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
| initial_cash_cents | int | 10_000_000 for the fund |
| base_ccy | str | `USD` |
| status | str | `active/halted/archived` |
| started_at | datetime | portfolio inception bar |
| peak_equity_cents | int | **[003]** if the risk module owns peak tracking, drop and read from kill_switch |
| config_json | JSON | sleeve weights, risk limits, rebalance rule **[003]** |

**sleeve** (a strategy allocation inside a portfolio)
| id | int PK | |
| portfolio_id | FK | |
| key | str | `core`, `meanrev`, `crypto_trend`, `cash` ; unique with portfolio_id |
| strategy_config_id | FK null | null for the cash sleeve |
| target_weight_bp | int | 5000/2000/2000/1000 |
| market | str | `equity` / `crypto` (selects the calendar and bar clock) |
| cash_cents | int | sleeve cash **[003]** (may instead live in ledger only) |
| enabled | bool | |

**order** **[003: field names and state machine]**
| id | int PK | |
| portfolio_id, sleeve_id | FK | |
| symbol | str | |
| side | str | `buy/sell` |
| order_type | str | `market` only in v1 |
| qty_e8 | int | |
| status | str | `pending/filled/cancelled/rejected` |
| signal_bar_ts | datetime | bar t that produced the signal |
| fill_due_bar_ts | datetime | bar t+1 (when it may fill) |
| reason | str | strategy tag / `rebalance` / `stop` / `kill_switch` |
| risk_note | text null | veto/shrink explanation from risk |
| idempotency_key | str UNIQUE | `f"{portfolio_id}:{sleeve_id}:{symbol}:{signal_bar_ts}:{side}:{reason}"` |

**ledger_event** (append-only fills and cash movements; the source of truth) **[003: event types]**
| id | int PK | |
| portfolio_id, sleeve_id | FK | |
| ts | datetime | bar ts of the event |
| type | str | `fill/fee/deposit/rebalance_transfer/adjustment` |
| order_id | FK null | |
| symbol | str null | |
| qty_e8 | int | signed |
| price_e6 | int null | |
| fee_cents, slippage_cents | int | |
| cash_delta_cents | int | signed |
| seq | int | monotonic per portfolio; unique (portfolio_id, seq) |

**position_snapshot** (portfolio_id, sleeve_id, ts, symbol, qty_e8, avg_cost_e6, mark_px_e6, market_value_cents, highest_close_e6) composite PK; written each processed bar. `highest_close_e6` supports ATR trailing stops **[003]**. Derivable from the ledger, stored for fast dashboard reads.

**equity_snapshot** (portfolio_id, sleeve_id nullable, ts, equity_cents, cash_cents, drawdown_bp, benchmark_equity_cents null) PK (portfolio_id, sleeve_id, ts). `sleeve_id` NULL means total portfolio.

**kill_switch_state** (one row per portfolio) **[003]**
| portfolio_id | PK/FK | |
| tripped | bool | |
| tripped_at_bar | datetime null | |
| trigger_drawdown_bp | int null | measured drawdown at trip |
| peak_equity_cents | int | peak at trip |
| acknowledged_at | datetime null | human review flag (STRATEGY.md section 5) |
| ack_note | text null | |
Plus **kill_switch_event** (id, portfolio_id, ts, kind `trip/ack/reset`, note) as audit log.

**runner_state** (idempotency anchor)
| portfolio_id, sleeve_id | composite PK | one row per sleeve; benchmark portfolios have a single `core`-like sleeve |
| market | str | |
| last_processed_bar_ts | datetime | the last completed bar fully committed |
| last_run_at | datetime | |
| last_status | str | `ok/error/data_unavailable` |
| last_error | text null | |
| consecutive_failures | int | |

**run_log** (portfolio_id, sleeve_id, bar_ts, started_at, finished_at, outcome, n_orders_created, n_fills, detail_json) with UNIQUE (portfolio_id, sleeve_id, bar_ts); defence in depth against double-processing.

### Runner design

**Bar clocks**
- Equity sleeves: bar date D is *complete* once `now >= close(D) + EQUITY_CLOSE_DELAY` (default 30 min) where `close(D)` comes from the XNYS calendar (handles early 13:00 ET closes on e.g. Black Friday). Only sessions listed by the calendar count as bars.
- Crypto sleeves: bar date D (UTC day) is complete once `now >= (D+1 day) 00:00 UTC + 10 min` (let the exchange finalize the candle).

**Exchange calendar library: `exchange_calendars`** (XNYS). It is the maintained successor of Quantopian's `trading_calendars`, gives sessions, `session_close`, early closes and holidays, and is the engine underneath `pandas_market_calendars`, which only adds a wrapper layer we don't need. Pure-Python wheels, offline, deterministic (holiday data ships with the package, so tests need no network). Pin a minor version since holiday tables change per release. `pandas_market_calendars` is the fallback if the API proves awkward.

**Processing a bar (one transaction per (portfolio, sleeve, bar))**
For sleeve S and the next unprocessed completed bar t (the first bar after `runner_state.last_processed_bar_ts`):
1. Load bars through t from the 001 `DataProvider` (cache-first; fetch if missing). If bar t is unavailable, set `last_status=data_unavailable`, leave `last_processed_bar_ts` unchanged, retry on the next tick.
2. **Fill step:** pending orders with `fill_due_bar_ts == t` are filled by the 003 `SimBroker` at bar t's open plus slippage and fees. Append `ledger_event`s.
3. **Mark step:** mark positions at close(t); write `position_snapshot`, `equity_snapshot` (sleeve and portfolio totals). Evaluate stops/kill-switch via 003 risk code on close(t).
4. **Decide step:** call the *same* 002 evaluation function the backtester uses (`evaluate(strategy, bars_through_t, state) -> TargetPositions/Signals`) then 003 sizing + risk, producing new `order`s with `status=pending`, `signal_bar_ts=t`, `fill_due_bar_ts=next bar`. If `kill_switch.tripped`, only exit/reduce orders are allowed (003 enforces).
5. Update `runner_state.last_processed_bar_ts = t`, insert `run_log` row. **COMMIT.**
All five steps are in one SQLite transaction (`BEGIN IMMEDIATE`). A crash before COMMIT leaves nothing behind; a crash after COMMIT means the next start sees `last_processed_bar_ts = t` and moves on. The `idempotency_key` and `run_log` unique constraints turn an accidental double-run into an IntegrityError that is caught and logged (skipped), never a second trade. The order of steps (fill, mark, then decide) guarantees a signal on bar t fills at bar t+1's open (CLAUDE.md rule 2).

**Portfolio ordering within a tick:** process bars across portfolios in ascending bar timestamp, then sleeves; the monthly rebalance is a portfolio-level step run on the first processed bar of a new month, after all sleeve steps for that bar **[003: rebalance API]**.

**Benchmark portfolios** `bench_spy` and `bench_btc` are ordinary `portfolio` rows (`kind=benchmark`) with one sleeve whose strategy is a "buy at first bar, hold" config from 002. They go through the identical code path. Each fund `equity_snapshot` stores the matching benchmark equity so the dashboard can chart relative performance without calculating anything.

**Catch-up decision: replay missed bars in order.** If the machine was off for N days, the runner processes every missed bar sequentially using historical bars (fill at that bar's real open), not skip. Reasons: (1) Skipping would leave pending orders orphaned and strategy state (SMA windows, 5-day exits, trailing-stop highs, monthly rebalance) inconsistent, so the paper fund would diverge from what the backtester would have produced for the same period, which defeats the "same code as backtest" guarantee. (2) The equity curve and drawdown stay continuous, so the 3-month forward test and benchmark comparison are valid. (3) It is lookahead-safe because each step only sees data up to its own bar. (4) Daily bars make replay cheap. Safeguards: replayed orders are tagged `reason` suffix `:catchup` in `run_log.detail_json` for audit; if the gap exceeds `MAX_CATCHUP_BARS` (400) the runner refuses, sets `last_status=error`, and requires `tradebot-runner --reset-from DATE` (human decision). A portfolio started for the first time begins at "the latest completed bar" (no backfill) unless `--start DATE` is given.

**Scheduler jobs (APScheduler 3.x, `SQLAlchemyJobStore` not used; jobs are re-registered at startup, state lives in `runner_state`)**
| job id | trigger | does |
|---|---|---|
| `startup_catchup` | once at start | `tick(now)` which replays any missed bars |
| `equity_close` | cron Mon-Fri 16:35 `America/New_York`, `misfire_grace_time=6h`, `coalesce=True` | `tick(now)` filtered to equity sleeves |
| `equity_retry` | interval 15 min, only active 16:35-23:59 ET Mon-Fri (guard inside the job) | retry if the daily bar was not yet available from yfinance |
| `crypto_close` | cron daily 00:10 `UTC`, `misfire_grace_time=6h`, `coalesce=True` | `tick(now)` filtered to crypto sleeves |
| `crypto_retry` | interval 15 min | same retry for crypto |
| `heartbeat` | interval 5 min | write `runner_heartbeat` (single row table: `ts`, `pid`, `version`) for the dashboard status badge |
Every job body is simply `tick(now)`; `tick` computes "due bars" from calendar + `runner_state`, so the cron timings are only nudges and *correctness never depends on a job firing on time*. A process-level lock (`max_instances=1`, plus a `runner.lock` file with `fcntl.flock`) prevents two runners. Holidays: on non-sessions `tick` finds no due bar and no-ops. DST is handled by cron in the `America/New_York` tz.

### API (FastAPI, `/api/v1`, JSON, no auth, bound to 127.0.0.1)
Responses use pydantic models in `api/schemas.py`. Money is returned as `{"cents": int}`-free plain `Decimal`-serialized strings in USD (`"100000.00"`) so the UI never touches integers/floats of money; metrics are floats. Time series are `list[Point]` for easy plotting.

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

class KillSwitchOut(BaseModel):
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
| POST `/portfolios/{id}/kill-switch/ack` | `{note: str}` | `KillSwitchOut` | records human review only; the runner owns reset rules **[003]** |
| GET `/strategies` | | `list[StrategyConfigOut]` | |
| POST `/strategies` | `StrategyConfigIn` | `StrategyConfigOut` | validates with 002's pydantic schema; new version on name clash |
| GET `/strategies/{id}` | | `StrategyConfigOut` | |
| GET `/strategies/cards` | | `list[StrategyCard]` | 1-year return, max DD, Sortino; paper if >=1y of snapshots else latest backtest, flagged by `source` |
| POST `/backtests` | `BacktestRunIn` | `BacktestRunSummary` (202) | queues thread job |
| GET `/backtests?strategy_id=` | | `list[BacktestRunSummary]` | |
| GET `/backtests/{id}` | | `BacktestRunDetail` | |
Portfolio creation is by CLI/seed script (`python -m tradebot.runner init`) in v1, not the API, to keep POST surface small and avoid the API creating trading state. Rule: the API never calls broker/sim code and exposes no order-placing endpoint.

### Dashboard (Streamlit, `dashboard/`)
Multipage app, all data via an `ApiClient` (httpx) with `st.cache_data(ttl=60)`:
1. **Portfolios** (`app.py`): table from `/portfolios`, runner heartbeat badge, kill-switch warning chips.
2. **Portfolio detail** (`pages/1_Portfolio.py`): equity curve vs SPY/BTC benchmarks (Plotly, rebased to 100), drawdown area chart, positions table, trade log (paged), sleeve breakdown (stacked bar of target vs actual weights), kill-switch panel with ack button. Sidebar portfolio select.
3. **Strategies** (`pages/2_Strategies.py`): NexusTrade-style cards (`StrategyCard`): 1-year return, max drawdown, Sortino, with the benchmark return beside it and a "paper/backtest" label. Plain-language tooltips for each term (owner is new to investing).
4. **Backtests** (`pages/3_Backtests.py`): list of runs, run form, detail view with metrics table, equity vs benchmark, in-sample/out-of-sample split and walk-forward table.
Every page shows the "simulated money, not financial advice" banner. No page computes returns, drawdown, or sizes; it only formats API values (percent formatting, rebasing a series to 100 is presentational and permitted).

### Logging
`logging` + `dictConfig` in `tradebot/logging_config.py`: console plus rotating file `logs/runner.log` (5 x 5 MB), JSON-ish key=value lines with `portfolio`, `sleeve`, `bar_ts` fields on every runner log record.

### Interfaces needed from other specs (proposed; PM to reconcile)
```python
# 001 data
class DataProvider(Protocol):
    def get_bars(self, symbol: str, start: datetime, end: datetime, interval: Literal["1d"]="1d") -> pd.DataFrame: ...
    def last_complete_bar(self, symbol: str, market: Literal["equity","crypto"], now: datetime) -> datetime: ...
    def manifest(self, symbols: list[str]) -> str: ...      # data_fingerprint for backtest_run
# Offline fixture provider for tests: FixtureDataProvider(dir)

# 002 strategy/backtest
class StrategySpec(BaseModel): ...                     # JSON-serializable, has .market and .universe
def evaluate(spec: StrategySpec, bars: dict[str, pd.DataFrame], state: StrategyState) -> list[Signal]  # same fn backtester calls
def run_backtest(spec: StrategySpec, provider: DataProvider, params: BacktestParams) -> BacktestResult
# BacktestResult: metrics, benchmark metrics, equity series, trades, oos split; .model_dump() is JSON-safe
def compute_card_metrics(equity: pd.Series, benchmark: pd.Series) -> CardMetrics   # 1y return, maxDD, Sortino (platform must NOT reimplement)

# 003 execution/portfolio/risk
class PortfolioEngine:   # wraps SimBroker + ledger + sizing + risk
    def step(self, ctx: BarContext) -> StepResult   # fill pending -> mark -> decide, pure w.r.t. DB; returns new orders, ledger events, snapshots, kill-switch change
    def rebalance_due(self, ts: datetime) -> bool
# Persistable state: Portfolio/Sleeve/Order/LedgerEvent/Position/KillSwitch as pydantic models with .from_rows()/.to_rows() mapping
# Needs: integer-money representation decided (cents / qty_e8 / price_e6), order state machine and idempotency key, peak-equity and drawdown ownership, whether sleeve cash is stored or derived from the ledger.
```
The runner owns the DB read/write; 003's engine is a pure function over state objects so it can be unit tested without SQLite and be shared with the backtester.

### Tooling plan
**Dependency groups (pyproject.toml, uv)**
- `dependencies` (runtime, shared): `pydantic>=2`, `pydantic-settings>=2`, `sqlmodel>=0.0.22`, `pandas>=2.2`, `exchange-calendars>=4.5`, plus whatever 001/002/003 add.
- `[project.optional-dependencies]`/groups: `api = [fastapi, uvicorn[standard]]`, `runner = [apscheduler>=3.10,<4]`, `dashboard = [streamlit, plotly, httpx]`.
- `dev = [pytest>=8, pytest-cov, httpx, ruff>=0.6, freezegun or time-machine, mypy optional]`. Default `uv sync` installs everything via `default-groups = ["dev", "api", "runner", "dashboard"]` under `[tool.uv]`.
- Ruff config stays as is (line-length 100, `E,F,I,UP,B`); add `"S"`-free (no bandit) and `per-file-ignores = {"tests/*" = ["B011"]}`; add `[tool.ruff.lint.isort] known-first-party = ["tradebot"]`.
- Pytest: add `markers = ["network: hits real data providers (excluded in CI)", "slow"]`, `addopts = "-m 'not network' -ra"`.
- Scripts: `[project.scripts]` `tradebot-runner = "tradebot.runner.__main__:main"`, `tradebot-api = "tradebot.api.__main__:main"`.

**Dependency note on APScheduler:** pin `<4`; 4.x is a rewrite with a different API and was pre-release at last check; 3.10 is stable. Verify before pinning.

**CI: `.github/workflows/ci.yml`** (repo github.com/yale-gray/trading-platform)
```
on: [push, pull_request]
jobs: lint-test:
  runs-on ubuntu-latest, python 3.12
  steps: actions/checkout; astral-sh/setup-uv (enable-cache); uv sync --frozen;
         uv run ruff check .; uv run ruff format --check .; uv run pytest -m "not network" --cov=tradebot
```
No secrets, no network tests, no scheduled runs in CI. Commit `uv.lock`; `.github/dependabot.yml` for weekly uv updates (optional).

**Run locally**
```bash
uv sync
cp .env.example .env
uv run python -m tradebot.runner init            # create DB, seed paper_fund + bench_spy + bench_btc, strategy configs
uv run python -m tradebot.runner                 # start scheduler (catch-up runs immediately)
uv run python -m tradebot.runner tick --once     # process due bars now and exit (for cron/manual)
uv run uvicorn tradebot.api.app:app --reload     # API on :8000, docs at /docs
uv run streamlit run dashboard/app.py            # dashboard on :8501
uv run pytest && uv run ruff check .
```

### Invariants / rules
- A (portfolio, sleeve, bar) is processed at most once: guaranteed by `runner_state`, `run_log` UNIQUE and `order.idempotency_key` UNIQUE, all in one transaction.
- `last_processed_bar_ts` only increases, and bar N+1 is never processed before bar N.
- Ledger is append-only; snapshots are derived and may be rebuilt from the ledger.
- Runner/API/dashboard contain no code that places a real order; only `SimBroker` exists.
- Dashboard never imports trading modules and never computes metrics.
- Same evaluation, SimBroker, risk functions as the backtester (imported, not copied).
- All DB timestamps are UTC; bar timestamps identify the bar by its session date (UTC midnight for crypto, session date for equities).

## Acceptance criteria
1. Killing the runner mid-bar (after fills, before commit) and restarting yields identical ledger/snapshots to an uninterrupted run; no duplicate fill.
2. Calling `tick` twice for the same time creates no new orders, fills or snapshots (idempotent).
3. With the machine "off" for 5 sessions, `startup_catchup` processes the 5 missed bars in order and results equal a backtest of the same period run through the same engine (equity within rounding).
4. A signal on bar t produces a fill with `ledger_event.ts == t+1` at t+1's open price.
5. Equity sleeves are not processed on NYSE holidays/weekends; early-close days become due at 13:30 ET; crypto sleeves are processed daily including weekends.
6. A gap greater than `MAX_CATCHUP_BARS` aborts with `last_status=error` and no state change.
7. If bar t is unavailable, `last_processed_bar_ts` is unchanged and the next tick retries.
8. Benchmark portfolios advance through the same path; their equity appears as `benchmark_equity` on the fund's curve.
9. Each API endpoint returns the specified schema (contract tests with TestClient on a seeded temp DB); POST `/strategies` rejects invalid schemas with 422.
10. The dashboard code imports nothing from `tradebot.sim/risk/portfolio/strategy/backtest` (enforced by a test scanning imports).
11. CI passes on a fresh clone with no network and no `.env`.
12. Settings load from env and `.env`; missing optional keys use defaults.

## Tasks

### T1 [haiku] Project tooling and CI
- **Files:** `pyproject.toml`, `.github/workflows/ci.yml`, `.env.example`
- **Depends on:** none
- **Do:** add dependency groups, uv default-groups, ruff isort config, pytest markers, scripts, CI workflow as specified above.
- **Done when:** AC 11; `uv sync` and `uv run ruff check .` pass.

### T2 [haiku] Settings and logging
- **Files:** `src/tradebot/config.py`, `src/tradebot/logging_config.py`
- **Depends on:** T1
- **Do:** `Settings(BaseSettings)` with the keys above; `configure_logging(settings)`.
- **Done when:** AC 12.

### T3 [sonnet] DB models, engine and repositories
- **Files:** `src/tradebot/db/__init__.py`, `engine.py`, `models.py`, `repo.py`
- **Depends on:** T2; 003 money representation settled in ADR-0001
- **Do:** SQLModel tables as specified, WAL engine factory, `create_all`, repository functions for snapshots, runner_state, ledger append (with `seq`), idempotent upserts, `session_scope()` using `BEGIN IMMEDIATE`. No Alembic in v1: schema version table plus `create_all`; breaking changes mean a rebuild from the ledger/backtest rerun (document).
- **Done when:** AC 1, 2 (storage side); constraints enforced in tests.

### T4 [sonnet] Runner core: clocks, tick, catch-up
- **Files:** `src/tradebot/runner/clock.py`, `tick.py`, `state.py`, `bootstrap.py`
- **Depends on:** T3, interfaces from 001/002/003
- **Do:** `due_bars(sleeve, now)` using `exchange_calendars` (XNYS) and UTC-day logic; `tick(now, session, provider, engine)` implementing the five-step transaction; catch-up and `MAX_CATCHUP_BARS`; `bootstrap` seeds fund + benchmarks; inject `now` as a parameter everywhere (no direct `datetime.now()`).
- **Done when:** AC 1-8.

### T5 [sonnet] Scheduler entrypoint
- **Files:** `src/tradebot/runner/__main__.py`, `scheduler.py`, `lock.py`
- **Depends on:** T4
- **Do:** register the jobs in the table, flock lock, heartbeat, signal handling (graceful shutdown), CLI `init`, `tick --once`, `--reset-from`, `--start`.
- **Done when:** runner starts, catches up, and exits cleanly on SIGINT.

### T6 [haiku] API skeleton and schemas
- **Files:** `src/tradebot/api/__init__.py`, `app.py`, `deps.py`, `schemas.py`, `__main__.py`
- **Depends on:** T3
- **Do:** app factory, DB dependency, CORS off, pydantic models above, `/health`.
- **Done when:** AC 9 (health + schema import).

### T7 [haiku] Read endpoints
- **Files:** `src/tradebot/api/routers/portfolios.py`, `strategies.py`, `runner.py`
- **Depends on:** T6
- **Do:** GET endpoints from the table; convert integer cents to `Money`; paginate trades. Strategy cards read stored metrics produced by 002's `compute_card_metrics` (computed by the runner at snapshot time or on card request through 002, never in the router). No logic beyond mapping.
- **Done when:** AC 9, 8 (benchmark field).

### T8 [sonnet] Backtest endpoints and job runner
- **Files:** `src/tradebot/api/routers/backtests.py`, `src/tradebot/api/jobs.py`
- **Depends on:** T6, 002 `run_backtest`
- **Do:** POST creates `backtest_run`, runs in a single-worker thread pool, persists result, denormalized columns, equity points and trades; failures set `status=failed`. On API startup mark stale `running` rows as `failed`.
- **Done when:** AC 9; killed API leaves no permanent `running` rows.

### T9 [haiku] Kill-switch ack endpoint
- **Files:** `src/tradebot/api/routers/portfolios.py`
- **Depends on:** T7, 003 kill-switch semantics
- **Do:** POST ack writes `acknowledged_at` and a `kill_switch_event`; no state reset.
- **Done when:** endpoint contract test.

### T10 [haiku] Dashboard
- **Files:** `dashboard/app.py`, `dashboard/api_client.py`, `dashboard/pages/1_Portfolio.py`, `2_Strategies.py`, `3_Backtests.py`, `dashboard/components.py`
- **Depends on:** T7, T8
- **Do:** pages as described; Plotly charts; plain-language tooltips; banner.
- **Done when:** pages load against a seeded API; AC 10.

### T11 [haiku] Tests
- **Files:** `tests/platform/test_settings.py`, `test_db_constraints.py`, `test_api_contract.py`, `test_dashboard_imports.py`, `tests/conftest.py` (temp DB + seeded fixtures)
- **Depends on:** T3, T6-T10
- **Do:** cover AC 2, 9, 10, 12 using the fixture data provider; no network.
- **Done when:** pass offline.

### T12 [sonnet] Runner tests (restart, idempotency, catch-up, calendar)
- **Files:** `tests/platform/test_runner_idempotency.py`, `test_runner_catchup.py`, `test_clock.py`
- **Depends on:** T4, T5
- **Do:** simulate crash by raising inside the transaction; double tick; 5-day gap versus uninterrupted run equality; holidays/early close fixtures (e.g. 2025-11-28, 2025-12-25); inject `now`.
- **Done when:** AC 1-8. (Sonnet because these are state-machine tests per the routing rule.)

## New runtime dependencies (with reason)
| Package | Reason |
|---|---|
| `sqlmodel` | schema + sessions on SQLite (PM-mandated) |
| `pydantic-settings` | typed `.env` config (PM-mandated) |
| `fastapi`, `uvicorn[standard]` | API service |
| `apscheduler` (<4) | runner scheduling (PM-mandated) |
| `exchange-calendars` | NYSE sessions, holidays, early closes, offline |
| `streamlit`, `plotly` | dashboard and interactive charts |
| `httpx` | dashboard API client; also FastAPI TestClient dependency |
| `tzdata` | IANA timezone data on all platforms for `zoneinfo` |
Dev only: `pytest-cov`, `time-machine` (or `freezegun`).

## Open questions
- **Integer money representation** (cents / `qty_e8` / `price_e6`): needs a single ADR decision with 003; the schema assumes these.
- Who owns peak equity and drawdown series computation: 003 risk (kill switch) vs 002 metrics? Proposal: 003 computes at snapshot time and the runner stores it.
- Strategy-card source: use paper-fund history only after >= 1 year (rare in v1) or fall back to the latest backtest? Proposed: backtest fallback, labelled.
- Should the API be allowed to create portfolios in v1? Proposed no (CLI only).
- Should `PortfolioEngine.step` be pure (state in, state out) so that backtester and runner share it exactly? Strongly proposed, needs 003 buy-in.
- Does 001 provide `last_complete_bar`, or does the platform derive it from the calendar? Proposed: platform computes bar due-ness; 001 only returns bars.
- yfinance daily bar for "today" can be partial or revised; confirm 001 only returns completed bars and handles adjusted vs raw prices (splits/dividends affect fills vs snapshots).

## Risks
- **Data timing:** yfinance may publish the daily bar late or revise it; mitigated by a 30-min close delay, retries and "data_unavailable" without advancing state. A revised bar after processing is NOT reprocessed (documented limitation).
- **SQLite concurrency:** runner writes while API/backtests read; mitigated with WAL and short transactions; long backtest result writes happen in a single batch.
- **Catch-up with adjusted history:** replayed fills use the provider's current adjusted opens, which may differ slightly from the real day's open; acceptable for simulation, flagged by `:catchup`.
- **Calendar library drift:** holiday data changes between releases; pin and add a test on known dates.
- **APScheduler 4 rewrite** may change APIs; pinned `<4`.
- **Schema churn** before ADR-0001: ledger/order columns marked [003] will change; no migrations in v1, so early DB files are disposable.
- **Dashboard drift into logic:** guarded by the import-scan test (AC 10) and review.
