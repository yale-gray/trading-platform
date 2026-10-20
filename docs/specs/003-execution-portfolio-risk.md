# SPEC-003: Simulated execution, portfolio and risk

- **Lead:** execution-lead
- **Status:** approved (conforms to ADR-0001)
- **Roadmap item:** Phase 2 (backtester fills) and Phase 3 (SimBroker, ledger, sleeves, sizing, limits, stops, kill switch)
- **Related ADRs/specs:** STRATEGY.md §4–6; SPEC-001 (data), SPEC-002 (strategy/backtest), SPEC-004 (platform), SPEC-005 (quality). **ADR-0001 (accepted) wins on every conflict**; this spec was amended to conform (D1-D8, D11). `PortfolioEngine` below is the canonical execution interface (ADR D3).

## Goal
One deterministic, persistable engine that turns strategy intent into simulated fills, tracks cash and positions to the cent per sleeve, and enforces the risk rules. The backtester (002) and the paper runner (004) call the **same** code, so a backtest and a paper run over the same bars produce identical ledgers.

## Non-goals
- Real brokers, live keys, or any code that could place a real trade (CLAUDE.md rule 1). Only `SimBroker` exists.
- Signals, indicators, metrics (strategy-lead). Data fetching (data-lead). DB schema, scheduler, UI (platform-lead).
- Shorting, leverage, margin, limit/stop-limit orders, intraday bars, partial fills from volume, separate dividend or interest-on-cash cash flows, taxes (all v2+). Dividends are not modelled as cash events: they are already inside the fully adjusted OHLC prices (ADR D2), so returns are total-return.
- Trade tracking: closed trades, equity series and exposure are pure functions of the ledger and snapshots, built by 002 (`backtest/trades.py`, ADR D3). This spec adds no trade-tracking API. `exit_reason` mapping: `OrderOrigin.STRATEGY`->`signal`, `STOP`->`stop`, `REBALANCE`->`rebalance`.

## Design

