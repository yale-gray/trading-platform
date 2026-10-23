# Day trading on real data (2026-10-23)

**Verdict: both pre-registered day-trading strategies fail the gate. The day-trading account is not started.**

## Setup
- **Data:** Alpaca SIP 15-minute bars for 501 of the 503 current S&P 500 symbols, 2016-01 to 2026-10.
  - DOW returned no bars, and one more symbol was unavailable at the start date.
  - The universe is the *current* list, so the results are survivorship-biased (ADR A13); they flatter the strategies.
- **Account:** $10k cash account, T+1 settlement, fractional shares, flatten at every close, 5 bps slippage a side, no fees.
- **Rules and timing:** at most 5 positions per sleeve. Decision lag is 2 bars (A22): a signal at bar t fills at bar t+3's open.
- **Run:** 2016-03-01 to 2026-10-22, kill switch on (20% drawdown, 20-session pause).
- **Gate:** 70/30 out-of-sample split; walk-forward 3y/1y/1y in 15m bars (19656/6552/6552); ±20% sensitivity.

## Results (10.6 years, $10,000 start)

| | Total | Per year | Worst drop | Sharpe | Trades | Win rate | Kill switch |
|---|---|---|---|---|---|---|---|
| Dip (`stocks_dt_dip`) | −89.1% | −18.8% | −89.9% | −2.31 | 12,416 | 47.8% | 10× |
| Breakout (`stocks_dt_breakout`) | −91.0% | −20.2% | −91.1% | −2.87 | 12,317 | 36.6% | 11× |
| 50/50 account (the preset) | −90.6% | −19.9% | −90.9% | −3.47 | 24,825 | 42.1% | 10× |
| SPY buy-and-hold | +360.5% | +15.4% | −35.1% | 0.93 | 1 | — | — |

### Gate
| Gate | Dip | Breakout |
|---|---|---|
| 1 history ≥10y | pass (10.78y) | pass (10.80y) |
| 2 walk-forward | **fail** (Sharpe>0 in 25% of windows) | **fail** (0%) |
| 3 OOS vs SPY | **fail** (Sharpe −3.19 vs 1.27; drop 62% vs 21%) | **fail** (−3.38 vs 1.27; 56% vs 21%) |
| 4 ≥30 trades | pass | pass |
| 5 ±20% sensitivity | pass (no collapse) | pass |

## Why it loses: costs vs edge
The same runs with zero slippage, a diagnostic only:

| | Total | Per year | Worst drop | Sharpe |
|---|---|---|---|---|
| Dip, no costs | +36.6% | +3.0% | −18.1% | 0.37 |
| Breakout, no costs | −6.0% | −0.6% | −22.1% | −0.03 |

- **Breakout has no edge** even before costs.
- **Dip has a tiny gross edge**, about 3%/yr, far below SPY. Each round trip costs about 10 bps, and that turns it into −19%/yr.
- About 1,200 round trips a year on $10k means the slippage alone is bigger than any edge.
- The **cash account** caps each sleeve at 5 entries a day, because proceeds settle the next day. More trading would not help: more trades means more cost.

## Decision
- Do not create the `daytrading` account.
- The code (intraday engine, presets, web page) stays. The strategy files stay as failed, pre-registered research.
- Per the pre-registration, any new day-trading idea is a new strategy id with its own write-up, not a re-tune of these numbers.
