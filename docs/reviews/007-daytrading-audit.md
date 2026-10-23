# Audit 007: Day-trading feature (ADR-0001 A21; commits 329cbf0, b8c2508)

- **Auditor:** quality-lead. **Date:** 2026-10-23. **Mode:** lean, read-only (scratch copy for probes and mutants; real `tradebot.db` and `~/.tradebot/cache` not touched; no live download; no git writes).
- **Evidence:** full suite 2387 passed; `ruff check .` clean. Probes ran from `/private/tmp/.../scratchpad/` (`probe_prefix.py`, `probe_t1.py`, `probe_halt.py`, `probe_db.py`, `mut.py`).
- **Verdict: PASS-WITH-NOTES for backtesting; FAIL for paper-trading the breakout sleeve until M1 is fixed.** The simulated accounting is sound. The live data path has one real problem: the live decision bar always comes from IEX, but the breakout rule compares its volume with a SIP-based average.

## Findings (ranked)

**Blockers: none** (nothing that makes backtest numbers false).

**Major**
- **M1. Live decisions use IEX volume against a SIP baseline (breakout sleeve effectively dead live; dip sleeve slightly skewed).**
  - `data/intraday_sync.py:256-286` (`with_live_tail`) appends IEX bars for everything newer than the SIP cache (SIP lags by 16 min). The bar the driver decides on is therefore always an IEX bar.
  - `strategies/stocks_dt_breakout.yaml:31` requires `volume > sma(volume, 20)`. IEX volume is a few percent of consolidated volume, so the current bar almost never beats a 20-bar average built mostly from SIP bars. The backtest (all SIP) will not match paper.
  - The dip sleeve's price indicators (`devatr`, RSI(2)) also mix feeds, by a small amount.
  - The module header documents "never splice into one cached series" but not the in-memory splice.
  - **Fix options (PM/data-lead pick one):**
    - (a) drop the volume leg or make it feed-independent (e.g. volume rank among symbols on the same bar);
    - (b) for live portfolios, build the baseline from IEX history as well (a separate `iex/15m` cache, never mixed with SIP);
    - (c) scale IEX volume by a per-symbol SIP/IEX ratio.
  - **Gate before any "paper = backtest" claim:** a parity test on real data over the same dates, SIP-only vs SIP + IEX tail, comparing signals.
- **M2. Halted or no-bar symbols are flattened at their last close.**
  - `portfolio/engine.py:549-556` (`_flatten`): a held symbol with no bar on the last bar is sold at `_last_close` minus slippage.
  - Probe `probe_halt.py`: ABT's bars removed after 15:15; it was sold at 15:15 close less 5 bps at 15:45. "Nothing held overnight" holds, but a really halted stock cannot be sold at a stale price.
  - Optimistic, rare. **Fix:** log and count "flattened without a bar" in the run log, and charge an extra haircut (e.g. 100 bps) on those sells or flag the run. Alternatively treat as an overnight exception and report it.
- **M3. Live tail costs one HTTP request per symbol per tick.**
  - `intraday_sync.py:268` calls `fetch_bars` per symbol, not `fetch_many`. 500 symbols at the 180 req/min bucket is about 2.8 min per 15-min tick (plus SIP refresh), and `require_fresh` doubles it (`service.py:499-503`).
  - It fits the window but is fragile (the 429 retry compounds it). **Fix:** use `fetch_many` in groups of 100 for the tail. Not yet measured live.

**Minor / notes**
- m1. **Test gap, mutant M8 survived:** `broker.py:296` (cancel pending at flatten) can be removed with no test failing. The path is only reachable when a BUY is still pending on the last bar (symbol has no bar there). Without the cancel, yesterday's BUY would fill at the next morning's open. Add a test: pending BUY for a symbol missing the last bar, then assert it is cancelled and nothing fills the next day.
- m2. **Round trip on the last bar:** a BUY signalled on bar L-1 fills at L's open and is flattened at L's close: a 15-minute round trip that pays slippage twice. Suggest suppressing BUYs on the second-to-last bar too (driver, `backtest/driver.py` session-end branch).
- m3. `_lean` state differs from eager: `export_state` has `last_close` / `last_vol20` for held and targeted symbols only (39 of 40 symbols differ in the probe). The ledger, fills and snapshots are identical, and a lean restore continues identically (tested), so this is cosmetic, but state hashes are not comparable between lean and eager runs.
- m4. `_session` computes `bar_index` per symbol from its own first bar of the day (`strategy/engine.py:337`). A symbol missing the 09:30 bar gets a shifted opening range. SIP rarely misses it; IEX tail can. Compute the index from the session calendar instead.
- m5. Live `tick.py:237` marks a bar "ready" if any symbol has it. Symbols whose bar arrives later are skipped for that bar permanently (fills and marks use the last close). A live vs backtest divergence, not a lookahead.
- m6. `credentials`: `.env.example` has no `APCA_*` lines; add placeholders (no real values).
- m7. Stuck state: if the last bar of a day never appears for any symbol, `ready` stops at that bar every tick (`data_unavailable`), positions stay frozen overnight and nothing progresses. It does not trade, but nothing flattens either; a "skip the bar after N minutes" rule or an alert is needed. `reset-from` exists as the manual escape.

