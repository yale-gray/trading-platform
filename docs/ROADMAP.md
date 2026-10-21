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

## Wave 3: Engine (next)

> **Resume here (PM notes, 2026-10-21).** Nothing in Wave 3 has been dispatched yet; the tree is clean at commit `c06cc0c`. Dispatch in this order. Same rules as Wave 2: disjoint file lists, workers lint/test only their own files, no repo-wide `ruff format`.
> 1. **Stage A (parallel):**
>    - 003 T6 sizing + risk checks [sonnet]
>    - 003 T7 trailing stop + kill switch [sonnet]
>    - 003 T8 SimBroker fills [sonnet]
>    - 002 T3 signal evaluation (`compile_strategy`/`prepare`/`evaluate`) [sonnet]
>    - Junior cleanup [haiku]: move `RevisionQuarantinedError` to `data/errors.py` and re-export it, and fix the SPEC-001 "price-return-like" wording.
> 2. **Stage B:**
>    - 003 T9 rebalancer [sonnet] (after T6)
>    - 003 T11c sizing tests [haiku] (after T6)
>    - 005 T12 strategy truncation tests [sonnet] (after 002 T3). This also turns the skipped lookahead meta-test on.
> 3. **Stage C:** 003 T10 `PortfolioEngine` facade + `EngineState` [sonnet] (after T6–T9). It must also take in the carried-over items below: stop overlay persisted in `EngineState`, I4/I7 transfer-pair checks, and execution minors F7, F10–F14.
> 4. **Stage D:**
>    - 003 T13 scenario tests [haiku]
>    - 005 T11 hand-calc fills, fees, rebalance, caps and stops tests [haiku]
>    - config tests [haiku]
> 5. **Then:**
>    - reviewer;
>    - mandatory quality-lead audit (money, fills, signals);
>    - fix pass, re-audit, commit;
>    - add trade/signal views to the dev console.
>
> The 002 T3 brief must say: normalize `required_symbols()` before data lookups; `bars_held` comes from `HeldPosition.entry_ts`; `evaluate` never computes `vol20`/`atr14` (the driver does, in Wave 4).

- [ ] 003 T6–T10 risk checks, stops + kill switch, SimBroker, rebalancer, `PortfolioEngine` · 002 T3 evaluate
- [ ] Carried over from Wave 2 audits: persist stop overlay in `EngineState`; I4/I7 transfer-pair checks; execution minors F7, F10–F14 (fill grids, duplicate fill ids, FEE linkage, DEPOSIT rules, non-positive close, verify cost, cent exponent); config tests
- [ ] Move `RevisionQuarantinedError` to `data/errors.py` and re-export; fix SPEC-001 "price-return-like" wording
- [ ] Strategy evaluator must normalize `required_symbols()` before data lookups
- [ ] Re-audit residual minors (see docs/reviews/*wave2*): ledger-from-events FILL without FEE, state copy cost, zero-proceeds SELL, qty precision; data head-prepend report-once, multi-process read retry, dir fsync, data F11/F12/F14/F15

## Wave 4: Backtester (planned)
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
