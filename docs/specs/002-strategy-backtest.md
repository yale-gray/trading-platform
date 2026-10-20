# SPEC-002: Strategy engine and backtester

- **Lead:** strategy-lead
- **Status:** approved (conforms to ADR-0001)
- **Roadmap item:** Phase 2 (strategy schema, indicators, backtester, metrics, OOS/walk-forward/sensitivity, 3 sleeves encoded)
- **Related ADRs/specs:** SPEC-001 (market data), SPEC-003 (execution/sim/risk/portfolio), SPEC-004 (platform/runner), SPEC-005 (quality); conforms to ADR-0001 (accepted; the ADR wins on any conflict). Source of rules: `docs/STRATEGY.md` §4, §7.

## Goal
Define how strategies are written (declarative pydantic models that round-trip to YAML/JSON), how they are evaluated (one pure function used by both the backtester and the forward paper runner), and how they are honestly tested (metrics, buy-and-hold benchmark, 70/30 hold-out, walk-forward, +-20% sensitivity, and the STRATEGY.md §7 gate as code).

## Non-goals
- Fills, fees, slippage, sizing, risk limits, stop *execution*, sleeve rebalancing, ledger: all SPEC-003.
- Data fetching, caching, calendars: SPEC-001. Scheduling, API, dashboard: SPEC-004.
- Intraday bars, shorting, options, leverage. v1 is daily bars, long-only.
- The Claude "plain English -> strategy" builder (Phase 5). The schema is designed so it can emit it, nothing more.
- Parameter *optimisation* beyond an optional grid search on in-sample data (see Walk-forward).

## Design

### 1. One evaluation path
```
bars (data layer) --> compile(spec) --> prepare(bars) --> evaluate(features, asof, held) --> SignalSet --> TargetBook --> PortfolioEngine.submit_target_weights
                      (pure)            (vectorised,      (pure, reads row `asof` only)   (driver.py)        (SPEC-003)
                                         causal)
```
The stages after `evaluate` and the per-bar loop live in `BarDriver` (`backtest/driver.py`, section 6), the one loop shared by `run_backtest` and the platform runner.
- `evaluate` is **pure and stateless**: no hidden memory. Anything that depends on history of the position (bars held) is derived from the `held` argument that `BarDriver` builds from `engine.snapshot()` (positions with `entry_asof`). That is what lets the platform runner restart safely and call the *same* function the backtester calls. There is no separate live code path.
- `BarDriver` calls `prepare` once on the bars it is given, then `evaluate` per decision bar. The backtester gives it full history; the live runner gives it bars up to the latest completed bar and steps the new bar. Correctness depends on every indicator being **causal** (value at t uses bars <= t). Enforced by the truncation-invariance test (AC 3).
- Recursive indicators (EMA, RSI, ATR, MACD) have unbounded memory through their seed. For backtest == live to the last digit, the runner must pass bars from the same `history_start` the backtest used (not an arbitrary last-N window). Documented in the runner contract; differences after about 10x the period are < 1e-6 anyway.

### 2. Strategy schema
A strategy is one **sleeve's rules**. Sleeve weights (50/20/20/10) and cross-sleeve rebalancing are portfolio config (SPEC-003); the backtester takes `sleeves: list[SleeveSpec]` and passes weights through.

`StrategySpec` = common header + a `kind` discriminator with two bodies:

| Field | Meaning |
|---|---|
| `schema_version`, `id`, `name`, `description` | metadata |
| `asset_class` | `equity` or `crypto`. One class per strategy (validator rejects mixed universes; fallback symbol must match) |
| `parameters` | named numbers. Any numeric field elsewhere may be a literal or `$name`. The sensitivity sweep perturbs exactly these |
| `constraints` | optional list like `"fast < slow"`; sweep variants violating them are skipped and logged |
| `universe` | list of symbols (`SPY`, `BTC/USD`) |
| `schedule` | when decisions are made: `daily`, `weekly` or `monthly` (see below) |
| `kind: rotation` | **ranking** body: `eligibility`, `rank`, `select`, `fallback` |
| `kind: rules` | **per-symbol** body: `entry`, `exit`, `position` |
| `stops` | optional list of **stop requests** for execution (section 5) |

**Operands** (things a condition compares): a bare number; `$param`; `{price: close|open|high|low|volume}`; `{ind: <name>, period: n|$p, of: close, ...}` (`of` defaults to `close`; extra keys per indicator, e.g. `fast/slow/signal`, `k`, `field: macd|signal|hist|upper|mid|lower|pctb`); `{pos: bars_held}` (rules kind only; bars since the entry fill, 0 on the fill bar). Any indicator or price operand may add `symbol: X` for a cross-asset reference (default: the symbol being evaluated); the symbol is added to required data automatically.

**Conditions**: `{gt|ge|lt|le|eq: [A, B]}`, `{crosses_above|crosses_below: [A, B]}` (A[t-1] <= B[t-1] and A[t] > B[t]), `{all: [...]}`, `{any: [...]}`, `{not: C}`, `{always: true}`. Any comparison involving NaN (warmup, or a symbol with too little history) is **False**, and `not` of a NaN comparison is also False (NaN-ness propagates as "unknown", checked before negation). So a young symbol is never eligible or tradable: this is also how the universe stays date-aware without a separate membership table.

**Warmup**: `compile()` computes `warmup_bars` = max lookback over all operands (resolved params) + 1. The backtester refuses to start trading before `warmup_bars` of history exist before `start`.

**Schedule**: derived from the bar index only (causal, no market-calendar dependency).
- `daily`: every bar in the strategy's own calendar.
- `weekly`: bar whose ISO week differs from the previous bar's.
- `monthly`: bar whose calendar month differs from the previous bar's (i.e. the **first bar of the month**, evaluated at its close; fills at the next open). We deliberately do not use "last trading day of month" because that cannot be known at bar t without a forward-looking calendar.
- The first decision-eligible bar of a run is always a decision bar (`force_decision=True`), so a sleeve started mid-month invests immediately rather than idling in cash.
- A strategy's calendar = union of its universe's bar timestamps. Equity sleeves therefore do not evaluate on weekends; crypto evaluates every day.

#### 2a. How ranking/rotation and per-symbol rules differ (the hard part)

| | `rotation` | `rules` |
|---|---|---|
| Question asked | "Of these N symbols, which k do I hold *right now*?" (cross-sectional, relative) | "Is symbol X in or out?" (time-series, absolute, independent per symbol) |
| State needed | none: the answer depends only on today's scores. Held positions matter only to flatten the losers | per-symbol in/out and bars held, taken from `held` |
| Decision cadence | `schedule` (monthly) | `schedule` (daily); entries only when flat, exits only when held |
| Output | the **complete target set** on a decision bar: selected -> `long`, held-but-not-selected -> `flat`. Empty slots -> fallback | only symbols with an opinion this bar: flat+entry -> `long`; held+exit -> `flat`; held+no exit -> `long` (explicit hold) |
| Weight hint | `1/top_n` per selected symbol; `(top_n - n_selected)/top_n` to the fallback symbol if configured | `position.weight` (`equal` = 1/len(universe), or a number), per symbol |

Rotation evaluation on a decision bar (all inputs read at row asof):
1. `eligible = {s in universe : eligibility(s) is True}` (default filter-then-rank, `filter_mode: pre_rank`). `filter_mode: post_rank` ranks everything first, takes the top k, then drops the ineligible (leaving more empty slots). STRATEGY.md §4 is ambiguous; the shipped sleeve uses `pre_rank` (ADR-0001 D10).
2. `score(s) = rank.by` evaluated for each eligible symbol; sort by (score desc, symbol asc). The symbol tiebreak keeps results deterministic. NaN scores are dropped.
3. `selected = first select.top` symbols (fewer if fewer eligible).
4. Empty slots go to `fallback.symbol` if given (and its optional `require` condition holds), else stay as cash (no intent, weight is simply unallocated).