## Checklist results

### 1. No lookahead intraday: PASS
- Fills happen at the next bar's open. The engine's step order is unchanged; the driver submits targets only after `process_bar`. Flatten fills carry created stamp = previous bar (I8).
- Session operands: `_session` (`strategy/engine.py:337-357`) gives `range_high/low` only from `bar_index >= bars-1`, from the first `bars` bars. `open` is the day's first open. All causal.
  - Mutants M3a (value visible early) and M3b (one extra bar) are both caught by `test_session_operands_are_per_session_and_causal`.
- Incomplete bar: the provider filters `open+15m <= clock.now` (`alpaca_provider.py` `_to_frame`) and `IntradaySync.read` filters again; the runner's due-bar rule adds a 1-min delay.
- **Prefix invariance probe** (40 symbols, both real YAMLs, seed 21, 13 sessions, 297 ledger events): data truncated at three arbitrary bars (including mid-session with a held position) gives a byte-identical ledger for every event before the cut and at the cut. Flatten of a partial final day is not assumed (`_find_session_ends` uses the XNYS close).

### 2. Flatten-at-close: PASS-WITH-NOTES
- Fill at the last bar's close with sell slippage; BUYs are not submitted on the last bar; the target books are cleared; pending orders are cancelled.
- **Is it achievable live?** Mostly yes. The flatten is unconditional (no signal decides it), so there is no information leak: it is a market-on-close order queued at about 15:45 (NYSE MOC cut-off is 15:50). The price differences are (1) the closing auction print is not the last 15-minute bar's last trade, (2) a 5 bps slippage stands in for that. A mild optimism, not a cheat. **Recommendation:** keep, and document "fills at bar close = MOC proxy"; when real data is available, measure the difference between the 15:45 bar close and the official close and set slippage for flatten fills from it.
- Overnight: probe over 11 sessions crossing a month end and a forced-halt run: positions flat at every session end. Early closes use the calendar (`test_early_close_day_...`). Exceptions: M2 (halted symbol sold at stale price) and m7.
- Mutants: M1 flatten disabled: **caught**. M9 BUY allowed on last bar: **caught**.

### 3. T+1 settlement: PASS
- Hand-check (`probe_t1.py`, deposit 10,000; Thu 2024-03-28, Good Friday 03-29 closed):
  - Thu 10:30 ET BUY 50 AAA @100 = 5,000 (cash 5,000).
  - Thu 15:45 SELL 50 @110 = 5,500 (cash 10,500, **unsettled 5,500, spendable 5,000**).
  - Thu BUY 9,000: refused ("settled cash 5,000 cannot cover 9,000").
  - Mon 04-01 (next session, over the holiday weekend): unsettled 0, spendable 10,500. BUY 10,000 accepted.
  - Replay of the events reproduces cash and unsettled exactly.
  - Daily-date variant: Fri 2024-01-12 sale is unsettled Friday only; settled by Monday/Tuesday (MLK).
- Restore: `engine.py:223-231` cross-checks stored unsettled against the replay. Buys, BUY fees and rebalance transfers use `spendable`; broker shrink (`broker.py:327`) and risk sizing (`risk/limits.py:available_cash`) use settled cash.
- Mutants: M2 (T+0) **caught**; M5 (broker shrink on total cash) **caught**; M6 (risk sizing on total cash) **caught**; M7 (settles a day early) **caught**.
- Note: a sell on the same day as its own fee: the fee eats that SELL's unsettled lot (`ledger.py:228`). Fine.

### 4. Lean engine path: PASS
- Lean vs eager: identical ledger on the intraday probe (297 events); the existing tests also pin the daily G4 golden in lean mode and a mid-run restore.
- Nothing needed is skipped: `_lean_bars` marks every held and pending-order symbol (`engine.py:403`), targeted symbols are marked on demand with `last_known` as fallback (`:409`). Equity and the kill switch use held symbols only, so they are unaffected.
- Mutant M4 (lean skips held symbols): **caught**. See m3 for the harmless state difference.

