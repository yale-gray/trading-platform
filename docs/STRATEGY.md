# Investment Strategy (Simulated)

> **Educational system design for a fake-money simulator, not financial advice.** Every number below is a *starting parameter to test*, not a recommendation.

## 1. Philosophy
- **The bot predicts nothing.** It runs written, testable rules without emotion.
- **Buy-and-hold is the bar to beat.** Every strategy is compared against holding 100% SPY (stocks) and 100% BTC (crypto) over the same period, after fees and slippage. If a strategy can't beat that on a risk-adjusted basis on unseen data, we prefer buy-and-hold.
- **Diversify across strategies, not just assets.** Strategies that win at different times smooth the ride.

## 2. Decision pipeline (runs on every new bar)
```
Universe → Regime filter → Signals → Position sizing → Risk checks → Simulated order (fills next bar) → Ledger & metrics
```
| Step | Question it answers | Owner |
|---|---|---|
| Universe | What is the bot allowed to trade? | data-lead |
| Regime filter | Is the market in an uptrend (price > 200-day SMA)? | strategy-lead |
| Signals | Buy, sell or hold, per strategy rule | strategy-lead |
| Position sizing | How much? (volatility-scaled) | execution-lead |
| Risk checks | Does this break a limit? (veto or shrink) | execution-lead |
| Simulated order | Fill at the next bar's open, plus slippage and fees | execution-lead |
| Ledger and metrics | Record it and measure it | execution-lead / strategy-lead |

## 3. Universe (liquid assets only)
- **Broad ETFs:** SPY (US large-cap), QQQ (Nasdaq-100), IWM (US small-cap), EFA (developed international), EEM (emerging markets)
- **Bonds and gold:** TLT (long Treasuries), IEF (intermediate Treasuries), GLD (gold)
- **Sector SPDRs:** XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLU, XLB
- **Crypto:** BTC/USD, ETH/USD, SOL/USD

Individual stocks are deferred until the ETF results are understood.

## 4. The paper fund: $100,000 fake, split into sleeves
| Sleeve | Weight | Rule (v1) | Edge it seeks | Fails when |
|---|---|---|---|---|
| **Core: trend-filtered ETF momentum** | 50% | Monthly, rank the ETFs by 6-month return and hold the top 3 equally weighted, only those whose price is above their 200-day SMA. Empty slots go to IEF or cash | Momentum: recent winners tend to keep winning for a while | Choppy, sideways markets and sharp V-shaped reversals |
| **Mean reversion** | 20% | SPY/QQQ: buy when RSI(2) < 10 **and** price > 200-day SMA. Exit when RSI(2) > 70 or after 5 trading days | Short-term overreaction snaps back | The first days of a real crash (the 200-day filter limits this) |
| **Crypto trend** | 20% | BTC/ETH/SOL: hold while price > 50-day SMA **and** 20-day SMA > 50-day SMA, otherwise USD | Crypto's large, persistent trends | Whipsaw periods, where each flip costs fees |
| **Cash buffer** | 10% | Always cash | Cushions drawdowns and funds rebalancing | n/a |

**Benchmarks** are separate portfolios: 100% SPY buy-and-hold, and 100% BTC buy-and-hold.

**Rebalancing:** monthly, back to 50/20/20/10.

## 5. Sizing and risk rules
| Rule | Value (v1) | Why |
|---|---|---|
| Position sizing | Volatility-scaled within a sleeve (inverse of 20-day volatility) | Jumpier assets get smaller positions, so each holding carries similar risk |
| Max per asset | 25% of portfolio equity | No single bet can sink the fund |
| Max crypto total | 30% of portfolio equity | Crypto can fall 70–80% |
| Trailing stop | 2.5 × ATR(14) from the highest close since entry (mean-reversion and crypto sleeves) | Cuts losers automatically, and the distance adapts to volatility |
| Kill switch | A 20% drawdown from the portfolio's peak equity stops new entries and flags the event for human review | Stops a broken strategy from digging deeper |

## 6. Simulated costs
| Market | Commission | Slippage |
|---|---|---|
| Stocks/ETFs | $0 | 5 bps (0.05%) per fill |
| Crypto | 10 bps fee | 10 bps per fill |

Taxes are ignored in v1.

## 7. Validation gate: backtest → paper trading
A strategy advances only if **all** of these pass:
1. History: ≥10 years for stocks and ≥5 years for crypto. The **last 30% is held out** and never used for tuning.
2. **Walk-forward:** results stay consistent across rolling train/test windows.
3. Out-of-sample **Sharpe and Sortino ≥ benchmark**, and **max drawdown ≤ 1.0× the benchmark's**.
4. **≥ 30 trades**, so the result isn't luck.
5. **Robustness:** metrics don't collapse when any parameter is nudged ±20%.
6. Then **≥ 3 months of forward paper trading**, compared against the benchmark portfolios.

## 8. Pitfalls we guard against
| Trap | What it is | Protection |
|---|---|---|
| Overfitting | Rules tuned to fit the past perfectly then fail on new data | Out-of-sample hold-out, walk-forward, ±20% parameter test |
| Lookahead bias | The backtest uses information it wouldn't have had yet | Signal on bar *t* fills at *t+1*; shifted-data tests |
| Survivorship bias | Testing only on assets that survived | Universe membership is date-aware |
| Ignoring costs | High-turnover strategies look great before fees | Fees and slippage on every fill |
| Luck | A few big trades mistaken for skill | ≥30 trades; consistency across windows |

## 9. Glossary
- **SMA (simple moving average):** the average closing price over the last N days. Price above the 200-day SMA is a common definition of an uptrend.
- **RSI (relative strength index):** a 0–100 gauge of recent up-moves vs down-moves. RSI(2) < 10 means the last two days were a sharp selloff.
- **ATR (average true range):** the typical daily price swing. It's used to set stops that adapt to volatility.
- **Momentum:** the tendency of assets that have risen recently to keep rising for a while.
- **Drawdown:** the drop from a previous peak. **Max drawdown** is the worst one, and it tells you how painful the strategy is to hold.
- **CAGR:** compound annual growth rate, the average yearly growth.
- **Sharpe ratio:** return per unit of total volatility. **Sortino** counts only downside volatility. Higher is better for both.
- **Slippage:** the gap between the price you expected and the price you got.
- **bps (basis points):** 1 bp = 0.01%.
- **Out-of-sample:** data the strategy was never tuned on, which gives an honest test.
- **Walk-forward:** repeatedly tune on one window and test on the next, rolling through time.

## 10. Role of AI (Phase 5)
Claude turns plain-English ideas into this rule schema and explains the results in plain language. It **never** makes discretionary buy/sell decisions.