### 1. Time model and per-bar protocol (the no-lookahead contract)
The engine never reads a wall clock. Every call carries `asof: date` (UTC calendar date of the bar's open-date label, ADR D1). The `pd.Timestamp` -> `date` conversion happens **only in `BarDriver`** (002); the engine never sees a timestamp. Per bar *d*, the caller (always `BarDriver`, for backtests and the paper runner alike) does exactly:

1. `result = engine.process_bar(asof=d, bars=...)`. Inside, in this fixed order:
   - **A. Fill** all pending orders at bar *d*'s **open** (+slippage). Orders were created at close *d-1* or earlier. SELLs fill before BUYs (frees cash first), each in `order_id` order.
   - **B. Mark** every position at bar *d*'s **close**. Update `high_water_close`. Symbols with no bar today keep their last close (stale mark).
   - **C. Execute rebalance transfers** if a plan is pending (§8).
   - **D. Stops:** evaluate trailing stops on the close; emit STOP sell orders (fill *d+1* open).
   - **E. Equity snapshot** at close; update `peak_equity`; evaluate the kill switch (may trip).
   - **F. Rebalance decision** if *d* is the first bar of a new calendar month and the switch is not tripped (§8).
2. `BarDriver` evaluates strategies on data up to close *d*, translates the `SignalSet` through `TargetBook`, and calls `engine.submit_target_weights(...)` only for (a) a sleeve on that sleeve's decision bars and (b) every sleeve on a bar where `BarResult.rebalance_planned` is true (ADR D3). Resulting orders fill at *d+1* open (step A of the next call).

A symbol's pending order fills at that symbol's **next available bar**. If no bar arrives within `max_pending_bars` (default 5) calendar bars of the engine clock, the order becomes `EXPIRED`. The engine clock is "every date on which any symbol has a bar". Crypto therefore trades on weekends while ETFs wait for Monday (ADR D6; Q1 resolved).

`process_bar` with `asof <= last_processed_asof` raises `StaleBarError` (restart-safe, no double-processing).

### 2. Numeric types, conversion boundary and rounding
All ledger/accounting/risk-sizing code uses `decimal.Decimal`. Every function runs inside `localcontext(prec=28)` and **passes an explicit `rounding=` to every `quantize`**. Never construct `Decimal(float)`.

| Quantity | Type | Quantum | Rounding |
|---|---|---|---|
| Price (open/high/low/close/fill/stop/ATR) | `Decimal` | `1e-8` | input: HALF_EVEN; buy fill price: CEILING; sell fill price: FLOOR |
| Cash, notional, fee, market value, cost basis, P&L | `Decimal` | `0.01` (cents) | notional and market value: HALF_EVEN; fee: HALF_UP |
| Equity quantity (whole shares) | `Decimal` (integral) | `1` | buy sizing: DOWN; sells exact |
| Crypto quantity | `Decimal` | `1e-8` | buy sizing: DOWN; sells exact |
| Weights, fractions, bps rates | `Decimal` | `1e-6` for weights from strategies; limits/bps are exact config strings | HALF_EVEN |
| Volatility, ATR-as-ratio | `Decimal` | `1e-10` | HALF_EVEN |

**Float boundary:** the data layer hands us floats. `money.to_price(x: float) -> Decimal` is the *only* place floats enter: reject non-finite or `<= 0`, then `Decimal(repr(x)).quantize(1e-8, HALF_EVEN)`. `money.to_ratio(x: float)` is the same for weights/vol/ATR-derived ratios. After conversion nothing flows back to float inside the engine. Snapshots expose `Decimal`; analytics (002) convert with `float()` on their side. Persistence stores Decimals as canonical `str(Decimal)` TEXT (ADR D7, see §10).

Derived formulas (all exact, then quantized once at the end):
- Buy fill price `= ceil8(open × (1 + slip_bps/10000))`; sell `= floor8(open × (1 − slip_bps/10000))`. Rounding always works against the trader.
- `notional = q2(qty × fill_price)`. `fee = q2_half_up(notional × fee_bps/10000)`.
- Buy cash out `= notional + fee`. Sell cash in `= notional − fee`.
- Slippage is embedded in the price (no separate cash line). `slippage_cost` is recorded informationally on the FILL event: `|fill_price − open| × qty`.

**Cost schedule (STRATEGY §6), config not code:** equity `fee_bps=0, slip_bps=5`; crypto `fee_bps=10, slip_bps=10`. Asset class comes from the universe (Interfaces needed, data). Crypto symbols look like `BTC/USD`.

### 3. Order model and lifecycle
```
            submit            risk ok                  next bar of symbol
 (request) ------> NEW ---------------> PENDING ------------------------> FILLED
                    |                      |  \--no bar within N bars--> EXPIRED
                    | risk veto            \--cancel()----------------> CANCELLED
                    v
                 REJECTED
```
`NEW` is transient (inside `submit_*`; never persisted). Terminal states: `FILLED, REJECTED, CANCELLED, EXPIRED`. No transitions out of a terminal state (raise `IllegalTransition`). There are no partial fills from liquidity. A cash shortfall at fill time **shrinks** the quantity (`filled_qty < qty`, `reason=SHRUNK_CASH`) and the order is still `FILLED`; zero affordable quantity makes it `REJECTED(INSUFFICIENT_CASH)` at fill time.

Rules: long-only; a SELL can never exceed the sleeve's position minus qty already reserved by pending SELLs; a BUY reserves cash (`qty × ref_price × (1+fee_rate+slip_rate)`) so two pending BUYs cannot over-commit a sleeve.

### 4. Strategies hand over **target weights**, not orders (decision)
`submit_target_weights(sleeve_id, weights, asof)`: `weights` are fractions of **sleeve equity**, long-only, `Σ ≤ 1`, remainder is sleeve cash. The call is level-triggered and idempotent: "hold exactly this". `BarDriver` calls it only on a sleeve's decision bars and on every sleeve when `BarResult.rebalance_planned` is true (ADR D3), never blindly every bar, so monthly sleeves do not drift-trade daily. The `TargetBook` (002) holds the latest decision-bar weights; a symbol absent from a decision bar's `long` intents has target 0. Reasons:
1. Sizing, risk checks, stops, rounding, cash reservation and rebalancing all need portfolio-wide state that strategies should not see. Orders from strategies would leak that logic into 002 and duplicate it.
2. Level-triggered targets make monthly rebalancing and restarts safe: a missed or repeated call cannot double-trade, since the engine only trades the *delta* between target and projected holdings.
3. Fewer ways to violate no-lookahead: strategies cannot pick a fill price or timing.
4. A low-level `submit_orders(...)` still exists (used by the rebalancer, stops and tests, and part of the `Broker` protocol) but strategies do not call it.

`weights` keys outside the sleeve's `allowed_asset_classes` are rejected. A symbol absent from `weights` has target 0 (exit). To avoid churn, deltas smaller than `min_trade_fraction` (default `0.005` = 0.5% of sleeve equity) are skipped, except full exits and a fill that would take the weight to 0.

### 5. Sizing (volatility-scaled)
Inside a sleeve with `vol_scaled=True`, given strategy weights `w_i` (sum `W`) and 20-day volatility `σ_i` (stdev of 20 daily simple returns ending at close *d*, not annualized, arriving on `Bar.vol20`, computed by `BarDriver` via `tradebot.strategy.indicators.vol`: sample std, ddof=1, of simple returns; ADR D4):
```
s_i   = w_i / max(σ_i, 1e-4)
w'_i  = W × s_i / Σ_j s_j        # same total exposure, tilted to calmer assets
target_value_i = w'_i × sizing_equity(sleeve)
```
`sizing_equity(sleeve)` is the sleeve's equity at the last close, except during a pending rebalance, when it is the sleeve's *planned target value* (§8). Symbols with `σ is None` (warm-up) are dropped from entries (`NO_VOL_DATA`), and the others renormalize. A single-symbol target keeps its weight (`w' = w`). With `vol_scaled=False` (cash sleeve) `w' = w`. Raw desired quantity = `target_value / ref_price` (ref = close *d*), rounded DOWN per §2.

### 6. Risk checks: order of application, shrink vs reject
Run inside `submit_target_weights`, per sleeve in config order, symbols alphabetical. **Sizing happens first, checks after.** Delta = `target_value − (projected position value)` where projected = current qty + pending BUYs − pending SELLs, valued at close *d*.

- **SELLs (reductions and exits) are never vetoed** by R0, R3–R5. They are only clamped to holdings and subject to R6. This is how exits stay open under the kill switch.
- **BUYs** pass this pipeline, in order:

| # | Check | Outcome on breach | Reason |
|---|---|---|---|
| R0 | Kill switch tripped | **Reject** (all entries and adds) | `KILL_SWITCH` |
| R1 | Symbol tradable, price and σ present, symbol not in sleeve's `stopped_out` lock | **Reject** | `NO_DATA`, `NO_VOL_DATA`, `STOP_LOCK` |
| R2 | Sizing (§5) | n/a (computes the desired notional) | |
| R3 | **25% max per asset**, measured on the symbol's exposure across **all sleeves**: `room = 0.25 × portfolio_equity − (Σ_sleeves mv(symbol) + pending BUY notional of symbol)`, equity = last close snapshot | **Shrink** to `room`; reject if `room <= 0` | `MAX_ASSET_SHRUNK`, `MAX_ASSET` |
| R4 | **30% max crypto**, same measurement over all crypto symbols and sleeves | **Shrink** to room; reject if none | `MAX_CRYPTO_SHRUNK`, `MAX_CRYPTO` |
| R5 | Sleeve cash: `notional + fee ≤ sleeve_cash − reserved_cash`. No borrowing from other sleeves | **Shrink** | `CASH_SHRUNK` |
| R6 | Rounding to whole shares / 1e-8, then minimum size: qty `> 0` and `notional ≥ min_trade_fraction × sizing_equity` (not applied to full exits) | **Reject** | `BELOW_MIN_SIZE` |

**Policy:** a *limit* breach **shrinks** (keeps the intent, trims to what is allowed; R3, R4, R5), because the strategy wanted the position and part of it is legal. A *hard block* (kill switch, missing data, stop lock, nothing fits, rounding to zero) **rejects**. The tightest of R3/R4/R5 wins since they apply sequentially to a shrinking amount.
Pending BUY notionals accumulate within the call, so a later symbol sees earlier ones. Pending SELLs are **not** credited against exposure (conservative; level-triggered targets retry next bar).
Limits are **entry-time** checks against close-*d* reference prices; a position that later drifts above 25% through price moves is not force-sold (it only blocks further adds). Cash is re-checked at fill (shrink, §3); limits are not.

Every veto or shrink is returned in `TargetResult.rejections` with a machine-readable reason, so 004 can log it and 005 can assert on it.

### 7. Trailing stop and kill switch
**Trailing stop** (sleeves with `trailing_stop_atr_mult` set: mean-reversion and crypto per STRATEGY §5; core has none). The multiplier is declared in the strategy spec's `stops: [atr_trailing]` field and `BarDriver` copies `mult` into `SleeveConfig.trailing_stop_atr_mult` (ADR D5). v1 supports one ATR period (14) per engine. `ATR14` arrives on `Bar.atr14`, computed by `BarDriver` via `tradebot.strategy.indicators.atr` (Wilder ATR); this spec never reimplements it (ADR D4).
- On the first fill from flat, `high_water_close = close of the fill bar`. Each later close sets `high_water_close = max(high_water_close, close)`. Reset when the position returns to flat.
- `candidate = high_water_close − 2.5 × ATR14(d)` (`Bar.atr14` as of close *d*). `stop_level = max(prev_stop_level, candidate)` (ratchets up only; floor at 0).
- **Trigger (close-based, no intrabar):** if `close_d <= stop_level`, create a full-position market SELL tagged `origin=STOP`, which fills at *d+1* open. Close-based triggers avoid intrabar-ordering ambiguity on daily bars and cannot look ahead.
- After a stop exit the symbol is added to the sleeve's `stopped_out` set. It is **re-armed** only after the strategy has submitted target 0 (or omitted the symbol) at least once, which "absent from the `TargetBook` means 0" does naturally (ADR D3; Q4 resolved: the lock lasts until the strategy goes flat once, and it survives monthly rebalances). This prevents instant re-entry whipsaw. State persists.
- If ATR14 is `None` on a bar, the stop level is unchanged; the stop is still tested against the existing level.

**Kill switch:**
- `drawdown = 1 − equity / peak_equity`, from the end-of-day snapshots (peak starts at initial capital). `drawdown >= 0.20` trips it at step E. Tripping appends a `KILL_SWITCH_TRIPPED` ledger event (equity, peak, drawdown) and cancels any PENDING BUYs.
- Tripped state: R0 rejects every BUY; SELLs and STOP exits still work; **no auto-liquidation**; **rebalance is frozen** (no new `RebalancePlan` at step F, and a pending plan is dropped on trip; ADR D8, Q5 resolved). Rebalance transfers therefore never run while tripped.
- **Manual reset only, CLI only:** `reset_kill_switch(operator: str, note: str, asof: date)` appends `KILL_SWITCH_RESET`, clears the flag, and sets `peak_equity = current equity` (otherwise it would re-trip immediately). It is reached **only** through the platform CLI command (ADR D8). The platform API's "ack" endpoint merely acknowledges the trip and **does not reset**; nothing else (runner, driver, backtester) ever calls it. A tripped backtest continues exit-only and reports `killed_on=<date>`. Peak equity is portfolio-wide (not per sleeve). Dashboards read `snapshot().killed`.

### 8. Sleeves and monthly rebalance
A `Portfolio` holds `Sleeve`s (cash + positions each). Initial capital `100000.00` is funded by one `DEPOSIT` ledger event per sleeve at `target_weight × capital` (`50000/20000/20000/10000`; the cash buffer sleeve has no strategy). The same symbol may live in two sleeves (e.g. SPY in core and mean reversion); each order and position carries a `sleeve_id`, and portfolio-wide limits sum across sleeves.

**Rebalance (monthly back to 50/20/20/10), strategy-driven, no special sell logic:**
1. *Decision* (step F, first processed bar of a new month, not tripped): `target_value_s = target_weight_s × portfolio_equity`. Store a `RebalancePlan{asof, surplus_s = equity_s − target_value_s}`. While it is pending, `sizing_equity(s) = target_value_s`.
2. Strategies' next `submit_target_weights` (same bar, step 2) are sized off `target_value_s`, so overweight sleeves sell down pro-rata and underweight sleeves ask for more (shrunk by available cash, R5).
3. *Transfer* (step C of the next `process_bar`, after sells fill): for each overweight sleeve move `min(surplus_s, sleeve_cash)` to the underweight sleeves pro-rata to their deficits, as paired `CASH_TRANSFER_OUT/IN` ledger events sharing a `transfer_id`. Any shortfall retries for up to 3 bars, then a `REBALANCE_INCOMPLETE` annotation is logged and the plan is dropped. The cash buffer sleeve is just a sleeve whose targets are always empty, so its surplus/deficit transfers immediately at the next bar.
4. Next day strategies deploy the newly received cash through the normal path.

Transfers only ever move **cash** between sleeves, never positions. Asset caps (R3/R4) still apply to every rebalance-driven buy.

### 9. Ledger and equity snapshots
The ledger is an **append-only list of `LedgerEvent`s**; portfolio state (cash and positions per sleeve) is a pure fold of it. Events are never edited or deleted; corrections would be new events.

| `type` | Cash effect on `sleeve_id` | Notes |
|---|---|---|
| `DEPOSIT` | `+amount` | initial funding only in v1 |
| `FILL` | BUY `−notional`, SELL `+notional` | `symbol, qty, price, order_id, slippage_cost, realized_pnl` (SELL) |
| `FEE` | `−amount` | linked to its `FILL` via `order_id` |
| `CASH_TRANSFER_OUT / IN` | `∓amount` | paired by `transfer_id` |
| `KILL_SWITCH_TRIPPED / RESET`, `REBALANCE_PLANNED / INCOMPLETE`, `STOP_TRIGGERED` | `0` | annotations, carry metadata |

Every event has: `seq` (dense, starts at 1), `asof: date`, `stage` (`OPEN_FILL|CLOSE|SUBMIT`), `sleeve_id`, `type`, `amount`, plus the fields above. IDs are deterministic (`O{n:08d}` orders, `T{n:06d}` transfers); no UUIDs, no wall-clock.

**Positions:** `qty` and `cost_basis` (cents, total cost excluding fees). On a SELL of `q` from `Q`: `reduce = q2(cost_basis × q / Q)` (all of `cost_basis` if `q == Q`); `realized = proceeds_notional − reduce`. Fees are tracked separately (`Σ FEE`). No tax lots in v1.

**Equity snapshot (step E):** per position `mv = q2(qty × close)`; `sleeve_equity = sleeve_cash + Σ mv`; `portfolio_equity = Σ sleeve_equity`. A snapshot row carries: `asof, equity, cash, drawdown, peak_equity, killed, last_seq`, per-sleeve `{cash, equity, weight, positions[{symbol, qty, mv, cost_basis, unrealized, entry_asof, high_water_close, stop_level}]}`, and exposures `{symbol→fraction, crypto_fraction}`. **`entry_asof: date | None`** (ADR D3) is the `fill_asof` of the FILL that took the position from flat. It is set on that fill, **kept unchanged on adds and trims**, and cleared (`None`) when the position returns to flat; a later re-entry sets a new value. It is persisted per position in `EngineState` and `BarDriver` reads it from `snapshot()` to build `held` for `evaluate()`. `snapshot()` is a pure read of the latest row; `process_bar` appends one row per bar (returned in `BarResult.snapshot`; platform persists as equity curve).

### 10. Persistence and determinism
Platform owns the DB; execution defines **what must persist** (`EngineState`, a pydantic model, JSON-round-trippable). **Decimals are stored as canonical `str(Decimal)`; in SQLite money and quantities are `TEXT` columns, not integer cents/e8/e6 (ADR D7).** No second rounding layer exists, and `EngineState` JSON maps one to one onto the stored rows.
- `config` (sleeves, risk, costs), `ledger` (all events), `orders` (all orders incl. terminal), `next_order_n`, `next_transfer_n`, `next_seq`
- `last_processed_asof`, `peak_equity`, `kill_state` (+ trip/reset info), `last_rebalance_month`, `rebalance_plan | None`
- per-position `entry_asof`, `high_water_close`, `stop_level`; per-sleeve `stopped_out` set; last close per symbol (stale marks)
Cash and positions are *derived* and rebuilt by replay; `from_state` verifies them against the stored values (`verify()` raises `StateCorruptionError`). **Runner contract (ADR D8):** per portfolio per bar the runner commits, in **one transaction**, `BarResult.new_ledger_events`, the order updates, `BarResult.snapshot`, the `export_state()` checkpoint, the `run_log` row (unique on portfolio and bar) and the `last_processed_bar_ts` advance. On start it restores from the latest checkpoint and calls `verify()`. Normalized tables exist for the API to read; the **ledger plus checkpoint is the source of truth**. Catch-up replays missed bars in order (capped by `MAX_CATCHUP_BARS`, 004).
Determinism rules: no randomness, no wall-clock, sorted iteration everywhere, explicit rounding. Same config + same bar sequence + same target calls ⇒ byte-identical ledger. Restoring from `export_state()` mid-run and continuing ⇒ identical to an uninterrupted run.

### Interfaces offered
```python
# tradebot/sim/orders.py
class Side(StrEnum): BUY="BUY"; SELL="SELL"
class OrderStatus(StrEnum): NEW="NEW"; PENDING="PENDING"; FILLED="FILLED"; REJECTED="REJECTED"; CANCELLED="CANCELLED"; EXPIRED="EXPIRED"
class OrderOrigin(StrEnum): STRATEGY="STRATEGY"; STOP="STOP"; REBALANCE="REBALANCE"; MANUAL="MANUAL"

class OrderRequest(BaseModel):          # market orders only in v1
    sleeve_id: str; symbol: str; side: Side; qty: Decimal; origin: OrderOrigin = OrderOrigin.STRATEGY

class Order(BaseModel):
    order_id: str; sleeve_id: str; symbol: str; side: Side; origin: OrderOrigin
    qty_requested: Decimal; qty_accepted: Decimal | None; qty_filled: Decimal
    status: OrderStatus; created_asof: date; ref_price: Decimal | None
    fill_asof: date | None; fill_price: Decimal | None; fee: Decimal | None
    reason: str | None                  # e.g. "MAX_ASSET_SHRUNK", "KILL_SWITCH"

# tradebot/sim/broker.py
class Bar(BaseModel):                   # engine input, floats allowed here ONLY (converted by money.to_price)
    symbol: str; open: float; high: float; low: float; close: float
    # vol20/atr14 are attached by BarDriver (002) using tradebot.strategy.indicators.vol/.atr (ADR D4)
    vol20: float | None = None          # sample std (ddof=1) of 20 daily simple returns, as of this close
    atr14: float | None = None          # Wilder ATR(14) in price units, as of this close

class Broker(Protocol):                 # the only impl: SimBroker
    def submit_orders(self, requests: Sequence[OrderRequest], asof: date) -> list[Order]: ...
    def cancel_order(self, order_id: str) -> Order: ...
    def process_bar(self, asof: date, bars: Mapping[str, Bar]) -> BarResult: ...   # fills only (step A)
    def snapshot(self) -> PortfolioSnapshot: ...

# tradebot/portfolio/engine.py  <- what 002 and 004 call
class PortfolioEngine:
    def __init__(self, config: EngineConfig, state: EngineState | None = None) -> None: ...
    def process_bar(self, asof: date, bars: Mapping[str, Bar]) -> BarResult: ...     # steps A-F
    def submit_target_weights(self, sleeve_id: str, weights: Mapping[str, float],
                              asof: date) -> TargetResult: ...
    def submit_orders(self, requests: Sequence[OrderRequest], asof: date) -> list[Order]: ...  # low level
    def cancel_order(self, order_id: str) -> Order: ...
    def reset_kill_switch(self, operator: str, note: str, asof: date) -> None: ...
    def snapshot(self) -> PortfolioSnapshot: ...
    def export_state(self) -> EngineState: ...
    def verify(self) -> None: ...                       # replay ledger, check invariants
    @property
    def ledger(self) -> Sequence[LedgerEvent]: ...      # read-only
    @property
    def sleeve_ids(self) -> Sequence[str]: ...

class BarResult(BaseModel):
    asof: date; fills: list[Order]; stop_orders: list[Order]; expired: list[Order]
    new_ledger_events: list[LedgerEvent]; snapshot: PortfolioSnapshot
    kill_switch_tripped: bool; rebalance_planned: bool

class TargetResult(BaseModel):
    orders: list[Order]; rejections: list[Rejection]    # Rejection{symbol, reason, requested, accepted}
```
`SleeveConfig{sleeve_id, name, target_weight: Decimal, vol_scaled: bool, trailing_stop_atr_mult: Decimal|None, allowed_asset_classes: frozenset[AssetClass], min_trade_fraction: Decimal}`; `RiskConfig{max_asset_fraction=0.25, max_crypto_fraction=0.30, kill_switch_drawdown=0.20, max_pending_bars=5}`; `CostConfig` per asset class; `EngineConfig{initial_capital, sleeves, risk, costs}`. `default_config()` encodes STRATEGY §4–6.

### Interfaces needed
**From data (001):**
- Per symbol per date: `open, high, low, close` floats on **one consistent adjustment basis**: fully adjusted OHLC, the factor `Adj Close / Close` applied to open, high, low and close alike, **including dividends** (ADR D2, Q3 resolved), so fills and marks share one basis and the SPY benchmark is total-return. Date is the bar-open date (UTC) as `date`, converted by `BarDriver`.
- `asset_class(symbol) -> AssetClass` (`EQUITY|CRYPTO`) and `is_tradable(symbol, asof)` (date-aware universe).
- Guarantee: no NaN/zero prices reach the engine (`to_price` rejects them with `BadPriceError`; the caller decides to skip or halt).

**From strategy (002):**
- `BarDriver` attaches `vol20` and `atr14` to each `Bar`, computed shift-safely **as of close *d*** with `tradebot.strategy.indicators.vol` / `.atr` (indicator code is 002's; sizing/stop semantics are defined here). Strategy `evaluate()` does not attach them.
- `BarDriver` calls `submit_target_weights` after `process_bar` for a sleeve on its decision bars, and for every sleeve on bars where `BarResult.rebalance_planned` is true (ADR D3); the cash buffer sleeve needs no call. It derives `held` from `snapshot()` (using `entry_asof`).
- The driver owns ordering: `process_bar` then submits; neither it nor the backtester ever calls `reset_kill_switch` (CLI only).
- Read results from `BarResult`/`snapshot()`/`ledger`; metrics (002) read `Decimal` and convert to float themselves. Benchmark portfolios (100% SPY / 100% BTC) are run as single-sleeve `EngineConfig`s through the same engine, so costs are applied identically.

## Accounting invariants (for quality-lead / test-engineer)
Checked by `engine.verify()` and by property tests after every `process_bar` and every `submit_*`:
1. **I1** `sleeve_cash + Σ mv == sleeve_equity` and `Σ sleeve cash + Σ all mv == portfolio_equity`, exactly (no tolerance).
2. **I2** `Σ sleeve_equity == portfolio_equity`.
3. **I3** `sleeve_cash >= 0` for every sleeve at all times (including after any fill/shrink).
4. **I4** `qty >= 0` per position (no shorts); equity qty integral; crypto qty has at most 8 decimals; no position with qty 0 retained.
5. **I5** Cash is a pure function of the ledger: `Σ cash-effect of events == sleeve_cash`; positions equal the fold of FILL events; replay of `ledger` reproduces state exactly.
6. **I6** `capital + Σ realized − Σ fees + Σ unrealized == portfolio_equity` exactly (cost basis is in cents, so this holds with zero residual).
7. **I7** Ledger `seq` is dense and strictly increasing; events are never mutated (hash of prefix is stable across steps); `CASH_TRANSFER_OUT` and `IN` amounts sum to zero per `transfer_id`; transfers never change portfolio equity.
8. **I8** No lookahead: every fill has `fill_asof > created_asof`, `fill_price` derives from that bar's **open** only; a FILL never uses a close or any later bar.
9. **I9** Costs: BUY `fill_price >= open`, SELL `fill_price <= open`; `fee == q2_half_up(notional × fee_bps/1e4)`; equity fee == 0.
10. **I10** After any accepted BUY order set, `symbol exposure <= 25%` and `crypto exposure <= 30%` of reference equity (at submission); orders that would breach are shrunk (never over the limit by more than rounding to one share/1e-8).
11. **I11** While killed: no BUY order is created or filled; SELLs still fill.
12. **I12** Kill switch trips iff `1 − equity/peak >= 0.20` at a close; `peak_equity` never decreases except via manual reset; reset requires explicit call.
13. **I13** Stop level never decreases while the position is open; STOP orders are full-position SELLs filling next bar open.
14. **I14** Order state machine: only legal transitions; terminal states are final; `qty_filled <= qty_accepted <= qty_requested`; at most one FILL event per order.
15. **I15** Determinism: two runs on identical input give byte-identical `export_state()`; restore-and-continue equals uninterrupted; `process_bar` is rejected for non-increasing `asof`.
16. **I16** After a completed rebalance, each sleeve's equity weight equals its target within `min_trade_fraction + rounding`.
17. **I17** Float safety: no `float` instance appears anywhere in `LedgerEvent`, `Order`, `PortfolioSnapshot`.
18. **I18** `entry_asof`: `None` iff qty is 0; set to the FILL's `fill_asof` on a fill from flat; unchanged by adds and trims; cleared at flat; preserved by `export_state()`/restore.

## Acceptance criteria
1. A BUY submitted at close *d* fills at *d+1* open × (1+slip), rounded per §2; fees match STRATEGY §6 for equity and crypto on worked examples.
2. Inverse-vol sizing reproduces a hand-computed example (two assets, σ 1% and 2%, weights 0.5/0.5 ⇒ 0.667/0.333 of the sleeve's deployed capital).
3. 25% per-asset limit counts the symbol across sleeves (SPY in core + mean reversion); breach shrinks, with `MAX_ASSET_SHRUNK`. 30% crypto limit shrinks; zero room rejects.
4. Trailing stop triggers at `close <= max(close since entry) − 2.5×ATR14`, produces a next-open SELL, then locks re-entry until the strategy goes flat once.
5. Kill switch trips at 20.00% drawdown, blocks BUYs, allows SELLs/stops, freezes rebalance, and survives `export_state()`/restore; only `reset_kill_switch` (reached via CLI only) clears it.
6. Monthly rebalance returns a drifted portfolio to 50/20/20/10 (within tolerance) using only cash transfers and strategy-driven sells.
7. Whole shares for equities, 8-dp for crypto; sub-share targets reject `BELOW_MIN_SIZE`.
8. All invariants I1–I17 hold under property tests with random bar paths and random target weights.
9. Backtest and runner paths both go through `PortfolioEngine`; replaying a persisted run in chunks (with restore between) yields an identical ledger.
10. Nothing under `sim/`, `risk/`, `portfolio/` imports a network/broker library or reads env/keys.
11. `entry_asof` follows I18 in snapshots and survives restore.

## Dependencies
This spec needs no third-party runtime library beyond `pydantic>=2` (models) and the stdlib (`decimal`, `datetime`, `enum`). Dev/test: `pytest`, `hypothesis`. These are **requested from platform-lead**, who applies every spec's dependencies in **one Wave-1 task** (ADR D11); no task below edits `pyproject.toml`. Nothing under `sim/`, `risk/`, `portfolio/` may import a network or broker library.

## Tasks
Per CLAUDE.md routing, everything computing money, positions, fills, P&L, risk or state machines goes to `engineer` (sonnet). Each task is tagged with its **dispatch wave** (ADR §3). File lists are exact and do not overlap between tasks. Within a wave, tasks may run in parallel except where an intra-wave `Depends on` says otherwise.

| Wave | Tasks |
|---|---|
| 1 Foundations | T1 (money), T11a (money unit tests) |
| 2 Data + schema | T2 (config), T3 (orders), T4 (costs), T5 (ledger/positions/portfolio), T11b (order and cost tests) |
| 3 Engine | T6, T7, T8, T9, T10, T11c, T13 |
| 4 Backtest | T12 (property/invariant tests) |

T2-T5 depend only on T1, so they run in parallel in Wave 2 (T4 takes fee/slip bps as plain `Decimal` arguments, not `CostConfig`; T5 stores `order_id` as `str`, not an `Order`). Wave 3 intra-wave order: T6, T7, T8 in parallel; T9 after T6; T10 after T6-T9; T11c after T6; T13 after T10.

### T1 [sonnet] Money module and conversion boundary  (Wave 1)
- **Files:** `src/tradebot/portfolio/money.py` (create `src/tradebot/portfolio/__init__.py` only if absent; empty)
- **Depends on:** none (stdlib only)
- **Self-contained brief.** Implement the single module through which every float enters the engine and every Decimal is rounded. No other spec needs to be read. Every function wraps its arithmetic in `localcontext` with `prec=28` and passes an explicit `rounding=` to every `quantize`. **Never** call `Decimal(float)`.

**Constants (Decimal):** `PRICE_Q = 1E-8`, `CENT_Q = 0.01`, `QTY8_Q = 1E-8`, `SHARE_Q = 1`, `WEIGHT_Q = 1E-6`, `RATIO_Q = 1E-10`.

**Types:** `class AssetClass(StrEnum): EQUITY = "EQUITY"; CRYPTO = "CRYPTO"`. Errors: `class MoneyError(ValueError)`; `class BadPriceError(MoneyError)`; `class BadRatioError(MoneyError)`; `class BadQuantityError(MoneyError)`.

**API (exact names and signatures):**

| Function | Behaviour | Rounding |
|---|---|---|
| `money_context()` | context manager = `localcontext()` with `prec=28`; used internally and by callers | n/a |
| `to_price(x: float \| int) -> Decimal` | the only float entry for prices. `TypeError` if `x` is `bool`, `str`, `Decimal` or other. `BadPriceError` if non-finite or `<= 0`, or if the quantized result is `<= 0`. Else `Decimal(repr(x)).quantize(PRICE_Q, ROUND_HALF_EVEN)` | HALF_EVEN |
| `to_ratio(x: float \| int, quantum: Decimal = RATIO_Q) -> Decimal` | for vol, ATR-ratios and weights. `TypeError` as above. `BadRatioError` if non-finite or `< 0` (zero allowed). `Decimal(repr(x)).quantize(quantum, ROUND_HALF_EVEN)` | HALF_EVEN |
| `to_weight(x: float \| int) -> Decimal` | `to_ratio(x, WEIGHT_Q)` | HALF_EVEN |
| `q_price(d: Decimal, rounding: str) -> Decimal` | quantize to `PRICE_Q` with the given mode | as given |
| `ceil8(d)` / `floor8(d)` | `q_price` with `ROUND_CEILING` / `ROUND_FLOOR` (buy / sell fill price) | CEILING / FLOOR |
| `q2(d: Decimal) -> Decimal` | quantize to `CENT_Q`; used for notional, market value, cost basis, P&L | HALF_EVEN |
| `q2_half_up(d: Decimal) -> Decimal` | quantize to `CENT_Q`; used for fees | HALF_UP (away from zero on ties) |
| `floor_shares(d: Decimal) -> Decimal` | whole shares for equity BUY sizing, `quantize(SHARE_Q, ROUND_DOWN)`; `BadQuantityError` if `d < 0` | DOWN |
| `floor_qty8(d: Decimal) -> Decimal` | crypto BUY sizing, `quantize(QTY8_Q, ROUND_DOWN)`; `BadQuantityError` if `d < 0` | DOWN |
| `buy_qty(d: Decimal, asset_class: AssetClass) -> Decimal` | dispatches to `floor_shares` (EQUITY) or `floor_qty8` (CRYPTO) | DOWN |
| `check_sell_qty(qty: Decimal, asset_class: AssetClass) -> Decimal` | SELL quantities are exact, never rounded: returns `qty` unchanged if already on-grid (integral for EQUITY, at most 8 decimals for CRYPTO), else `BadQuantityError`; `qty <= 0` also `BadQuantityError` | none |
| `q_ratio(d: Decimal) -> Decimal` / `q_weight(d: Decimal) -> Decimal` | quantize to `RATIO_Q` / `WEIGHT_Q` | HALF_EVEN |
| `to_text(d: Decimal) -> str` | canonical persistence form: `str(d)` computed in the explicit money context (capitals=1), with `-0` normalised to `0` (exponent kept); `TypeError` if not `Decimal` | n/a |
| `from_text(s: str) -> Decimal` | `Decimal(s)`; `TypeError` if not `str`; `MoneyError` if not finite. Round trip: `from_text(to_text(d)) == d` and has the same exponent | n/a |

**Acceptance criteria (each is a unit test):**
1. `to_price(100.0) == Decimal("100.00000000")` and its exponent is `-8`. `to_price(0.1) == Decimal("0.10000000")` (proves `repr`, not the binary expansion). `to_price(7) == Decimal("7.00000000")`.
2. Ties are HALF_EVEN: `to_price(1.000000005) == Decimal("1.00000000")` (tie to even 0); `to_price(1.000000015) == Decimal("1.00000002")` (tie to even 2).
3. `to_price` raises `BadPriceError` for `float("nan")`, `float("inf")`, `-1.0`, `0.0`, `1e-9` (quantizes to zero). `to_price(True)` and `to_price("1")` raise `TypeError`.
4. `to_weight(0.6666667) == Decimal("0.666667")`; `to_ratio(0.0123456789012) == Decimal("0.0123456789")`; `to_ratio(0.0) == 0`; `to_ratio(-0.1)` and `to_ratio(float("nan"))` raise `BadRatioError`.
5. `ceil8(Decimal("123.518517404505")) == Decimal("123.51851741")` and `floor8(Decimal("123.395060615495")) == Decimal("123.39506061")`. These are the buy and sell fill prices for open `123.45678901` at 5 bps slippage (open x 1.0005 and x 0.9995 respectively, computed exactly then rounded once). `ceil8(Decimal("100.05")) == Decimal("100.05000000")` (already on-grid, unchanged).
6. `q2(Decimal("2.345")) == Decimal("2.34")` and `q2(Decimal("2.355")) == Decimal("2.36")` (HALF_EVEN); `q2(Decimal("-2.345")) == Decimal("-2.34")`. `q2_half_up(Decimal("2.345")) == Decimal("2.35")` and `q2_half_up(Decimal("-2.345")) == Decimal("-2.35")`. Fee example: `q2_half_up(Decimal("1234.56") * Decimal("10") / Decimal("10000")) == Decimal("1.23")` (exact value 1.23456) and `q2_half_up(Decimal("100.05") * Decimal("10") / Decimal("10000")) == Decimal("0.10")` (exact 0.100050 rounds to 0.10).
7. Quantity DOWN: `floor_shares(Decimal("12.9999")) == Decimal("12")` and its exponent is 0; `floor_shares(Decimal("0.99")) == Decimal("0")`; `floor_qty8(Decimal("0.123456789")) == Decimal("0.12345678")`; `floor_qty8(Decimal("-0.1"))` raises `BadQuantityError`. Whole-share sizing example: `floor_shares(Decimal("5000.00") / Decimal("123.51851741")) == Decimal("40")` (40.4799...).
8. `check_sell_qty(Decimal("3"), AssetClass.EQUITY) == Decimal("3")`; `check_sell_qty(Decimal("3.5"), AssetClass.EQUITY)` raises `BadQuantityError`; `check_sell_qty(Decimal("0.00000001"), AssetClass.CRYPTO)` returns it unchanged; `check_sell_qty(Decimal("0.000000001"), AssetClass.CRYPTO)` raises.
9. `to_text(Decimal("100.00000000")) == "100.00000000"`; `from_text("0E-8") == 0`; `from_text(to_text(d))` has the same exponent as `d` for 8 sample values; `to_text(1.5)` raises `TypeError`.
10. Results are independent of the caller's ambient decimal context: with `getcontext().prec = 4` set by the test, `q2(Decimal("123456.785"))` still returns `Decimal("123456.78")`. `mypy`/`ruff` clean; module has no imports outside the stdlib.
- **Done when:** criteria 1-10 pass; this covers ADR D7's single-source rounding table.

### T11a [haiku] Unit tests for the money module  (Wave 1)
- **Files:** `tests/portfolio/test_money.py`
- **Depends on:** T1
- **Do:** implement T1 acceptance criteria 1-10 verbatim as pytest cases (plus a hypothesis-free parametrized rounding table). No network, no fixtures.
- **Done when:** all pass offline.

### T2 [haiku] Config models  (Wave 2)
- **Files:** `src/tradebot/portfolio/config.py`, `src/tradebot/risk/config.py`
- **Depends on:** T1
- **Do:** pydantic `SleeveConfig`, `RiskConfig`, `CostConfig`, `EngineConfig`, `default_config()` (STRATEGY §4-6 values as Decimal strings). Validation only: weights sum to 1, positive limits. No money math. `trailing_stop_atr_mult` is set from the strategy's `stops: [atr_trailing]` by `BarDriver` (ADR D5).
- **Done when:** default config validates; bad configs raise.

### T3 [sonnet] Orders and state machine  (Wave 2)
- **Files:** `src/tradebot/sim/orders.py`
- **Depends on:** T1
- **Do:** `OrderRequest`, `Order`, enums (`OrderOrigin` is the source of `exit_reason`, ADR D3), `transition()` with `IllegalTransition`; deterministic id generator.
- **Done when:** AC 8 (I14).

### T4 [sonnet] Cost model and fill pricing  (Wave 2)
- **Files:** `src/tradebot/sim/costs.py`
- **Depends on:** T1
- **Do:** fill price, notional, fee, slippage_cost functions per §2, taking `fee_bps` and `slip_bps` as `Decimal` arguments.
- **Done when:** AC 1; I9.

### T5 [sonnet] Ledger, positions, sleeve and portfolio accounting  (Wave 2)
- **Files:** `src/tradebot/portfolio/ledger.py`, `src/tradebot/portfolio/models.py`, `src/tradebot/portfolio/portfolio.py`
- **Depends on:** T1
- **Do:** `LedgerEvent`, append-only `Ledger`, `Position` (with `entry_asof`: set on fill from flat, unchanged on add/trim, cleared at flat), `Sleeve`/`Portfolio`, `PortfolioSnapshot` models (positions carry `entry_asof`), apply-fill, replay/fold, equity snapshot computation (§9).
- **Done when:** I1-I7, I18.

### T11b [haiku] Unit tests for orders and costs  (Wave 2)
- **Files:** `tests/sim/test_orders.py`, `tests/sim/test_costs.py`
- **Depends on:** T3, T4
- **Do:** worked numeric examples (AC 1 equity and crypto), state-machine legality table.
- **Done when:** pass offline.

### T6 [sonnet] Sizing and risk check pipeline  (Wave 3)
- **Files:** `src/tradebot/risk/sizing.py`, `src/tradebot/risk/limits.py`, `src/tradebot/risk/checks.py`
- **Depends on:** T1, T2, T5
- **Do:** inverse-vol sizing (consumes `Bar.vol20`; does not compute it); exposure measurement across sleeves; R0-R6 pipeline returning accepted qty + `Rejection`s; cash reservation.
- **Done when:** AC 2, 3, 7; I10.

### T7 [sonnet] Trailing stop and kill switch  (Wave 3)
- **Files:** `src/tradebot/risk/stops.py`, `src/tradebot/risk/killswitch.py`
- **Depends on:** T1, T5
- **Do:** high-water/stop ratchet using `Bar.atr14` (does not compute ATR), close-based trigger `close <= stop_level`, stop-out lock; peak/drawdown, trip (also freezes rebalance), manual reset function (§7).
- **Done when:** AC 4, 5; I11-I13.

### T8 [sonnet] SimBroker fill engine  (Wave 3)
- **Files:** `src/tradebot/sim/broker.py`
- **Depends on:** T3, T4, T5
- **Do:** `Broker` protocol, `SimBroker`: pending order book, fill step (sells first), cash shrink, expiry, cancel, `Bar` conversion; maintain `entry_asof` on fills via T5 models.
- **Done when:** AC 1; I3, I8, I14, I18.

### T9 [sonnet] Rebalancer  (Wave 3)
- **Files:** `src/tradebot/portfolio/rebalance.py`
- **Depends on:** T5, T6 (intra-wave: starts after T6)
- **Do:** monthly trigger, `RebalancePlan`, sizing-equity override, cash transfers with retry (§8); no plan while the kill switch is tripped.
- **Done when:** AC 6; I7, I16.

### T10 [sonnet] PortfolioEngine facade and persistence state  (Wave 3)
- **Files:** `src/tradebot/portfolio/engine.py`, `src/tradebot/portfolio/state.py`
- **Depends on:** T6-T9 (intra-wave: starts last)
- **Do:** wire steps A-F, `submit_target_weights`, `EngineState` export/restore/verify (includes `entry_asof`; Decimals as `to_text` strings), `StaleBarError`, `reset_kill_switch`.
- **Done when:** AC 5, 9, 11; I5, I15, I18.

### T11c [haiku] Unit tests for sizing examples  (Wave 3)
- **Files:** `tests/risk/test_sizing_examples.py`
- **Depends on:** T6 (intra-wave: after T6)
- **Do:** AC 2 hand-computed example (σ 1% and 2%, weights 0.5/0.5 => 0.667/0.333), sub-share rejection (`BELOW_MIN_SIZE`).
- **Done when:** pass offline.

### T13 [haiku] Scenario tests  (Wave 3)
- **Files:** `tests/portfolio/test_scenarios.py`, `tests/fixtures/bars_scenarios.py`
- **Depends on:** T10 (intra-wave: after T10)
- **Do:** synthetic-bar scenarios: cross-sleeve 25% breach, crypto 30% cap, stop-out and re-arm, kill-switch trip (rebalance frozen)/reset, month-boundary rebalance, weekend crypto vs Monday ETF fills, `entry_asof` across add/trim/flat/re-entry. Does not touch `tests/conftest.py` (owned by SPEC-005).
- **Done when:** AC 3-6, 11 pass offline.

### T12 [sonnet] Property and accounting-invariant tests  (Wave 4)
- **Files:** `tests/portfolio/test_invariants.py`, `tests/portfolio/test_determinism.py`
- **Depends on:** T10
- **Do:** hypothesis tests for I1-I18 with random bar paths and weights; restore-in-the-middle equivalence. (Property/invariant tests go to `engineer` per routing rule.) Uses SPEC-005's hypothesis profiles.
- **Done when:** AC 8, 9.

## Open questions
All of Q1, Q3, Q4 and Q5 are resolved by ADR-0001. Q2 and Q6 are also closed or deferred as noted.
- **Q1 (resolved, ADR D6):** Mixed calendars. Engine clock = union of dates with any bar; equities stale-marked on weekends, orders wait for the symbol's next bar.
- **Q2 (resolved, ADR D4):** `BarDriver` attaches `vol20` and `atr14` using `tradebot.strategy.indicators.vol` / `.atr` (sample std ddof=1 of simple returns; Wilder ATR).
- **Q3 (resolved, ADR D2):** Fully adjusted OHLC including dividends (factor applied to open, high, low, close); the SPY benchmark is total-return. No v1 dividend cash flows.
- **Q4 (resolved, ADR D3):** The stop-out lock lasts until the strategy goes flat once (absent from the `TargetBook` means 0); it persists across monthly rebalances.
- **Q5 (resolved, ADR D8):** A tripped kill switch freezes rebalance; dashboards read `snapshot().killed`; reset is CLI only and the API "ack" does not reset.
- **Q6 (deferred, v2):** Volume/participation limits and gap-through-stop modelling; fills assume unlimited liquidity at open +/- slippage.

## Risks
- **Rounding drift in cost basis** if anyone reintroduces avg-price math: mitigated by cents-based `cost_basis` and exact I6.
- **Level-triggered targets plus min-trade band** may leave sleeves slightly off target; acceptable but must be visible in snapshots.
- **Rebalance interplay** (sizing-equity override) is the subtlest logic; needs the scenario test and property test for I16.
- **Close-based stops on daily bars** understate gap losses (a stop at 100 may fill at an open of 90); no gap handling is a conscious simplification, flagged for the validation write-up.
- **Float leakage:** a single `Decimal(float)` anywhere breaks determinism; I17 and a lint-style grep test guard it.
- **Cross-spec drift:** `Bar` shape (with `vol20`/`atr14` attached by `BarDriver`) and the `PortfolioEngine` signatures must match 002 and 004 as amended by ADR-0001 §4.
