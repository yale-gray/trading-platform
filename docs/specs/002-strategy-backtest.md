# SPEC-002: Strategy engine and backtester

- **Lead:** strategy-lead
- **Status:** approved with amendments (see ADR-0001 §4; ADR wins on conflict)
- **Roadmap item:** Phase 2 (strategy schema, indicators, backtester, metrics, OOS/walk-forward/sensitivity, 3 sleeves encoded)
- **Related ADRs/specs:** SPEC-001 (market data), SPEC-003 (execution/sim/risk/portfolio), SPEC-004 (platform/runner), SPEC-005 (quality); feeds ADR-0001. Source of rules: `docs/STRATEGY.md` §4, §7.

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
bars (data layer) --> compile(spec) --> prepare(bars) --> evaluate(features, asof, held) --> SignalSet --> execution
                      (pure)            (vectorised,      (pure, reads row `asof` only)
                                         causal)
```
- `evaluate` is **pure and stateless**: no hidden memory. Anything that depends on history of the position (bars held) is derived from the `held` argument that the portfolio supplies. That is what lets the platform runner restart safely and call the *same* function the backtester calls. There is no separate live code path.
- The backtester calls `prepare` once on full history (fast) then `evaluate` per bar. The live runner calls `prepare` on bars up to the latest completed bar then `evaluate` at the last row. Correctness depends on every indicator being **causal** (value at t uses bars <= t). Enforced by the truncation-invariance test (AC 3).
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
1. `eligible = {s in universe : eligibility(s) is True}` (default filter-then-rank, `filter_mode: pre_rank`). `filter_mode: post_rank` ranks everything first, takes the top k, then drops the ineligible (leaving more empty slots). STRATEGY.md §4 is ambiguous; see Open questions.
2. `score(s) = rank.by` evaluated for each eligible symbol; sort by (score desc, symbol asc). The symbol tiebreak keeps results deterministic. NaN scores are dropped.
3. `selected = first select.top` symbols (fewer if fewer eligible).
4. Empty slots go to `fallback.symbol` if given (and its optional `require` condition holds), else stay as cash (no intent, weight is simply unallocated).

Rules evaluation per symbol uses the same condition language; `exit` omitted means **hold-while**: exit = `not entry` (used by the crypto trend sleeve). Exits take priority: if a held symbol's exit is true it is flat even if entry is also true. A symbol held and flagged by execution's stop appears in `held` as absent on the next bar; the strategy may then re-enter if `entry` is still true (execution can enforce a cooldown, its call).

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

### 5. How a strategy requests stops (owned by execution)
A strategy never stops anything. It *declares* `stops: [StopRequest]` in the spec, and `evaluate` copies the request onto each `long` `Intent` it emits. Execution owns the stop level, tracking and exit.

```
StopRequest: type "atr_trailing"; period (ATR bars); mult; anchor "highest_close_since_entry"
```
Proposed semantics for execution to confirm (daily bars, no intraday path): after the close of bar t, `level_t = highest_close_since_entry - mult * ATR_t(period)`; if `close_t < level_t` execution queues an exit that fills at the open of t+1 (the same no-lookahead rule as every signal). Execution calls `tradebot.strategy.indicators.atr` so there is one ATR implementation. A stop exit shows up in `held` as the position disappearing and as `exit_reason="stop"` on the closed trade.

### 6. Backtest loop

Calendar: master calendar = sorted union of all required symbols' timestamps within `[start, end]`. A symbol with no bar on a date (e.g. SPY on Saturday) is skipped for fills, carried forward at its last close for marking, and never produces a decision (its strategy's calendar excludes that date).

```
def run_backtest(cfg: BacktestConfig, bars: dict[str, DataFrame]) -> BacktestResult:
    validate(bars)                                   # contract in section "Interfaces needed from data"
    compiled = {sl.id: compile(sl.strategy) for sl in cfg.sleeves}
    assert every sleeve has >= warmup_bars of history before cfg.start
    feats    = {sl.id: compiled[sl.id].prepare(bars) for sl in cfg.sleeves}   # causal, vectorised
    ex       = make_execution(cfg.execution, cfg.sleeves, cfg.initial_cash)   # SPEC-003
    first    = {sl.id: True for sl in cfg.sleeves}
    for ts in master_calendar(bars, cfg.start, cfg.end):
        today = {s: df.loc[ts] for s, df in bars.items() if ts in df.index}
        # 1) OPEN: orders queued at the previous close fill at this bar's open
        ex.on_open(ts, {s: r.open for s, r in today.items()})
        # 2) CLOSE: mark to market, update stops (may queue stop exits), sleeve rebalance, kill switch
        ex.on_close(ts, {s: r for s, r in today.items()})        # returns/records EquitySnapshot
        # 3) SIGNALS: use only data <= ts; results queue for the NEXT open
        for sl in cfg.sleeves:
            c = compiled[sl.id]
            if ts in c.calendar and (c.is_decision_bar(ts) or first[sl.id]) and ts >= warmup_ok[sl.id]:
                sigs = c.evaluate(feats[sl.id], ts, ex.held(sl.id), force_decision=first[sl.id])
                ex.on_signals(ts, sl.id, sigs)                   # sizing + risk happen inside execution
                first[sl.id] = False; log(sigs)
    # orders queued after the final bar never fill; open positions are reported, not force-closed
    return BacktestResult(equity=ex.equity_series(), trades=ex.closed_trades(), ...)
