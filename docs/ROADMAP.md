# Roadmap

Status key: `[ ]` todo · `[~]` in progress · `[x]` done. The PM owns this file.

## Phase 0: Scaffold
- [x] git init, `CLAUDE.md` (PM manual), 9 agent files in `.claude/agents/`
- [x] `docs/STRATEGY.md`, spec/ADR templates, process log
- [x] `uv` project with pytest and ruff, package skeleton `src/tradebot/`

## Phase 0.5: Lead research round (option B)
- [ ] Each of the 5 leads writes a design spec for its area (`docs/specs/001`–`005`)
- [ ] PM reconciles them into **ADR-0001: Architecture and module interfaces**

## Phase 1: Market data (data-lead)
- [ ] `DataProvider` interface, plus yfinance and ccxt adapters
- [ ] Parquet cache, UTC timestamps, gap and duplicate checks
- [ ] Offline fixture dataset for tests

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