Rules evaluation per symbol uses the same condition language; `exit` omitted means **hold-while**: exit = `not entry` (used by the crypto trend sleeve). Exits take priority: if a held symbol's exit is true it is flat even if entry is also true. A symbol sold by execution's stop appears in `held` as absent on the next bar; the strategy may then re-enter if `entry` is still true (execution's stop lock blocks the re-entry until the strategy has gone flat once, i.e. been absent from the `TargetBook` or target 0, ADR-0001 D3).

Cash buffer (10%) is not a strategy; it is a `SleeveSpec` with no strategy, owned by SPEC-003.

### 3. Concrete schemas for the three v1 sleeves

**Sleeve 1: monthly momentum rotation** (`strategies/core_momentum.yaml`)
```yaml
schema_version: 1
id: core_momentum
name: Trend-filtered ETF momentum
kind: rotation
asset_class: equity
parameters: {mom_lookback: 126, trend_len: 200, top_n: 3}   # 126 bars ~ 6 months
universe: [SPY, QQQ, IWM, EFA, EEM, TLT, GLD, XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLU, XLB]
schedule: {freq: monthly}
eligibility:                       # 200-day SMA trend filter
  gt: [{price: close}, {ind: sma, period: $trend_len}]
filter_mode: pre_rank
rank: {by: {ind: ret, period: $mom_lookback}, order: desc}
select: {top: $top_n, weighting: equal}
fallback: {symbol: IEF}            # empty slots -> IEF; omit this block for cash
```
**Sleeve 2: RSI(2) mean reversion** (`strategies/mean_reversion.yaml`)
```yaml
schema_version: 1
id: mean_reversion
name: RSI(2) pullback in an uptrend
kind: rules
asset_class: equity
parameters: {rsi_len: 2, rsi_entry: 10, rsi_exit: 70, trend_len: 200, max_hold: 5, atr_len: 14, stop_mult: 2.5}
universe: [SPY, QQQ]
schedule: {freq: daily}
entry:
  all:
    - lt: [{ind: rsi, period: $rsi_len}, $rsi_entry]
    - gt: [{price: close}, {ind: sma, period: $trend_len}]
exit:
  any:
    - gt: [{ind: rsi, period: $rsi_len}, $rsi_exit]
    - ge: [{pos: bars_held}, $max_hold]      # time exit
position: {weight: equal}
stops:
  - {type: atr_trailing, period: $atr_len, mult: $stop_mult, anchor: highest_close_since_entry}
```
**Sleeve 3: crypto SMA 20/50 trend** (`strategies/crypto_trend.yaml`)
```yaml
schema_version: 1
id: crypto_trend
name: Crypto SMA 20/50 trend
kind: rules
asset_class: crypto
parameters: {fast: 20, slow: 50, atr_len: 14, stop_mult: 2.5}
constraints: ["fast < slow"]
universe: [BTC/USD, ETH/USD, SOL/USD]
schedule: {freq: daily}
entry:
  all:
    - gt: [{price: close}, {ind: sma, period: $slow}]
    - gt: [{ind: sma, period: $fast}, {ind: sma, period: $slow}]
# exit omitted => hold-while: exit when NOT entry
position: {weight: equal}
stops:
  - {type: atr_trailing, period: $atr_len, mult: $stop_mult, anchor: highest_close_since_entry}
```
**Benchmark** (generated by the backtester, never hand-written): `kind: rules`, `universe: [SPY]` (or `[BTC/USD]`), `entry: {always: true}`, `exit: {not: {always: true}}`, `position: {weight: 1.0}`. It runs through the **same execution engine** so fees and slippage apply (hard rule 4).

Time-exit semantics: `bars_held` is 0 on the entry fill bar. The exit fires when `bars_held >= 5` at a close, filling the next open, so the position is held about 5 sessions of exposure plus the exit open.

### 4. Indicator formulas
All operate on a float Series `x` (default `close`, adjusted for stocks) indexed by bar; `t` is the current bar; results are NaN until the stated first valid bar. Float64 throughout. "Periods" are **bars**, not calendar time (126 bars ~ 6 months of equity sessions; the crypto parameter would differ, so sleeves choose their own numbers).

| Indicator | Definition | First valid t |
|---|---|---|
| `sma(n)` | SMA_t = (1/n) * sum_{i=0}^{n-1} x_{t-i} | n-1 |
| `ema(n)` | alpha = 2/(n+1). Seed EMA_{n-1} = SMA_{n-1}(n). EMA_t = alpha*x_t + (1-alpha)*EMA_{t-1} | n-1 |
| `ret(n, skip=0)` | x_{t-skip} / x_{t-skip-n} - 1 | n+skip |
| `rsi(n)` Wilder | d_t = x_t - x_{t-1}; U_t = max(d_t,0); D_t = max(-d_t,0). AU_n = mean(U_1..U_n), AD_n likewise. For t>n: AU_t = ((n-1)*AU_{t-1} + U_t)/n, same for AD. RS = AU/AD; RSI = 100 - 100/(1+RS). If AD=0: RSI = 100 (AU>0) or 50 (AU=0) | n |
| `macd(fast=12, slow=26, signal=9)` | macd_t = EMA_fast(x) - EMA_slow(x); signal_t = EMA_signal(macd) seeded by the SMA of its first `signal` valid values; hist = macd - signal | slow+signal-2 |
| `bbands(n=20, k=2)` | mid = SMA(n); sd = **population** std (ddof=0) of the last n values; upper = mid + k*sd; lower = mid - k*sd; pctb = (x - lower)/(upper - lower) (NaN if upper=lower); bandwidth = (upper-lower)/mid | n-1 |
| `atr(n)` Wilder | TR_t = max(h_t - l_t, abs(h_t - c_{t-1}), abs(l_t - c_{t-1})) for t>=1. ATR_n = mean(TR_1..TR_n). ATR_t = ((n-1)*ATR_{t-1} + TR_t)/n | n |
| `vol(n)` | sample std (ddof=1) of simple daily returns r_t = x_t/x_{t-1} - 1 over the last n returns; **not** annualised | n |

Sources: Wilder (1978) *New Concepts in Technical Trading Systems* for RSI and ATR smoothing; Bollinger (2001) *Bollinger on Bollinger Bands* for population std; Appel's MACD(12,26,9) as standard; Connors/Alvarez for RSI(2) pullbacks (the strategy is the PM's; we only implement the indicator). Cited from memory, not web-verified in this round; the indicator tests carry hand-computed reference vectors so the formulas, not any library, are the source of truth.

### 5. How a strategy requests stops (owned by execution; ADR-0001 D5)
A strategy never stops anything. It *declares* `stops: [StopRequest]` in the spec, and `evaluate` copies the request onto each `long` `Intent` it emits (informational for dashboards). Execution (SPEC-003 §7) owns the stop level, tracking and exit. The strategy side supplies exactly two things: the `mult` (copied into the engine config by the driver) and the ATR(14) value (attached to each `Bar` by the driver).

```
StopRequest: type "atr_trailing"; period (ATR bars); mult; anchor "highest_close_since_entry"
```
Semantics (canonical, from SPEC-003 §7; daily bars, no intraday path):
- `high_water_close` = close of the fill bar on the first fill from flat, then `max(high_water_close, close)` each later close.
- After the close of bar t: `candidate_t = high_water_close - mult * ATR_t`; `stop_level_t = max(stop_level_{t-1}, candidate_t)` (ratchets up only, floor 0).
- **Trigger is `close_t <= stop_level_t`** (close-based, `<=`, not `<`). Execution then creates a full-position SELL with `origin=STOP` that fills at the open of t+1 (the same no-lookahead rule as every signal). If ATR is unavailable the level is unchanged but is still tested.
- **v1 supports a single ATR period (14) per engine** (ADR D5). The driver attaches `atr14` only. `StrategySpec` validation therefore rejects an `atr_trailing` stop whose resolved `period != 14`, and a strategy with more than one stop. The `mult` may differ per sleeve (`SleeveConfig.trailing_stop_atr_mult`).
- **Stop lock / cooldown:** after a stop exit execution blocks re-entry of that symbol in that sleeve until the strategy has gone flat once, i.e. the sleeve's `TargetBook` target for it was 0 or the symbol was absent (ADR D3, resolving SPEC-003 Q4). No strategy-side cooldown parameter exists.
- A stop exit appears in the ledger as a SELL fill whose order has `origin=STOP`; `trades.py` maps it to `exit_reason="stop"` (section 6b).
- Execution takes ATR from the driver (computed with `tradebot.strategy.indicators.atr`), so there is one ATR implementation.

### 6. Backtest loop: `BarDriver` is the single per-bar loop (ADR-0001 D3, D4, D5)

Calendar: master calendar = sorted union of all required symbols' timestamps within `[start, end]` (ADR D6: the engine clock is every date on which any symbol has a bar). A symbol with no bar on a date (e.g. SPY on Saturday) is skipped for fills, is marked at its last close by the engine, and never produces a decision (its strategy's calendar excludes that date). An equity order created on a weekend-clock bar waits for that symbol's next bar (SPEC-003).