### 5. Data and keys safety: PASS
- The only URL is the constant `https://data.alpaca.markets/v2/stocks/bars` (`alpaca_provider.py`); `_request` re-checks the host and refuses trading hosts. No other Alpaca reference in `src/` or `scripts/`. No order code.
- Keys are held in a class with a hidden repr, sent only as headers, passed through a redactor in every error message; settings use `SecretStr`. I grepped `logs/`, `docs/`, `src/`, `tests/`, `strategies/`, `web/src/` and `tradebot.db` for the two values (without printing them): no hits. `.env` is git-ignored and untracked.
- The IEX tail is returned in memory only; `with_live_tail` has no cache writes, and no caller writes its result. SIP history goes to `alpaca/15m`.
- The provider's `now` comes from the injected `Clock`.

### 6. DB migration and scheduler guard: PASS
- On a backup copy of the real DB (14 ledger events, 11 orders, 2 portfolios): `create_all` + `migrate_asof` repaired 0 cells (all daily rows, untouched), row counts unchanged, `integrity_check` ok, ORM reads `date` values as before, `restore_context` works on `smart_investing`. Idempotent on a second run. Text storage means `DATE`-declared columns need no table rebuild.
- Scheduler (`_intraday_window` evaluated at every cron minute): normal day 26 firing points from 09:46 to 16:01 ET (EST and EDT both, DST Monday 2024-03-11 included); early closes (2024-07-03, 2024-11-29) 14 firing points ending 13:01; holidays and weekends none. The cron is in New York time, so DST needs no special handling. Catch-up after downtime uses `due_bars_15m` plus `MAX_CATCHUP_BARS`.

### 7. Mutation probe (scratch copy; targeted tests plus `tests/portfolio, backtest, strategy, risk, sim, lookahead` and the new platform tests)
| Mutant | Result | Caught by |
|---|---|---|
| M1 flatten disabled (`engine.py`) | CAUGHT | `test_daytrading_e2e::test_no_position_is_ever_held_overnight` |
| M2 settlement T+0 (`ledger.unsettled` = 0) | CAUGHT | `test_settlement_t1::test_sale_proceeds_are_unsettled_...` |
| M3a first-hour range visible before the hour ends | CAUGHT | `test_session_operands_are_per_session_and_causal` |
| M3b range includes one extra bar | CAUGHT | same |
| M4 lean path skips held symbols | CAUGHT | `test_intraday_driver::test_every_session_ends_flat_...` |
| M5 broker shrink on total cash | CAUGHT | `test_broker_shrinks_a_buy_to_the_settled_cash_at_fill_time` |
| M6 risk sizing on total cash | CAUGHT | `test_buy_is_sized_from_settled_cash_only` |
| M7 settles a day early | CAUGHT | `test_sale_proceeds_are_unsettled_...` |
| M8 no cancel of pending orders at flatten | **SURVIVED** | see m1 |
| M9 BUY allowed on the last bar | CAUGHT | `test_every_session_ends_flat_...` |

### 8. Usability gap list for the PM (not defects)
Today the page reads `/portfolios/{id}/trades` only. Settled cash is in the engine snapshot (`unsettled`) but is **not stored** per bar (`equity_snapshot` has `cash` only) and not exposed. Round-trip P&L exists in `backtest/trades.py::closed_trades` but not in the API. Minimal additions:
1. **DB:** add nullable `unsettled TEXT` to `equity_snapshot` (written by `write_snapshots` from `snapshot.unsettled`; a migration like `migrate_asof`, default NULL on old rows).
2. **`PortfolioDetail`** (or `/portfolios/{id}`): add `cash`, `unsettled_cash`, `settled_cash` (= cash - unsettled), `as_of` (bar ts). Page text: "Money you can use now: X. Sale money arriving tomorrow: Y."
3. **`GET /portfolios/{id}/roundtrips?from=&to=&limit=`:** items `{sleeve_key, symbol, entry_ts, exit_ts, qty, entry_price, exit_price, fees, pnl, pnl_pct, exit_reason: signal|stop|flatten}`, built from ledger FILL events paired per sleeve/symbol (reuse `closed_trades`), plus `total` and `win_rate`.
4. **`GET /portfolios/{id}/daily`:** per New York session `{date, start_equity, end_equity, pnl, pnl_pct, n_roundtrips, benchmark_pnl_pct}` for the "today vs yesterday" tiles and a day-by-day table.
5. Flag on the status endpoint: `flattened_without_bar` count (M2) and `live_feed_mixed: true` (M1) so the page can warn "decisions use a lighter live data feed".

## Fit for the next wave
- The real YAMLs were only run on synthetic data. Before any result is quoted: SIP-only backtest through the gate, then M1 parity test, then paper.
- ADR-0001 A21 implementation notes are accurate; add the M1 limitation and the MOC-proxy note.

## Report path
`/Users/yalegray/Trading_Bot/docs/reviews/007-daytrading-audit.md`
