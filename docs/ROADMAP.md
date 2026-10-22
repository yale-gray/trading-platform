# Roadmap

Status key: `[ ]` todo · `[~]` in progress · `[x]` done. The PM owns this file.

## Phase 0: Scaffold
- [x] git init, `CLAUDE.md` (PM manual), 9 agent files in `.claude/agents/`
- [x] `docs/STRATEGY.md`, spec/ADR templates, process log
- [x] `uv` project with pytest and ruff, package skeleton `src/tradebot/`

## Phase 0.5: Lead research round (option B)
- [x] Each of the 5 leads writes a design spec for its area (`docs/specs/001`–`005`)
- [x] PM reconciles them into [ADR-0001](adr/ADR-0001-architecture-and-module-contracts.md)
- [x] Leads amend specs per ADR-0001 §4

## Wave 1: Foundations (done 2026-10-20)
- [x] Deps, lock, CI (004 T1) · Clock, test guards, synthetic fixtures + manifest test (005 T1–T4)
- [x] Data types + trading calendar (001 T1, T3) · Money module + tests (003 T1, T11a) · Indicators + tests (002 T2, T7a)
- [x] Reviewer: 9/9 approved · Quality-lead audits: money + fixtures PASS-WITH-NOTES, fixes applied

## Wave 2: Data + schema (done 2026-10-20)
- [x] 001 T2–T13: symbols, quality, crash-safe parquet cache, restatement, ccxt + yfinance + fixture providers, universe, `MarketData` facade
- [x] 002 T1 strategy schema + 3 sleeve YAMLs (+ T7b schema tests, pulled forward)
- [x] 003 T2–T5 config, orders, costs, ledger/portfolio (+ T11b tests, ledger tests from audit)
- [x] 004 T2 settings/logging · 005 T5–T10 hand-calc, lookahead, data-quality tests, coverage checker, audit template
- [x] Live smoke checks: Coinbase daily buckets at 00:00 UTC, SOL/USD from 2021-06-17; Yahoo SPY adjusted bars OK
- [x] Reviewer 8/8 approve · audits: execution + data PASS-WITH-NOTES, majors fixed, re-audited

## Mini-wave 2.5: Local dev console (done 2026-10-21)
- [x] [SPEC-006](specs/006-dev-console.md): Streamlit app at `devconsole/` (Overview, Market data, Strategies, Cost sandbox, Quality & reviews). Read-only, synthetic data by default. Run: `uv run streamlit run devconsole/app.py`
- Grows later: trades after Wave 3, backtests after Wave 4, merges into `dashboard/` in Wave 5

## Wave 3: Engine (done 2026-10-21)
- [x] 003 T6–T10: sizing + risk checks, stops + kill switch, SimBroker (+ book API), rebalancer, `PortfolioEngine` + `EngineState` · 002 T3 strategy evaluator
- [x] Tests: 003 T11c/T13, 005 T11/T12, evaluator semantics, kill switch, rebalance, fix-pass regressions
- [x] Carried-over Wave 2 items resolved (ledger grids/links/atomic transfers, config cent split, data error move)
- [x] Reviewer (1 blocker fixed: weight-sum rounding) · quality-lead audit PASS-WITH-NOTES, majors F1/F2 fixed

## Wave 4: Backtester (done 2026-10-21, lean mode)
- [x] 002: `BarDriver` + `TargetBook`, trades/equity/exposure from the ledger, metrics, `run_backtest`/`run_with_benchmark` (SPY, BTC, monthly-rebalanced blend), warmup enforced
- [x] 002: validation: 70/30 OOS (read once), walk-forward inside IS, ±20% sensitivity, `evaluate_gate`; memorizer rejected, positive control passes
- [x] Engine property tests with an independent oracle (003 T12, 005 T13–T14); goldens G1–G4 signed off by quality-lead; backtest prefix invariance
- [x] Dev console Backtests page · CI split (fast PR job ~60 s; slow/golden on main + nightly)
- [x] Audit PASS-WITH-NOTES; fixes applied (window pins, gate boundaries, production walk-forward defaults)
- Result on synthetic fixtures: all three sleeves **fail** the gate (hold). Expected on practice data; real-data runs come with Wave 5

## Wave 5: Paper trading + dashboard (done 2026-10-21, lean mode)
- [x] SQLite persistence (WAL, TEXT Decimal), restart-safe runner (one transaction per portfolio per bar, checkpoint incl. TargetBook, catch-up, stale-runner refusal), scheduler
- [x] FastAPI (3 write routes only: kill-switch ack, save strategy, queue backtest), unified `tradebot` CLI incl. `runner reset-from`
- [x] Dashboard (`dashboard/app.py`, port 8502) via the API; dev console pages merged in as a "Developer" section
- [x] Audit PASS-WITH-NOTES: runner ledger == backtest ledger; crash/restart identical; fixes applied
- [ ] Later: checkpoint without the full ledger (DB ≈13 MB/yr for 3 portfolios); dashboard banner for `benchmark_missing`; confirm restated-bar handling stays in the data layer

## Real-data check (2026-10-21)
- [x] Real prices downloaded: 17 ETFs (from 2005), BTC/SOL, ETH (crypto history starts 2017 to avoid a Coinbase ETH gap in 2016)
- [x] First real-data backtests, with fair settings (fund caps and kill switch relaxed) vs buy-and-hold:
  - crypto trend: 22.9%/yr, worst drop −47%, Sharpe 0.92 vs BTC 22.9%/yr, −81%, 0.65 (**promising**)
  - core momentum: 2.4%/yr, Sharpe 0.25 vs SPY 14.1%/yr, 0.86 (**loses**: inverse-vol sizing tilts it to bonds/gold)
  - mean reversion: 1.7%/yr, Sharpe 0.31 vs SPY (**loses**: mostly in cash)

## Wave 6: two-account product (next, lean mode) — ADR A12–A16
- [ ] **Owner action:** create a free Alpaca account (market data only) and add the key to `.env`
- [x] Backtest setup fix (A16): per-strategy caps, `killed_on`/`killed_dates`, kill switch on/off in CLI and API
- [x] Index rework: 4 pre-registered candidates all failed the gate → index section = SPY/QQQ buy-and-hold (A17; `docs/research/index-strategies-2026-10.md`)
- [x] S&P 500 stocks: 501 symbols cached, class shares (BRK.B), `stocks_momentum` 22.1%/yr vs SPY 10.9% but **survivorship-biased** and fails the gate on drawdown (`docs/research/sp500-stocks-2026-10.md`)
- [x] Kill switch pause-and-re-arm (A18); smart $10k preset (`--preset smart`, 50/40/10); dashboard Index/Stocks/Crypto sections
- [ ] Intraday: Alpaca 15-minute bars, intraday calendar, flatten-before-close, PDT/settlement constraint, runner ticking every 15 min during market hours
- [ ] Day-trading strategies: "buy sharp dips" + "ride breakouts", tested side by side through the gate
- [ ] Two $10k portfolios (Smart: 50/40/10; Day trading), dashboard organised into four sections
- [ ] Then go live: `db init` → create both accounts → `runner start`

## Phase 6: AI and templates (later)
- [ ] Claude plain-English → strategy schema builder
- [ ] Template library, fork/clone, alerts