The loop lives in `tradebot/backtest/driver.py` and is shared by `run_backtest` and the platform runner. `run_backtest` is only: build engine config, construct `PortfolioEngine`, construct `BarDriver`, iterate, derive results with `trades.py`.

```
def run_backtest(cfg: BacktestConfig, bars: Mapping[str, DataFrame]) -> BacktestResult:
    validate(bars)                                        # bar contract (see "Interfaces needed from data")
    assert every strategy sleeve has >= warmup_bars of history before cfg.start
    engine = PortfolioEngine(build_engine_config(cfg.engine, cfg.sleeves))   # SPEC-003; copies stop mult
    driver = BarDriver(engine, cfg.sleeves, bars, start=cfg.start)
    steps  = list(driver.run(end=cfg.end))
    # orders queued after the final bar never fill; open positions are reported, not force-closed
    return derive_result(cfg, engine, steps)              # trades.py functions over ledger + snapshots
```

`BarDriver.step(ts)` for one bar `ts` (a tz-aware UTC `pd.Timestamp`), in this fixed order:
1. `asof = ts.date()` (the **only** place the Timestamp becomes a `date`, ADR D1). Build `bars_today = {symbol: Bar(...)}` for symbols having a row at `ts`, in sorted symbol order, with `vol20 = indicators.vol(close, 20)` and `atr14 = indicators.atr(high, low, close, 14)` as of this close (NaN -> `None`). The two series are precomputed per symbol once, in `__init__`, from the full bar frames; they are causal, so a value at `ts` never depends on later rows.
2. `result = engine.process_bar(asof, bars_today)`. Inside the engine (SPEC-003 steps A-F): fills at this open, marks at this close, transfers, stops, snapshot and kill switch, rebalance decision.
3. For each sleeve with a strategy, in sorted `sleeve_id` order:
   - **Decision bar** iff `ts` is in the strategy calendar, `ts >= start`, the calendar position of `ts` is `>= warmup_bars - 1`, and (`strategy.is_decision_bar(ts)` or the sleeve has no decision yet in the `TargetBook`). The "no decision yet" case is the `force_decision=True` first-bar rule.
   - If decision bar: `held` is derived from `engine.snapshot()` (below); `signals = compiled.evaluate(feats, ts, held, force_decision=<no decision yet>)`; `book.update(signals)`.
   - **Submit** iff decision bar **or** `result.rebalance_planned` is true: `engine.submit_target_weights(sleeve_id, book.weights(sleeve_id), asof)`. Never submit on any other bar (submitting every bar would cause daily drift trades in monthly sleeves; vol-scaled weights drifting between decisions is intended). The cash-buffer sleeve (`strategy is None`) is never submitted.
4. Return `StepResult(asof, ts, bar_result, signals, targets)`.

Step order guarantees a signal made at `ts` cannot fill at `ts`: fills (step 2) happen before the bar's signals (step 3), and orders created by `submit_target_weights` fill at the next `process_bar`. The benchmark is a second `run_backtest` with the generated buy-and-hold sleeve, the same `EngineConfig` (risk, costs) and the same window.

**`TargetBook` semantics (exact, ADR D3):**
- The book holds, per sleeve, the weights of the `long` intents from that sleeve's **latest decision bar**: `weights[symbol] = intent.weight` for each intent with `target == "long"`.
- A symbol that is absent from the book (flat intent, no intent, or never selected) has target 0. `TargetBook.weights()` returns only positive weights; the engine treats an omitted symbol as 0.
- `update` replaces the sleeve's whole book (it never merges). Rotation sleeves emit the complete target set on a decision bar. Rules sleeves are daily, so their intents are complete: a held symbol always gets an explicit `long` or `flat`; a flat symbol is `long` only when its entry fires.
- "Absent means 0" is what re-arms SPEC-003's stop lock (section 5).
- The book is state. It is JSON-serialisable via `state()` / `from_state()` and the runner must persist it in its per-bar checkpoint (see Risks), so a restarted runner does not submit an empty book on the next `rebalance_planned` bar.

**`held` derivation:** `HeldPosition(symbol, entry_ts)` for every position in `engine.snapshot().sleeves[sleeve_id].positions` with `qty > 0`, where `entry_ts = pd.Timestamp(position.entry_asof, tz="UTC")` and `entry_asof` is SPEC-003's new snapshot field (set on a fill from flat, cleared at flat). The snapshot is read after `process_bar`, so today's open fills are included. `evaluate` computes `bars_held` as the number of strategy-calendar bars between `entry_ts` and `asof` (0 on the fill bar).

**Stop wiring:** `build_engine_config(base, sleeves)` returns a copy of `base` in which, for each sleeve with a resolved `atr_trailing` stop, `SleeveConfig.trailing_stop_atr_mult = Decimal(str(stop.mult))`, and `None` otherwise. It raises `ValueError` if sleeve ids differ between `base` and `sleeves`, if a stop's `period != 14`, or if a strategy has more than one stop.

**Runner usage (SPEC-004):** the runner constructs `BarDriver(engine, sleeves, bars, start=inception, book=restored_book)` where `bars` holds history **from the same `history_start` the backtest used** through the latest completed bar, then calls `driver.step(latest_ts)` for the new bar; catch-up is `for step in driver.run(after=engine_last_processed_asof)` which replays only bars with `ts.date() > after`, in order. Features are causal (AC 3), so a driver built on bars through T and a driver built on bars through T' > T give identical results for every bar <= T. A backtest and a paper run over the same bars must therefore produce a byte-identical ledger.

Determinism: no randomness, no wall clock in results, symbols iterated in sorted order, ties broken by symbol. `BacktestResult` carries `config_hash` (sha256 of canonical JSON of resolved config) and `data_fingerprint` (sha256 of the bar arrays) so golden tests pin both inputs.

### 6b. Trades, equity and exposure from the ledger (`backtest/trades.py`, ADR D3)
SPEC-003 offers no trade-tracking API. `trades.py` holds **pure functions** (no I/O, no clock) over the engine's ledger, its orders and its per-bar snapshots:
- **Closed trades.** Replay `FILL` events in `seq` order per `(sleeve_id, symbol)`, keeping a running quantity. A round trip opens when the quantity goes 0 -> >0 and closes when it returns to 0; scale-ins and trims belong to the same trade. For each trade: `entry_asof` = asof of the first BUY fill, `exit_asof` = asof of the closing SELL fill, `qty` = total bought, `entry_price`/`exit_price` = quantity-weighted average buy/sell fill price, `fees` = sum of `FEE` events linked to the trade's orders, `net_pnl` = sum of `realized_pnl` over the trade's SELL fills minus `fees` (fill prices already contain slippage).
- **`exit_reason`** is mapped from the `OrderOrigin` of the **closing** SELL order (looked up by `order_id` in the orders map):

  | `OrderOrigin` | `exit_reason` |
  |---|---|
  | `STRATEGY` | `signal` |
  | `STOP` | `stop` |
  | `REBALANCE` | `rebalance` |
  | `MANUAL` | `manual` (not in ADR table; added because the enum has it; never produced in a backtest) |

  There is no `kill_switch` reason: the kill switch does not auto-liquidate (SPEC-003 §7), so a trade closed afterwards carries the origin of its actual closing order.
- **Open positions at end** are listed from the last snapshot (each has `entry_asof`), never force-closed, and excluded from `trade_count`/`win_rate`.
- **Equity series:** one point per snapshot, index = `pd.Timestamp(asof, tz="UTC")`, values `float(snapshot.equity)`. The first snapshot is `E_0`: no fill can occur on the first bar (signals fill at the next open), so `E_0` equals initial capital.
- **Exposure series:** per snapshot, `sum(position mv over all sleeves) / equity`.
- **Annualisation sampling needs no resampling:** the engine clock already contains every date on which any symbol has a bar, so a run containing crypto is sampled daily and an equity-only run on equity sessions (ADR D6).

