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

## Wave 4: Backtester (next, lean mode)
- [ ] Small carry-overs: zero-qty SELL reason → `BELOW_MIN_SIZE` (`sim/broker.py`); `KillSwitch.reset` guard at module level (strict xfail in `tests/risk/test_killswitch.py`)
- [ ] Carried from the Wave 3 audit (`docs/reviews/003-wave3-engine-audit.md`): remaining mutant survivors and test gaps G7–G9 (broker sells-first/cash-shrink/expiry, restore with live stops and pending orders, risk repeat-call idempotency and cash reservation). Fold them into Wave 4's property tests (003 T12)
- [ ] 002 `BarDriver` + `TargetBook` + `trades.py`, metrics, backtest engine, OOS split, walk-forward, ±20% sensitivity, validation gate
- [ ] Property and invariant tests (003 T12, 002 T9), golden backtests G1–G4 on the synthetic fixtures
- [ ] The 3 sleeves from STRATEGY.md backtested against buy-and-hold

## Wave 5: Paper trading + dashboard (planned)
- [ ] SQLite persistence, restart-safe APScheduler runner, engine checkpoints (004)
- [ ] FastAPI endpoints, unified `tradebot` CLI
- [ ] Streamlit dashboard (dev console pages merge in): portfolios, equity vs benchmark, positions, trades, perf cards

## Phase 6: AI and templates (later)
- [ ] Claude plain-English → strategy schema builder
- [ ] Template library, fork/clone, alerts
