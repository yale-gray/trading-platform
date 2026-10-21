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

## Wave 3: Engine (next)
- [ ] 003 T6–T10 risk checks, stops + kill switch, SimBroker, rebalancer, `PortfolioEngine` · 002 T3 evaluate
- [ ] Carried over from Wave 2 audits: persist stop overlay in `EngineState`; I4/I7 transfer-pair checks; execution minors F7, F10–F14 (fill grids, duplicate fill ids, FEE linkage, DEPOSIT rules, non-positive close, verify cost, cent exponent); config tests
- [ ] Move `RevisionQuarantinedError` to `data/errors.py` and re-export; fix SPEC-001 "price-return-like" wording
- [ ] Strategy evaluator must normalize `required_symbols()` before data lookups
- [ ] Re-audit residual minors (see docs/reviews/*wave2*): ledger-from-events FILL without FEE, state copy cost, zero-proceeds SELL, qty precision; data head-prepend report-once, multi-process read retry, dir fsync, data F11/F12/F14/F15

> From here, build order follows the **dispatch waves in ADR-0001 §3**. The phases below group the same work by area.

## Phase 1: Market data (data-lead)
- [ ] `DataProvider` interface, plus yfinance and ccxt adapters
- [ ] Parquet cache, UTC timestamps, gap and duplicate checks
- [x] Offline fixture dataset for tests (synthetic, ADR-0001 D9)

## Phase 2: Strategy and backtest (strategy-lead + execution-lead)
- [ ] Strategy schema, plus indicators (SMA, EMA, RSI, MACD, Bollinger, ATR, returns)
- [ ] Event-driven backtester (*t* → *t+1* fills), fees, slippage
- [ ] Metrics, benchmark comparison, OOS split, walk-forward, ±20% sensitivity
- [ ] The 3 sleeves from STRATEGY.md encoded and backtested

## Phase 3: Paper trading (execution-lead + platform-lead)
- [ ] SimBroker, ledger, portfolios with sleeves, monthly rebalance
- [ ] Sizing, limits, ATR stops, kill switch
- [ ] SQLite persistence and the APScheduler runner (restart-safe)

## Phase 4: API and dashboard (platform-lead)
- [ ] FastAPI endpoints
- [ ] Streamlit: portfolios, equity vs benchmark, positions, trades, perf cards

## Phase 5: AI and templates
- [ ] Claude plain-English → strategy schema builder
- [ ] Template library, fork/clone, alerts