### 7. Metrics
Inputs: equity series E_0..E_N (floats converted from the ledger's Decimal at the boundary; `E_0` = initial cash), closed trades and per-bar gross exposure (the equity series, trades and exposure are all derived by `backtest/trades.py` from the ledger and snapshots, section 6b). Let r_i = E_i/E_{i-1} - 1, `P` = periods per year.

**Annualisation `P`.** `P` is the number of observations per year in the equity series being measured. A pure-equity run uses the equity-session calendar -> **252**. A pure-crypto run, or **any run containing a crypto sleeve or symbol (a mixed portfolio)** uses the daily calendar -> **365**: the portfolio curve is sampled on every calendar day, stock equity is carried forward on weekends/holidays (return 0), and sqrt(365) is then the consistent scaler for that series. The benchmark curve is sampled on the same index and the same `P`, so comparisons are like for like. Single-sleeve reports use the sleeve's own class (252 or 365). `P` is a field of the report.

| Metric | Formula |
|---|---|
| total_return | E_N/E_0 - 1 |
| CAGR | (E_N/E_0)^(1/years) - 1, years = (ts_N - ts_0).days / 365.25 (independent of P) |
| max_drawdown | min_i ( E_i / max_{j<=i} E_j - 1 ), stored <= 0; gates compare magnitudes |
| sharpe | mean(r - rf_p) / std(r, ddof=1) * sqrt(P); rf_p = (1+rf)^(1/P) - 1; `rf` defaults 0.0 (config) |
| sortino | mean(r - m) / DD * sqrt(P), DD = sqrt( (1/N) * sum min(r_i - m, 0)^2 ) over **all** N periods (target downside deviation); m = rf_p, default 0 |
| win_rate | count(closed trades with net pnl > 0) / count(closed trades); NaN if none |
| trade_count | number of **closed round trips** (see below) |
| exposure_avg | mean over bars of (sum of position market values / equity) |
| time_in_market | fraction of bars with at least one open position |

Zero-variance guards: Sharpe/Sortino are NaN (not inf) when the denominator is 0. A *round trip* = a symbol's position going 0 -> >0 -> back to 0 within a sleeve; scale-in/trim during it belongs to the same trade; pnl is net of fees and slippage (computed by `trades.py` from the ledger). Positions still open at the end are excluded from `trade_count`/`win_rate` and listed under `open_positions_at_end`. Sharpe approach per Sharpe (1994) "The Sharpe Ratio"; Sortino & Price (1994) for the downside deviation.

**Report** (`BacktestResult.metrics`, `benchmark_metrics` per benchmark, and `deltas`): every report includes the buy-and-hold benchmark over the identical window: `SPY` for equity, `BTC/USD` for crypto; for multi-class portfolios both, plus a static blend (sleeve class weights, cash included) so one portfolio-level comparison exists.

### 8. Validation (maps to STRATEGY.md §7)

**Gate 1, history + hold-out.** `check_history(bars, asset_class)`: >= 10y (equity) / >= 5y (crypto) of calendar span after warmup, measured on the **strategy's calendar** (the union of its universe), not on each symbol; a late-listing symbol such as SOL is allowed and the NaN-means-ineligible rule handles it (ADR-0001 D10). `split_oos(index, oos_frac=0.30)`: cut = index[floor(N*0.70)] on the strategy calendar; in-sample (IS) = before the cut, out-of-sample (OOS) = from the cut. Runs take `start/end` plus implicit warmup bars: the OOS run may *read* IS bars as indicator warmup but **trades only from the cut**. Every tuning/sweep/walk-forward function takes a `DataView` restricted to IS (`oos_locked`): the only function allowed to read the OOS window is `run_oos(frozen_config)`, which takes a config whose `config_hash` was recorded before the call. OOS is evaluated **once** per hash; a second OOS call on a different hash for the same strategy id logs a `peeking` warning in the report (honest-process signal for quality-lead).

**Gate 2, walk-forward** (rolling-window design after Pardo, *The Evaluation and Optimization of Trading Strategies*, 2008): inside the IS region only. `WalkForwardConfig(train_bars, test_bars, step_bars, anchored=False, tune=None)`. Defaults: equities 756 / 252 / 252 bars (3y/1y/1y), crypto 548 / 182 / 182 (1.5y/6m/6m), giving >= 4 test windows on the minimum history. For window k: train = [s_k, s_k+train), test = [s_k+train, s_k+train+test), s_{k+1} = s_k + step. `tune=None` (default for v1) means **fixed parameters evaluated across rolling windows** (consistency check, no optimisation). With `tune=GridSpec({param: [values]}, objective="sharpe")` each train window picks the best grid point, then the chosen params are run on the following test window, and walk-forward efficiency (annualised test return / annualised train return) is reported. Consistency pass (defaults, PM-tunable in `GateConfig`): >= 60% of test windows have Sharpe > 0, and in >= 60% of windows strategy max drawdown <= 1.0x the benchmark's over the same window. Both reported with the per-window table. Each test window is run with warmup from preceding bars.

**Gate 3, OOS vs benchmark.** On the OOS run: `sharpe >= bench.sharpe`, `sortino >= bench.sortino`, `abs(max_dd) <= 1.0 * abs(bench.max_dd)`; benchmark = the strategy's class benchmark over the identical OOS window and `P`.

**Gate 4, trades.** `trade_count >= 30` over the full backtest (IS+OOS, closed round trips); OOS count is reported alongside. (Monthly rotation may fail this by construction on short history; the gate is not relaxed, ADR-0001 D10.)

**Gate 5, +-20% sensitivity.** `sensitivity_sweep(spec, bars_IS, pct=0.20)`: for each parameter p with base value v (one at a time, others fixed): low = floor(0.8*v) and high = ceil(1.2*v) for integer params (clamped to >= 1 and forced to differ from v, so RSI period 2 -> 1 and 3, `max_hold` 5 -> 4 and 6, `top_n` 3 -> 2 and 4), exactly 0.8v and 1.2v for float params. Plus two corner runs (all params low, all params high). Variants violating `constraints` are skipped and listed; so is any variant whose resolved `atr_trailing` period differs from 14 (unsupported by the engine in v1, ADR D5), which means `atr_len` is effectively not swept. A variant **collapses** if `sharpe < 0.5 * base.sharpe` (when base sharpe > 0; if base <= 0 the strategy has already failed gate 3 and the sweep is informational) **or** `abs(max_dd) > 1.5 * abs(base.max_dd)` **or** total_return changes sign. Pass = no collapse. Run on IS so OOS is never touched; the same sweep is cheap to rerun on OOS read-only as an extra report column (flagged "informational").

**Gate 6, forward paper trading.** `forward_stats(equity, benchmark, since)` reuses the same `compute_metrics` over the live equity curve; passes when span >= 90 calendar days and the report is attached to the benchmark comparison. The runner supplies the curve from the persisted ledger.

`evaluate_gate(report: ValidationReport, cfg: GateConfig) -> GateResult` returns a per-check `passed/failed/pending` with the numbers behind it (gate 6 is `pending` until forward data exists). The verdict is "advance" only when all six pass.

Overfitting caveat: the sweep and walk-forward are screens, not proof; the number of strategies/parameter sets tried should be logged in the report so Sharpe can later be deflated (Bailey & Lopez de Prado, "The Deflated Sharpe Ratio", 2014). v1 only records the count.

### Interfaces

**Offered by strategy/backtest (used by runner SPEC-004, API, quality tests)**
```python
# tradebot/strategy/schema.py
class StrategySpec(BaseModel):              # discriminated on `kind`: RotationSpec | RulesSpec
    schema_version: int; id: str; name: str; asset_class: Literal["equity", "crypto"]
    parameters: dict[str, float]; universe: list[str]; schedule: Schedule
    stops: list[StopRequest] = []
    def resolve(self, overrides: Mapping[str, float] | None = None) -> "StrategySpec": ...  # substitute $params; pure
    def required_symbols(self) -> list[str]: ...
def load_strategy(path_or_text: str | Path) -> StrategySpec: ...    # YAML (safe_load) or JSON
def dump_strategy(spec: StrategySpec, fmt: Literal["yaml", "json"] = "yaml") -> str: ...

class StopRequest(BaseModel):
    type: Literal["atr_trailing"]; period: int; mult: float
    anchor: Literal["highest_close_since_entry"]

# tradebot/strategy/signals.py
class HeldPosition(BaseModel):              # built by BarDriver from engine.snapshot()
    symbol: str; entry_ts: pd.Timestamp     # entry fill bar = pd.Timestamp(entry_asof, tz="UTC"); built by BarDriver
class Intent(BaseModel):
    symbol: str; target: Literal["long", "flat"]
    weight: float                           # fraction of SLEEVE capital, hint: execution may scale down, never up
    reason: str                             # e.g. "rank 2/3 score=0.14", "exit: bars_held>=5"
    stop: StopRequest | None = None
class SignalSet(BaseModel):
    sleeve_id: str; asof: pd.Timestamp; decision: bool; intents: list[Intent]
    diagnostics: dict[str, Any] = {}        # scores, eligible set; for dashboards/debugging only

# tradebot/strategy/engine.py
class CompiledStrategy:
    warmup_bars: int
    calendar: pd.DatetimeIndex              # set by prepare()
    def is_decision_bar(self, asof: pd.Timestamp) -> bool: ...
    def prepare(self, bars: Mapping[str, pd.DataFrame]) -> "Features": ...     # causal indicator frames
    def evaluate(self, feats: "Features", asof: pd.Timestamp,
                 held: Mapping[str, HeldPosition], force_decision: bool = False) -> SignalSet: ...
def compile_strategy(spec: StrategySpec, sleeve_id: str | None = None) -> CompiledStrategy: ...

# tradebot/strategy/indicators.py : pure functions, Series in -> Series/DataFrame out
def sma(x, n); ema(x, n); ret(x, n, skip=0); rsi(x, n); macd(x, fast=12, slow=26, signal=9)
def bbands(x, n=20, k=2.0); atr(high, low, close, n); vol(x, n)

# tradebot/backtest/driver.py   (ADR-0001 D3/D4/D5; imports PortfolioEngine, Bar, EngineConfig from SPEC-003)
class TargetBook:
    def __init__(self, sleeve_ids: Sequence[str], state: Mapping[str, Any] | None = None) -> None: ...
    def update(self, signals: SignalSet) -> None: ...                 # replace sleeve's book: weights of `long` intents
    def weights(self, sleeve_id: str) -> dict[str, float]: ...        # copy, positive weights only; {} if no decision yet
    def has_decision(self, sleeve_id: str) -> bool: ...
    def last_decision_ts(self, sleeve_id: str) -> pd.Timestamp | None: ...
    def state(self) -> dict[str, Any]: ...                            # JSON-serialisable; runner persists it per bar

class StepResult(BaseModel):
    asof: date; ts: pd.Timestamp; bar_result: BarResult
    signals: dict[str, SignalSet] = {}                  # sleeves that were on a decision bar
    targets: dict[str, TargetResult] = {}               # sleeves for which submit_target_weights was called

def build_engine_config(base: EngineConfig, sleeves: Sequence[SleeveSpec]) -> EngineConfig: ...  # copies stop mult

class BarDriver:
    def __init__(self, engine: PortfolioEngine, sleeves: Sequence[SleeveSpec],
                 bars: Mapping[str, pd.DataFrame], *, start: date,
                 book: TargetBook | None = None) -> None: ...         # compiles + prepares; precomputes vol20/atr14
    @property
    def book(self) -> TargetBook: ...
    def master_calendar(self, end: date | None = None, after: date | None = None) -> pd.DatetimeIndex: ...
    def step(self, ts: pd.Timestamp) -> StepResult: ...                # ONE bar; what the runner calls for a new bar
    def run(self, end: date | None = None, after: date | None = None) -> Iterator[StepResult]: ...  # backtest + catch-up loop

# tradebot/backtest/trades.py   (pure functions over SPEC-003 LedgerEvent / Order / PortfolioSnapshot)
class ClosedTrade(BaseModel):                # Decimal money; metrics converts to float
    sleeve_id: str; symbol: str; entry_asof: date; exit_asof: date
    qty: Decimal; entry_price: Decimal; exit_price: Decimal; fees: Decimal; net_pnl: Decimal
    exit_reason: Literal["signal", "stop", "rebalance", "manual"]
class OpenPosition(BaseModel): sleeve_id: str; symbol: str; qty: Decimal; entry_asof: date; mv: Decimal
def closed_trades(ledger: Sequence[LedgerEvent], orders: Mapping[str, Order]) -> list[ClosedTrade]: ...
def open_positions(snapshot: PortfolioSnapshot) -> list[OpenPosition]: ...
def equity_series(snapshots: Sequence[PortfolioSnapshot]) -> pd.Series: ...      # float, UTC DatetimeIndex
def exposure_series(snapshots: Sequence[PortfolioSnapshot]) -> pd.Series: ...    # gross position mv / equity
def exit_reason_for(origin: OrderOrigin) -> str: ...

# tradebot/backtest/engine.py
class SleeveSpec(BaseModel): sleeve_id: str; strategy: StrategySpec | None   # None = cash; weight lives in SleeveConfig.target_weight
class BacktestConfig(BaseModel):
    sleeves: list[SleeveSpec]; start: date; end: date
    engine: EngineConfig                   # SPEC-003: initial_capital, sleeves (SleeveConfig), risk, costs
    rf_annual: float = 0.0
class BacktestResult(BaseModel):
    config_hash: str; data_fingerprint: str; periods_per_year: int
    equity: pd.Series; exposure: pd.Series; trades: list[ClosedTrade]; open_positions: list[OpenPosition]
    killed_on: date | None; ledger: list[LedgerEvent]; n_signals: int
def run_backtest(cfg: BacktestConfig, bars: Mapping[str, pd.DataFrame]) -> BacktestResult: ...
def run_with_benchmark(cfg, bars) -> BacktestReport: ...          # strategy + buy-and-hold, same window

# tradebot/backtest/metrics.py
def compute_metrics(equity: pd.Series, trades: Sequence[ClosedTrade], exposure: pd.Series,
                    periods_per_year: int, rf_annual: float = 0.0) -> Metrics: ...
def periods_per_year(symbols_or_classes: Iterable[str]) -> int: ...      # 365 if any crypto else 252

# tradebot/backtest/validation.py
def split_oos(index: pd.DatetimeIndex, oos_frac: float = 0.30) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]: ...
def walk_forward(cfg, bars, wf: WalkForwardConfig) -> WalkForwardResult: ...
def sensitivity_sweep(cfg, bars, pct: float = 0.20) -> SensitivityResult: ...
def run_oos(frozen_cfg, bars) -> BacktestReport: ...
def evaluate_gate(report: ValidationReport, cfg: GateConfig = GateConfig()) -> GateResult: ...
def forward_stats(equity: pd.Series, benchmark: pd.Series, since: date) -> ForwardStats: ...
```

**Interfaces needed from data (SPEC-001; ADR D1, D2)**
```python
class MarketData:   # SPEC-001; the backtester uses cache-only reads
    def get_bars(self, symbol: str, start: date, end: date, *, refresh: bool = True, ...) -> pd.DataFrame: ...
    def get_bars_many(self, symbols: Sequence[str], start: date, end: date, **kw) -> dict[str, pd.DataFrame]: ...
def asset_class(symbol: str) -> AssetClass: ...      # SPEC-001 calendar module
def bar_close_time(symbol: str, d: date) -> datetime: ...
```
Bar contract (the backtester `validate(bars)` asserts it): index = tz-aware UTC `DatetimeIndex`, strictly increasing, unique, **label = bar open date at 00:00 UTC** (equities: session date; crypto: UTC day); columns exactly `open, high, low, close, volume`, float64, no NaN, `low <= open,close <= high`; **fully adjusted** (ADR D2): the factor `Adj Close / Close`, dividends included, is applied to open, high, low and close alike, so SPY benchmark returns are total-return; **only completed bars** (the in-progress current day is never returned, which is what makes the live runner safe). No forward-fill of missing sessions. `start` is inclusive and may precede the strategy start by the warmup; the caller asks for warmup bars itself.

**Interfaces needed from execution (SPEC-003; canonical, ADR D3).** `ExecutionPort` and `make_execution` are **dropped**. 002 calls only:
```python
PortfolioEngine(config: EngineConfig, state: EngineState | None = None)
engine.process_bar(asof: date, bars: Mapping[str, Bar]) -> BarResult        # steps A-F, one call
engine.submit_target_weights(sleeve_id: str, weights: Mapping[str, float], asof: date) -> TargetResult
engine.snapshot() -> PortfolioSnapshot; engine.export_state() -> EngineState; engine.ledger
Bar(symbol, open, high, low, close, vol20=None, atr14=None)                 # vol20/atr14 attached by BarDriver
SleeveConfig.trailing_stop_atr_mult: Decimal | None                         # set by build_engine_config
PositionSnapshot.entry_asof: date | None                                    # NEW in SPEC-003 (set on fill from flat, cleared at flat)
OrderOrigin {STRATEGY, STOP, REBALANCE, MANUAL}; LedgerEvent; Order
```
Requests and guarantees:
- (a) The `BarDriver` loop in section 6 is the only per-bar loop; the runner (SPEC-004) calls the same `BarDriver`. 002 never calls `reset_kill_switch`.
- (b) `PortfolioEngine` is constructed fresh per run with no global state, so sweeps are independent and deterministic.
- (c) SPEC-003 never reimplements ATR or vol; it only receives `vol20`/`atr14` on `Bar`.
- (d) `process_bar` raises `StaleBarError` for `asof <= last_processed_asof`; the driver relies on it for runner restart safety.
- (e) The position snapshot carries `entry_asof`; the ledger `FILL`/`FEE` events carry `order_id`, `symbol`, `qty`, `price`, `realized_pnl` as in SPEC-003 section 9, and `export_state().orders` contains every order including terminal ones (the source of the `order_id -> Order` map for `trades.py`).
- `ClosedTrade` is now owned by 002 (`backtest/trades.py`), derived purely from ledger and snapshots.

### Invariants / rules
- Indicators at t use only bars <= t. A signal at t becomes an order that fills at t+1's open of that symbol's own calendar. Within `BarDriver.step`, `process_bar` precedes `submit_target_weights`.
- `asof: date = ts.date()` is computed only in `BarDriver`; strategy and data code use tz-aware `pd.Timestamp`.
- `submit_target_weights` is called only on a sleeve's decision bars and on `rebalance_planned` bars.
- `evaluate` and `prepare` have no I/O, no clock, no randomness; same inputs give identical outputs (hash-stable).
- Any NaN operand makes a comparison False. A symbol with insufficient history can neither be selected nor entered.
- Strategy parameters are only changed through `StrategySpec.resolve`; the original spec is never mutated.
- OOS data is never visible to tuning, sweep, or walk-forward functions.
- Every report includes the benchmark, `P`, `config_hash`, `data_fingerprint`.

## Acceptance criteria
1. The three YAML sleeves load, validate, and re-dump to a semantically equal spec (round trip YAML->model->JSON->model equal); malformed specs (mixed asset class, unknown indicator, undefined `$param`, `pos` in a rotation spec, `constraints` violated) raise `ValidationError`.
2. Each indicator matches hand-computed reference vectors to 1e-9 (SMA, EMA with SMA seed, RSI Wilder incl. all-up and flat edge cases, MACD, Bollinger ddof=0, ATR, ret, vol) and is NaN before its first valid bar.
3. **Truncation invariance:** for random bar data, for every t, `evaluate` on features prepared from `bars[:t+1]` equals `evaluate` at t on features prepared from the full series (exact for SMA/ret/Bollinger; <=1e-9 for the recursive ones given identical start). Appending a bar never changes earlier-bar signals.
4. Rotation: on a synthetic panel the top-3 by 126-bar return among symbols above their 200-bar SMA are selected; ties break by symbol; fewer than 3 eligible gives the remainder to IEF with the right weight; `filter_mode: post_rank` differs as specified; non-decision bars emit no intents; held-but-unselected symbols come out `flat`.
5. Rules: RSI(2)<10 & close>SMA200 enters; exit on RSI>70 or `bars_held>=5` (exit signalled at the close of the 5th bar after the fill bar, fills next open); hold-while sleeve exits exactly when entry becomes false; exits beat entries on the same bar.
6. In a backtest on a fixture, no fill timestamp is <= its signal timestamp; the first trade can fill no earlier than bar `warmup_bars + 1`. Shifting all prices forward one bar degrades a deliberately-lookahead dummy rule and leaves the real rules causal (hook for SPEC-005's bias audit).
7. Metrics equal hand-calculated values on a small equity series (total return, CAGR with 365.25 years, max drawdown, Sharpe, Sortino with all-N downside deviation, win rate, exposure, trade count). Pure-equity uses P=252, any run with a crypto symbol uses P=365 with weekend carry-forward.
8. Two runs with identical config and data give byte-identical JSON reports (golden test); `config_hash` changes when any resolved parameter changes.
9. Benchmark for every report goes through the same execution engine and costs; equity runs compare with SPY, crypto with BTC/USD.
10. `split_oos` gives a 70/30 split; walk-forward windows match the layout in section 8 and never extend into OOS; `run_oos` on a second config hash is flagged.
11. Sensitivity produces floor/ceil variants for integer params and 0.8v/1.2v for floats, skips `constraints` violations, and flags a deliberately fragile strategy as collapsed.
12. `evaluate_gate` returns the six checks with `pending` for the forward check when no live data exists; each threshold is configurable in `GateConfig`.
13. The three sleeves run end to end on the synthetic fixture dataset (ADR-0001 D9) through `BarDriver` and the real `PortfolioEngine`; the results are stored as golden files.
14. **BarDriver:** (a) `vol20`/`atr14` on each `Bar` equal `indicators.vol(close,20)` / `indicators.atr(h,l,c,14)` at that bar (NaN -> `None`); (b) `ts.date()` is the `asof` passed to the engine; (c) `submit_target_weights` is called for a sleeve only on its decision bars and on every strategy sleeve on a `rebalance_planned` bar, never otherwise, and never for the cash sleeve; (d) `held` equals the snapshot positions with `entry_asof` converted to a UTC Timestamp; (e) `build_engine_config` copies the stop `mult` into `trailing_stop_atr_mult` and rejects a stop period != 14; (f) driving bars one at a time with `step` (history truncated at each bar) gives a ledger byte-identical to `run` over full history.
15. **TargetBook:** the book is replaced on each decision bar by the `long` intents' weights; an absent symbol has target 0; a held symbol that drops out of the book is sold; a monthly sleeve produces no orders on non-decision, non-rebalance bars; `state()` round-trips through JSON, and a driver restored from it behaves identically to an uninterrupted one.
16. **trades.py:** on hand-built ledgers, closed trades, `net_pnl` (realized minus fees), average prices and `exit_reason` (STRATEGY->signal, STOP->stop, REBALANCE->rebalance, MANUAL->manual) are exact; scale-in and trim stay inside one round trip; a position open at the end is listed in `open_positions`, not in trades; equity and exposure series equal hand-calculated values.
17. **Stop trigger:** a position whose close equals the stop level exits (`close <= stop_level`), verified through the engine with the driver's `atr14`.

## Tasks
Every task carries its **dispatch wave** (ADR-0001 §3). File lists are exact and do not overlap between any two tasks. Tasks in a later wave may rely on every earlier wave being merged. Within Wave 4 the listed `Depends on` order applies (driver and trades first, then T4, then T5, then T6, tests last). No task edits `pyproject.toml`, `.github/` or `src/tradebot/cli.py` (platform-owned, ADR D11); required packages are listed once under "New runtime dependencies" for the platform Wave-1 task.

Dispatch summary: **Wave 1** T2, T7a. **Wave 2** T1. **Wave 3** T3. **Wave 4** T11 (driver + TargetBook), T12 (trades), T4, T5, T6, T7b, T8, T9, T10, T13, T14.

### Reference vectors for the indicator tests (normative; used by T2 and T7a)
All inputs float64; tolerance 1e-9 absolute; `nan` = NaN. Index is 0-based position.

| # | Call | Input | Expected |
|---|---|---|---|
| V1 | `sma(x, 3)` | `x=[10,12,11,13,14,12,15]` | `[nan, nan, 11, 12, 38/3, 13, 41/3]` |
| V2 | `ema(x, 3)` (alpha=0.5, seed = SMA at idx 2) | same `x` | `[nan, nan, 11, 12, 13, 12.5, 13.75]` |
| V3 | `ret(x, 2)` | `x=[100,110,121,121]` | `[nan, nan, 0.21, 0.10]` |
| V3b | `ret(x, 1, skip=1)` | same | `[nan, nan, 0.10, 0.10]` |
| V4 | `rsi(x, 2)` | `x=[10,11,10,12,11,13]` | `[nan, nan, 50, 250/3, 50, 1050/13]` (idx 3: AU=1.25, AD=0.25, RS=5) |
| V4b | `rsi(x, 2)` all-up `[1,2,3,4]` / flat `[5,5,5,5]` / all-down `[4,3,2,1]` | | idx 2,3: `100,100` / `50,50` / `0,0`; idx 0,1 nan |
| V5 | `macd(x, fast=2, slow=3, signal=2)` | `x=[10,12,11,13,14,12,15]` | `macd = [nan, nan, 0, 1/3, 4/9, -1/54, 133/324]`; `signal = [nan, nan, nan, 1/6, 19/54, 17/162, 25/81]`; `hist = [nan, nan, nan, 1/6, 5/54, -10/81, 11/108]`. First valid macd = slow-1 = 2; first valid signal/hist = slow+signal-2 = 3 |
| V6 | `bbands(x, n=8, k=2)` | `x=[2,4,4,4,5,5,7,9]` | idx 0..6 all nan; idx 7: `mid=5, upper=9, lower=1, pctb=1.0, bandwidth=1.6` (population std = 2) |
| V6b | `bbands(x, 3, 2)` flat `[5,5,5,5]` | | idx 2,3: `mid=5, upper=5, lower=5, pctb=nan, bandwidth=0` |
| V7 | `atr(h, l, c, 2)` | `h=[10,12,13,12,14]`, `l=[8,9,10,9,11]`, `c=[9,11,12,10,13]` | `TR=[nan,3,3,3,4]` so `atr=[nan, nan, 3, 3, 3.5]` |
| V8 | `vol(x, 2)` | `x=[100,110,99,99]` (returns 0.1, -0.1, 0.0) | `[nan, nan, sqrt(0.02)=0.14142135623730953, sqrt(0.005)=0.07071067811865477]` |

### T2 [haiku] Indicator library: **Wave 1**
- **Files:** `src/tradebot/strategy/__init__.py` (docstring only: no imports, no re-exports), `src/tradebot/strategy/indicators.py`
- **Depends on:** none. Needs only `pandas` and `numpy` (installed by the platform Wave-1 task). Assumes `src/tradebot/` is an importable package (platform tooling task); do not create `src/tradebot/__init__.py`.
- **Exact signatures** (all pure; inputs are `pd.Series` of float64 with any index; every output has the **same index and length** as the input, dtype float64, `name=None`; DataFrames have the listed columns in the listed order):
  ```python
  def sma(x: pd.Series, n: int) -> pd.Series: ...
  def ema(x: pd.Series, n: int) -> pd.Series: ...
  def ret(x: pd.Series, n: int, skip: int = 0) -> pd.Series: ...
  def rsi(x: pd.Series, n: int) -> pd.Series: ...
  def macd(x: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame: ...   # columns: macd, signal, hist
  def bbands(x: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame: ...   # columns: mid, upper, lower, pctb, bandwidth
  def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series: ...
  def vol(x: pd.Series, n: int) -> pd.Series: ...
  ```
- **Formulas** (t = 0-based position; "first valid" = first non-NaN position; everything before it is NaN; if `len(x)` is smaller than the first valid position + 1 the output is all NaN; an empty input returns an empty Series):
  - `sma(n)`: `SMA_t = (1/n) * sum_{i=0}^{n-1} x_{t-i}`. First valid `n-1`.
  - `ema(n)`: `alpha = 2/(n+1)`. Let `f` be the position of the first non-NaN value of `x`. Seed `EMA_{f+n-1} = mean(x[f : f+n])`; for later t `EMA_t = alpha*x_t + (1-alpha)*EMA_{t-1}`. NaN before the seed. A NaN after position `f` raises `ValueError`. (Allowing leading NaNs is what lets `macd` run `ema` on the macd line.) First valid `f+n-1`.
  - `ret(n, skip=0)`: `x_{t-skip} / x_{t-skip-n} - 1`. First valid `n+skip`.
  - `rsi(n)` Wilder: `d_t = x_t - x_{t-1}` (t>=1); `U_t = max(d_t,0)`; `D_t = max(-d_t,0)`; `AU_n = mean(U_1..U_n)`, `AD_n = mean(D_1..D_n)`; for t>n `AU_t = ((n-1)*AU_{t-1} + U_t)/n`, same for `AD`. `RS = AU/AD`; `RSI = 100 - 100/(1+RS)`. If `AD == 0`: `RSI = 100` when `AU > 0`, `50` when `AU == 0`. First valid `n`. A NaN in `x` raises `ValueError`.
  - `macd(fast, slow, signal)`: `macd_t = ema(x, fast) - ema(x, slow)`; `signal_t = ema(macd, signal)` (SMA-seeded over the first `signal` valid macd values); `hist = macd - signal`. Requires `fast < slow` else `ValueError`. First valid macd `slow-1`; signal and hist `slow+signal-2`.
  - `bbands(n, k)`: `mid = SMA(n)`; `sd` = **population** standard deviation (ddof=0) of the last n values; `upper = mid + k*sd`; `lower = mid - k*sd`; `pctb = (x - lower)/(upper - lower)` (NaN where `upper == lower`); `bandwidth = (upper - lower)/mid` (NaN where `mid == 0`). First valid `n-1`.
  - `atr(n)` Wilder: `TR_t = max(h_t - l_t, |h_t - c_{t-1}|, |l_t - c_{t-1}|)` for t>=1 (`TR_0` is undefined/NaN); `ATR_n = mean(TR_1..TR_n)`; for t>n `ATR_t = ((n-1)*ATR_{t-1} + TR_t)/n`. First valid `n`. The three inputs must have equal length and index else `ValueError`.
  - `vol(n)`: `r_t = x_t/x_{t-1} - 1`; the sample standard deviation (ddof=1) of the last n returns `r_{t-n+1..t}`; not annualised. First valid `n`.
  - Every function raises `ValueError` for `n < 1` (and `skip < 0`). No function reads anything after position t to compute position t (causal). No TA library, no wall clock, no randomness.
- **Done when:** the reference vectors V1-V8 above hold to 1e-9 (T7a asserts them), outputs are NaN exactly before the stated first-valid positions, and `ruff check` and `ruff format --check` pass.

### T7a [haiku] Tests: indicators: **Wave 1**
- **Files:** `tests/strategy/test_indicators.py`
- **Depends on:** none to write (it needs only the T2 signatures and the vectors above); it passes once T2 is merged, so run it with T2.
- **Do:** one test per vector V1-V8 (`np.testing.assert_allclose(..., atol=1e-9, equal_nan=True)` and an explicit check that NaN positions match); error cases (`n=0`, `macd` with `fast >= slow`, NaN inside `rsi`/`ema` input after the first valid value, mismatched `atr` inputs); output index equals input index; **truncation invariance**: for a seeded `np.random.default_rng(0)` random-walk series of length 300 and every indicator, the value at position t computed from `x[:t+1]` equals the value at t computed from the full series (`atol=1e-9`) for t in a sample of positions including the first-valid position. No network, no files, no hypothesis.
- **Done when:** AC 2 holds once T2 is merged; ruff clean.

### T1 [sonnet] Strategy schema, parameter resolution, IO: **Wave 2**
- **Files:** `src/tradebot/strategy/schema.py`, `src/tradebot/strategy/io.py`, `strategies/core_momentum.yaml`, `strategies/mean_reversion.yaml`, `strategies/crypto_trend.yaml`
- **Depends on:** none (T2 created the package `__init__.py`; T1 must not edit it)
- **Do:** pydantic v2 models for operands, conditions, `RotationSpec`, `RulesSpec`, `StopRequest`, `Schedule`; `$param` references with substitution in `resolve`; validators (one asset class, params defined, `pos` only in rules, constraints, **`atr_trailing` period must resolve to 14 and at most one stop per strategy, ADR D5**); `warmup_bars` calculation; YAML (`yaml.safe_load`) and JSON load/dump (`load_strategy`, `dump_strategy` in `io.py`, re-exported by `schema.py` only if needed to avoid a cycle). Copy the three YAML sleeves from this spec exactly.
- **Done when:** AC 1.

### T3 [sonnet] Signal evaluation engine: **Wave 3**
- **Files:** `src/tradebot/strategy/signals.py`, `src/tradebot/strategy/engine.py`
- **Depends on:** T1, T2
- **Do:** `compile_strategy`, `prepare` (per-symbol causal feature frames, de-duplicated indicator computation, NaN->False comparison semantics), `is_decision_bar`, rotation and rules evaluation exactly as section 2a with the ADR D10 semantics (`pre_rank`, unconditional IEF fallback), `Intent`/`SignalSet`/`HeldPosition`, `bars_held` derived from `HeldPosition.entry_ts` and the strategy calendar, stop requests copied onto long intents, deterministic ordering. `evaluate` never computes `vol20`/`atr14` (the driver does).
- **Done when:** AC 3, 4, 5.

### T11 [sonnet] `BarDriver`, `TargetBook`, engine-config builder: **Wave 4**
- **Files:** `src/tradebot/backtest/driver.py`, `src/tradebot/backtest/__init__.py` (docstring only)
- **Depends on:** T3, T2, SPEC-003 `PortfolioEngine`/`Bar`/`EngineConfig` (SPEC-003 T10) and `entry_asof`
- **Do:** section 6 exactly: `TargetBook`, `StepResult`, `build_engine_config`, `BarDriver` with the signatures under "Interfaces", the precomputed `vol20`/`atr14` (via `indicators.vol` / `indicators.atr`), `held` from `engine.snapshot()`, submit only on decision bars or `rebalance_planned`, `ts.date()` conversion only here, `step` for one bar and `run(end, after)` for the loop/catch-up. `TargetBook.state()` / `state=` constructor argument round-trip through JSON.
- **Done when:** AC 14, 15 (and AC 6 together with T5).

### T12 [sonnet] Trades, equity and exposure from the ledger: **Wave 4**
- **Files:** `src/tradebot/backtest/trades.py`
- **Depends on:** SPEC-003 `LedgerEvent`, `Order`, `OrderOrigin`, `PortfolioSnapshot` (SPEC-003 T3, T5, T10)
- **Do:** section 6b: `ClosedTrade`, `OpenPosition`, `closed_trades`, `open_positions`, `equity_series`, `exposure_series`, `exit_reason_for` (STRATEGY -> signal, STOP -> stop, REBALANCE -> rebalance, MANUAL -> manual). Pure functions; no I/O; Decimal math until the series conversion.
- **Done when:** AC 16.

### T4 [haiku] Metrics: **Wave 4**
- **Files:** `src/tradebot/backtest/metrics.py`
- **Depends on:** T12 (imports `ClosedTrade`)
- **Do:** section 7 formulas; `periods_per_year`; zero-variance and empty-trades guards; `Metrics` pydantic model.
- **Done when:** AC 7.

### T5 [sonnet] Backtest engine and report: **Wave 4**
- **Files:** `src/tradebot/backtest/engine.py`, `src/tradebot/backtest/calendar.py`, `src/tradebot/backtest/report.py`
- **Depends on:** T11, T12, T4, SPEC-001 `MarketData` types
- **Do:** `BacktestConfig`, `SleeveSpec`, `BacktestResult`, bar-contract `validate`, the `run_backtest` of section 6 on top of `BarDriver` (no per-bar logic of its own), benchmark generation and `run_with_benchmark`, `config_hash` and `data_fingerprint`, canonical JSON report.
- **Done when:** AC 6, 8, 9.

### T6 [sonnet] OOS split, walk-forward, sensitivity, gate: **Wave 4**
- **Files:** `src/tradebot/backtest/validation.py`, `src/tradebot/backtest/gate.py`
- **Depends on:** T5
- **Do:** section 8 in full: `split_oos` and the `oos_locked` data view, `check_history` (on the strategy calendar), `walk_forward` (fixed-param and grid-tuned), `sensitivity_sweep` (floor/ceil rule, corners, constraints, unsupported-ATR-period skip, collapse test), `run_oos` peeking flag, `evaluate_gate`, `forward_stats`.
- **Done when:** AC 10, 11, 12.

### T7b [haiku] Tests: schema: **Wave 4**
- **Files:** `tests/strategy/test_schema.py`
- **Depends on:** T1
- **Do:** AC 1, plus the ATR period != 14 and two-stops rejections.
- **Done when:** pass offline.

### T8 [haiku] Tests: metrics and report determinism: **Wave 4**
- **Files:** `tests/backtest/test_metrics.py`, `tests/backtest/test_report_golden.py`
- **Depends on:** T4, T5
- **Do:** AC 7 and 8 with a small hand-built equity series and a stub engine result.
- **Done when:** pass offline.

### T9 [sonnet] Tests: no-lookahead and signal tests: **Wave 4**
- **Files:** `tests/strategy/test_no_lookahead.py`, `tests/strategy/test_signals.py`
- **Depends on:** T3
- **Do:** AC 3 (property/randomised truncation invariance), AC 4 and 5 on synthetic panels. Property tests go to engineer per routing rule.
- **Done when:** pass offline.

### T10 [haiku] Tests: validation tools and sleeves end to end: **Wave 4**
- **Files:** `tests/backtest/test_validation.py`, `tests/backtest/test_sleeves_golden.py`
- **Depends on:** T6, SPEC-001 synthetic fixtures (ADR D9), SPEC-003 `PortfolioEngine`
- **Do:** AC 6, 10-13 and 17 using the synthetic fixture dataset; write goldens under `tests/golden/` (golden updates need the PM-approved note, SPEC-005 section 5).
- **Done when:** pass offline.

### T13 [sonnet] Tests: `BarDriver` and `TargetBook`: **Wave 4**
- **Files:** `tests/backtest/test_driver.py`
- **Depends on:** T11
- **Do:** AC 14 and 15 with a stub engine recording calls and on the synthetic fixtures with the real engine.
- **Done when:** pass offline.

### T14 [haiku] Tests: trades, equity and exposure derivation: **Wave 4**
- **Files:** `tests/backtest/test_trades.py`
- **Depends on:** T12
- **Do:** AC 16 on hand-built ledgers and snapshots (scale-in/trim round trip, STOP/REBALANCE/STRATEGY exit mapping, open position at end, fees and net pnl).
- **Done when:** pass offline.

## New runtime dependencies
All dependencies are applied by platform-lead's single Wave-1 tooling task (ADR D11). **Nothing in this spec edits `pyproject.toml`**; this table is the one place 002 states what it needs.

| Package | Scope | Reason |
|---|---|---|
| `pandas`, `numpy` | runtime | already in stack; all indicators and metrics are short vectorised formulas, and we need exact, documented conventions (SMA seed, Wilder, ddof) |
| `pydantic` (v2) | runtime | already in stack; schema, validation, JSON |
| `pyyaml` | runtime | the one addition: YAML load/dump of strategy files, via `yaml.safe_load` only (never `load`) |
| `hypothesis` | dev | property tests in T9 (SPEC-005 owns profiles) |

Rejected: TA-Lib (C build/install friction; its conventions differ in warmup and seeding), pandas-ta / `ta` (less maintained, hidden defaults that would make golden numbers depend on a third party), vectorbt/backtrader/zipline (their own fill and data models would conflict with our simulator and the t->t+1 rule, and execution is a separate lead's code).

## Open questions
Resolved by ADR-0001 D10 (recorded here, questions removed):
- **Rotation filter order:** `filter_mode: pre_rank` (200-day SMA filter first, then rank). `post_rank` stays available in the schema for experiments only.
- **IEF fallback:** unconditional (no `fallback.require` in the shipped sleeve).
- **Trade-count gate:** >= 30 closed round trips over the **full** history; the OOS count is reported alongside.
- **Data-history gate:** measured on the **strategy's calendar** (the union of its universe), not per symbol; SOL's shorter history is allowed and the NaN-means-ineligible rule handles it.
- **Gate thresholds:** walk-forward >= 60% of windows with Sharpe > 0 and drawdown <= benchmark; sensitivity collapse = Sharpe < 0.5x base, or drawdown > 1.5x base, or the return flips sign. Defaults stay PM-tunable through `GateConfig`.
- **Risk-free rate:** 0%.
- **Mixed-portfolio report:** SPY, BTC and a static blend.
- **ATR-stop semantics and cooldown:** close-based `close <= stop_level`, exit at next open (D5); re-entry lock until the strategy has gone flat once (D3).
- **Adjusted prices:** fully adjusted OHLC (D2), dividends included, forward chain-linking accepted.

Still open:
1. **Crypto lookback units:** 126 bars means 6 months for stocks but about 4 for crypto; sleeves pick their own numbers, but the future builder must be taught this.

## Risks
- **Backtest != live drift** from recursive indicators if the runner's history window differs from the backtest's (mitigation: fixed `history_start`, equality test).
- **Overfitting by repetition:** many sweeps on IS inflate the best result; we only record the count in v1.
- **Weekend carry-forward** makes mixed-portfolio volatility look lower in equity terms; the convention is stated and applied identically to the benchmark.
- **Interface drift with SPEC-003** (`PortfolioEngine`, `Bar`, `entry_asof`, ledger fields used by `trades.py`) is the main integration risk; SPEC-003 is canonical for those signatures (ADR-0001 D3).
- **`TargetBook` persistence:** the book is state. If the runner restarts without restoring it, the next `rebalance_planned` bar would submit empty weights and liquidate. SPEC-004's per-bar checkpoint must store `TargetBook.state()` and pass it back to `BarDriver` (flagged to the PM).
- **Fixed ATR period:** v1 supports ATR(14) only (ADR D5), so `atr_len` is not swept; the sensitivity sweep skips and lists any variant whose resolved `atr_trailing` period is not 14.
- **Short crypto history** (SOL/USD since 2020) limits walk-forward windows; the 5-year crypto gate may force fewer, shorter test windows.
- **Schema growth:** every new indicator or operator touches schema, engine and sweep; keep the operand list closed and versioned (`schema_version`).