```
Step order matters: fills (1) happen before the bar's signals (3), so a signal made at t can never fill at t. The benchmark is a second `run_backtest` with the generated buy-and-hold sleeve, same `execution` config and same window.

Determinism: no randomness, no wall clock in results, symbols iterated in sorted order, ties broken by symbol. `BacktestResult` carries `config_hash` (sha256 of canonical JSON of resolved config) and `data_fingerprint` (sha256 of the bar arrays) so golden tests pin both inputs.

### 7. Metrics
Inputs: equity series E_0..E_N (floats converted from the ledger's Decimal at the boundary; `E_0` = initial cash), closed trades, per-bar gross exposure. Let r_i = E_i/E_{i-1} - 1, `P` = periods per year.

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

Zero-variance guards: Sharpe/Sortino are NaN (not inf) when the denominator is 0. A *round trip* = a symbol's position going 0 -> >0 -> back to 0 within a sleeve; scale-in/trim during it belongs to the same trade; pnl is net of fees and slippage (execution reports it). Positions still open at the end are excluded from `trade_count`/`win_rate` and listed under `open_positions_at_end`. Sharpe approach per Sharpe (1994) "The Sharpe Ratio"; Sortino & Price (1994) for the downside deviation.

**Report** (`BacktestResult.metrics`, `benchmark_metrics` per benchmark, and `deltas`): every report includes the buy-and-hold benchmark over the identical window: `SPY` for equity, `BTC/USD` for crypto; for multi-class portfolios both, plus a static blend (sleeve class weights, cash included) so one portfolio-level comparison exists.

### 8. Validation (maps to STRATEGY.md §7)

**Gate 1, history + hold-out.** `check_history(bars, asset_class)`: >= 10y (equity) / >= 5y (crypto) of calendar span after warmup. `split_oos(index, oos_frac=0.30)`: cut = index[floor(N*0.70)] on the strategy calendar; in-sample (IS) = before the cut, out-of-sample (OOS) = from the cut. Runs take `start/end` plus implicit warmup bars: the OOS run may *read* IS bars as indicator warmup but **trades only from the cut**. Every tuning/sweep/walk-forward function takes a `DataView` restricted to IS (`oos_locked`): the only function allowed to read the OOS window is `run_oos(frozen_config)`, which takes a config whose `config_hash` was recorded before the call. OOS is evaluated **once** per hash; a second OOS call on a different hash for the same strategy id logs a `peeking` warning in the report (honest-process signal for quality-lead).

**Gate 2, walk-forward** (rolling-window design after Pardo, *The Evaluation and Optimization of Trading Strategies*, 2008): inside the IS region only. `WalkForwardConfig(train_bars, test_bars, step_bars, anchored=False, tune=None)`. Defaults: equities 756 / 252 / 252 bars (3y/1y/1y), crypto 548 / 182 / 182 (1.5y/6m/6m), giving >= 4 test windows on the minimum history. For window k: train = [s_k, s_k+train), test = [s_k+train, s_k+train+test), s_{k+1} = s_k + step. `tune=None` (default for v1) means **fixed parameters evaluated across rolling windows** (consistency check, no optimisation). With `tune=GridSpec({param: [values]}, objective="sharpe")` each train window picks the best grid point, then the chosen params are run on the following test window, and walk-forward efficiency (annualised test return / annualised train return) is reported. Consistency pass (defaults, PM-tunable in `GateConfig`): >= 60% of test windows have Sharpe > 0, and in >= 60% of windows strategy max drawdown <= 1.0x the benchmark's over the same window. Both reported with the per-window table. Each test window is run with warmup from preceding bars.

**Gate 3, OOS vs benchmark.** On the OOS run: `sharpe >= bench.sharpe`, `sortino >= bench.sortino`, `abs(max_dd) <= 1.0 * abs(bench.max_dd)`; benchmark = the strategy's class benchmark over the identical OOS window and `P`.

**Gate 4, trades.** `trade_count >= 30` over the full backtest (IS+OOS, closed round trips); OOS count is reported alongside. (Monthly rotation may fail this by construction on short history; see Open questions.)

**Gate 5, +-20% sensitivity.** `sensitivity_sweep(spec, bars_IS, pct=0.20)`: for each parameter p with base value v (one at a time, others fixed): low = floor(0.8*v) and high = ceil(1.2*v) for integer params (clamped to >= 1 and forced to differ from v, so RSI period 2 -> 1 and 3, `max_hold` 5 -> 4 and 6, `top_n` 3 -> 2 and 4), exactly 0.8v and 1.2v for float params. Plus two corner runs (all params low, all params high). Variants violating `constraints` are skipped and listed. A variant **collapses** if `sharpe < 0.5 * base.sharpe` (when base sharpe > 0; if base <= 0 the strategy has already failed gate 3 and the sweep is informational) **or** `abs(max_dd) > 1.5 * abs(base.max_dd)` **or** total_return changes sign. Pass = no collapse. Run on IS so OOS is never touched; the same sweep is cheap to rerun on OOS read-only as an extra report column (flagged "informational").

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
class HeldPosition(BaseModel):              # supplied by portfolio (SPEC-003)
    symbol: str; entry_ts: pd.Timestamp     # timestamp of the entry fill bar
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

# tradebot/backtest/engine.py
class SleeveSpec(BaseModel): sleeve_id: str; weight: float; strategy: StrategySpec | None  # None = cash
class BacktestConfig(BaseModel):
    sleeves: list[SleeveSpec]; start: date; end: date; initial_cash: Decimal
    execution: ExecutionConfig             # from SPEC-003 (fees, slippage, risk, sizing)
    rf_annual: float = 0.0
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

**Interfaces needed from data (SPEC-001), proposed**
```python
class DataProvider(Protocol):
    def get_bars(self, symbol: str, start: date, end: date, timeframe: Literal["1d"] = "1d") -> pd.DataFrame: ...
    def get_bars_many(self, symbols: Sequence[str], start: date, end: date) -> dict[str, pd.DataFrame]: ...
    def asset_class(self, symbol: str) -> Literal["equity", "crypto"]: ...
```
Bar contract (the backtester `validate(bars)` asserts it): index = tz-aware UTC `DatetimeIndex`, strictly increasing, unique, **label = bar open** (equities: session date at 00:00 UTC; crypto: 00:00 UTC day); columns exactly `open, high, low, close, volume`, float64, no NaN, `low <= open,close <= high`; stocks fully adjusted (all of O/H/L/C consistently); **only completed bars** (the in-progress current day is never returned, which is what makes the live runner safe). No forward-fill of missing sessions. `start` is inclusive and may precede the strategy start by the warmup; the caller asks for warmup bars itself.

**Interfaces needed from execution (SPEC-003), proposed**
```python
class ExecutionPort(Protocol):
    def on_open(self, ts: pd.Timestamp, opens: Mapping[str, float]) -> list[Fill]: ...
        # fill orders queued at the previous close, at this bar's open +/- slippage, minus fees
    def on_close(self, ts: pd.Timestamp, bars: Mapping[str, pd.Series]) -> EquitySnapshot: ...
        # mark to market (carry forward symbols with no bar), update stop levels and queue stop exits,
        # sleeve rebalance schedule, kill-switch; bars hold the day's O/H/L/C/V
    def on_signals(self, ts: pd.Timestamp, sleeve_id: str, signals: SignalSet) -> list[OrderIntent]: ...
        # sizing, risk veto/shrink, queue orders for the NEXT open; entries blocked if kill switch is on
    def held(self, sleeve_id: str) -> dict[str, HeldPosition]: ...
    def equity_series(self) -> pd.Series: ...        # float, UTC DatetimeIndex, one point per master-calendar bar
    def exposure_series(self) -> pd.Series: ...      # gross position value / equity per bar
    def closed_trades(self) -> list[ClosedTrade]: ...
    def open_positions(self) -> list[HeldPosition]: ...

def make_execution(cfg: ExecutionConfig, sleeves: Sequence[SleeveSpec], initial_cash: Decimal) -> ExecutionPort: ...

class ClosedTrade(BaseModel):    # money fields Decimal in ledger; backtest converts to float
    sleeve_id: str; symbol: str; entry_ts: pd.Timestamp; exit_ts: pd.Timestamp
    qty: Decimal; entry_price: Decimal; exit_price: Decimal; fees: Decimal
    net_pnl: Decimal; exit_reason: Literal["signal", "stop", "rebalance", "kill_switch"]
```
Also requested: (a) the same `ExecutionPort` is what the runner drives each live bar (it just supplies one `on_close` + `on_signals`, and `on_open` next morning), so the loop above is shared logic; (b) `ExecutionPort` is constructed fresh per run with no global state so sweeps are independent and deterministic; (c) execution imports `strategy.indicators.atr`/`vol` rather than reimplementing them.

### Invariants / rules
- Indicators at t use only bars <= t. A signal at t becomes an order that fills at t+1's open of that symbol's own calendar. `on_open` precedes `on_signals` within each loop iteration.
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
13. The three sleeves run end to end on the offline fixture dataset (from SPEC-001) using the real `ExecutionPort`; the results are stored as golden files.

## Tasks
Dependencies on SPEC-003/001 types (`ExecutionPort`, `ClosedTrade`, `ExecutionConfig`, `DataProvider`) mean T5 onwards need those interfaces in `main` first; T1-T4 do not.

### T1 [sonnet] Strategy schema, parameter resolution, IO
- **Files:** `src/tradebot/strategy/schema.py`, `src/tradebot/strategy/io.py`, `src/tradebot/strategy/__init__.py`, `strategies/core_momentum.yaml`, `strategies/mean_reversion.yaml`, `strategies/crypto_trend.yaml`
- **Depends on:** none
- **Do:** pydantic v2 models for operands, conditions, `RotationSpec`, `RulesSpec`, `StopRequest`, `Schedule`; `$param` references with substitution in `resolve`; validators (one asset class, params defined, `pos` only in rules, constraints); `warmup_bars` calculation; YAML (`yaml.safe_load`) and JSON load/dump. Copy the three YAML sleeves from this spec exactly.
- **Done when:** AC 1.

### T2 [haiku] Indicator library
- **Files:** `src/tradebot/strategy/indicators.py`
- **Depends on:** none
- **Do:** implement section 4 formulas with pandas/numpy only, including the SMA seeding and edge cases (RSI all-up/flat, Bollinger ddof=0, NaN warmup). No TA libraries. Pure functions, float64.
- **Done when:** AC 2.

### T3 [sonnet] Signal evaluation engine
- **Files:** `src/tradebot/strategy/signals.py`, `src/tradebot/strategy/engine.py`
- **Depends on:** T1, T2
- **Do:** `compile_strategy`, `prepare` (per-symbol causal feature frames, de-duplicated indicator computation, NaN->False comparison semantics), `is_decision_bar`, rotation and rules evaluation exactly as section 2a, `Intent`/`SignalSet`/`HeldPosition`, stop requests copied onto long intents, deterministic ordering.
- **Done when:** AC 3, 4, 5.

### T4 [haiku] Metrics
- **Files:** `src/tradebot/backtest/metrics.py`
- **Depends on:** none (uses a local `ClosedTrade` Protocol until SPEC-003 lands)
- **Do:** section 7 formulas; `periods_per_year`; zero-variance and empty-trades guards; `Metrics` pydantic model.
- **Done when:** AC 7.

### T5 [sonnet] Backtest engine and report
- **Files:** `src/tradebot/backtest/engine.py`, `src/tradebot/backtest/calendar.py`, `src/tradebot/backtest/report.py`, `src/tradebot/backtest/__init__.py`
- **Depends on:** T3, T4, SPEC-003 `ExecutionPort`, SPEC-001 `DataProvider` types
- **Do:** `BacktestConfig`, `SleeveSpec`, bar-contract `validate`, master calendar and weekend carry-forward handling, the loop in section 6, benchmark generation and `run_with_benchmark`, `config_hash` and `data_fingerprint`, canonical JSON report.
- **Done when:** AC 6, 8, 9.

### T6 [sonnet] OOS split, walk-forward, sensitivity, gate
- **Files:** `src/tradebot/backtest/validation.py`, `src/tradebot/backtest/gate.py`
- **Depends on:** T5
- **Do:** section 8 in full: `split_oos` and the `oos_locked` data view, `check_history`, `walk_forward` (fixed-param and grid-tuned), `sensitivity_sweep` (floor/ceil rule, corners, constraints, collapse test), `run_oos` peeking flag, `evaluate_gate`, `forward_stats`.
- **Done when:** AC 10, 11, 12.

### T7 [haiku] Tests: schema and indicators
- **Files:** `tests/strategy/test_schema.py`, `tests/strategy/test_indicators.py`
- **Depends on:** T1, T2
- **Do:** cover AC 1 and 2 with hand-computed reference vectors written into the test file.
- **Done when:** pass offline.

### T8 [haiku] Tests: metrics and report determinism
- **Files:** `tests/backtest/test_metrics.py`, `tests/backtest/test_report_golden.py`
- **Depends on:** T4, T5
- **Do:** cover AC 7 and 8 with a small hand-built equity series and a stub `ExecutionPort`.
- **Done when:** pass offline.

### T9 [sonnet] Tests: no-lookahead and engine property tests
- **Files:** `tests/strategy/test_no_lookahead.py`, `tests/strategy/test_signals.py`
- **Depends on:** T3
- **Do:** AC 3 (property/randomised truncation invariance), AC 4 and 5 on synthetic panels. Property tests go to engineer per routing rule.
- **Done when:** pass offline.

### T10 [haiku] Tests: validation tools and sleeves end to end
- **Files:** `tests/backtest/test_validation.py`, `tests/backtest/test_sleeves_golden.py`
- **Depends on:** T6, SPEC-001 offline fixture, SPEC-003 `SimBroker`
- **Do:** AC 6, 10-13 using the fixture dataset; write goldens under `tests/golden/`.
- **Done when:** pass offline.

## New runtime dependencies
| Package | Reason |
|---|---|
| `pandas`, `numpy` | already in stack; all indicators and metrics are short vectorised formulas, and we need exact, documented conventions (SMA seed, Wilder, ddof) |
| `pydantic` (v2) | already in stack; schema, validation, JSON |
| `pyyaml` | the one addition: YAML load/dump of strategy files, via `yaml.safe_load` only (never `load`) |

Rejected: TA-Lib (C build/install friction; its conventions differ in warmup and seeding), pandas-ta / `ta` (less maintained, hidden defaults that would make golden numbers depend on a third party), vectorbt/backtrader/zipline (their own fill and data models would conflict with our simulator and the t->t+1 rule, and execution is a separate lead's code). Dev-only suggestion for SPEC-005: `hypothesis` for the property tests in T9.

## Open questions
1. **Filter-then-rank vs rank-then-filter** for sleeve 1 (STRATEGY.md says "hold the top 3 ... only those whose price is above their 200-day SMA"). Default here: filter first (`pre_rank`), which keeps more slots invested; `post_rank` is available. PM to confirm.
2. **Fallback for empty slots:** IEF unconditionally, or only if IEF itself passes a check (e.g. above its own SMA), else cash? Schema supports `fallback.require`; the example uses unconditional IEF.
3. **ATR-stop semantics** (close-based trigger, exit at next open) must be agreed with execution-lead; intraday low breaches are ignored on daily bars. Cooldown after a stop exit is undecided.
4. **Trade-count gate scope:** >=30 over full history, or OOS only? A monthly top-3 rotation may produce fewer than 30 round trips on 10y.
5. **Walk-forward consistency thresholds** (60% / 60%) and the collapse thresholds (0.5x Sharpe, 1.5x drawdown) are my defaults, not from STRATEGY.md; PM to ratify in `GateConfig`.
6. **Mixed-portfolio benchmark:** separate SPY and BTC benchmarks plus a static blend; confirm the blend is wanted for the combined report.
7. **Risk-free rate:** 0% default; fine for a first look but flatters Sharpe against cash-like fallback (IEF).
8. **Adjusted prices:** stock series adjusted at download time are re-based by later dividends; ratio-based rules are insensitive, absolute thresholds would not be. Data-lead should state whether adjustment is point-in-time.
9. **Crypto lookback units:** 126 bars means 6 months for stocks but about 4 for crypto; sleeves pick their own numbers, but the future builder must be taught this.

## Risks
- **Backtest != live drift** from recursive indicators if the runner's history window differs from the backtest's (mitigation: fixed `history_start`, equality test).
- **Overfitting by repetition:** many sweeps on IS inflate the best result; we only record the count in v1.
- **Weekend carry-forward** makes mixed-portfolio volatility look lower in equity terms; the convention is stated and applied identically to the benchmark.
- **Interface mismatch with SPEC-003** (who owns sleeve rebalancing, the shape of `held`/`ClosedTrade`) is the main integration risk; ADR-0001 must pin `ExecutionPort`.
- **Short crypto history** (SOL/USD since 2020) limits walk-forward windows; the 5-year crypto gate may force fewer, shorter test windows.
- **Schema growth:** every new indicator or operator touches schema, engine and sweep; keep the operand list closed and versioned (`schema_version`).
