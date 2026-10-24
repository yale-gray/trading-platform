# Trial 11: short-term reversal on a point-in-time S&P 500 (results, 2026-10-23)

- Pre-registration: [pit-reversal-preregistration-2026-10.md](pit-reversal-preregistration-2026-10.md), commit `a172de0`.
- Development window: 2016-03-01 to 2023-12-31. The hold-out is **not opened**.
- This is education and system design, not financial advice.

**Verdict: does not advance.** It fails gate 7 only: deflated Sharpe 0.48 against the required 0.95, with N = 11 trials. The trial registry now holds N = 11 of 12.

## Results ($10,000 start; daily bars; tiered costs)
SPY over the same window: +13.9%/yr, Sharpe 0.80, worst drop −33.7%.

| Run | Symbols | Per year | Total | Worst drop | Sharpe | Trades | Stress per year |
|---|---|---|---|---|---|---|---|
| **PIT, verdict** (all cleaned members) | 669 | **+15.4%** | +206% | −29.9% | 0.74 | 3,664 | +10.1% |
| PIT, excluding the 21 flagged symbols (sensitivity) | 648 | +13.5% | +170% | −29.3% | 0.68 | 3,699 | +13.6% |
| *Tier A `tA_st_reversal`, today's list (for comparison)* | 493 | *+20.1%* | *+318%* | *−31.7%* | *0.94* | *3,723* | *+16.2%* |

Other figures:
- **Delisted holdings force-sold** (A33): 4 in the verdict run, 3 in the sensitivity run.
- **Kill-switch trips:** 4 in the verdict run, 3 in the sensitivity run.
- **Survivorship bias, measured:** going from today's list to point-in-time membership removes about **4.7 percentage points a year** (20.1% → 15.4%) and takes the Sharpe from 0.94 to 0.74.

## Gate (verdict run)

| Check | Result |
|---|---|
| 1 history | pass (8.9 years) |
| 2 walk-forward | pass (Sharpe > 0 in 3 of 3 windows; drawdown ≤ SPY in 2 of 3) |
| 3 OOS vs SPY (reported, no veto) | **fail**: 2021-2023 Sharpe −0.29 vs SPY 0.31; worst drop 32% vs 25% |
| 4 trades | pass (3,664) |
| 5 ±20% sensitivity | pass (8 variants, none collapsed) |
| Edge vs T-bills | pass: +15.7%/yr, Newey-West t = 1.72, one-sided p = 0.043 |
| SPY overlay | pass on the 50/50 Sharpe, 0.85 vs SPY's 0.80. Alpha +5.4%/yr is not significant (t = 0.76); beta 0.79 |
| 7 deflated Sharpe | **fail**: 0.48 vs 0.95 (N = 11, SR0 = 0.0475 per day) |

The sensitivity run fails both the DSR (0.43) and the edge test (p = 0.053).

## Reading
- **Most of the old +20% was real market exposure plus survivorship.** With a fair universe, the rule earns about what SPY earns (+15.4% vs +13.9%) with a slightly lower risk-adjusted return (Sharpe 0.74 vs 0.80). Its beta is about 0.8: it is mostly a jumpier way of owning large US stocks.
- **The "edge vs cash" passes (p = 0.04), but that mostly reflects being invested in stocks during a rising market.** The test that asks whether it adds anything beyond SPY (alpha t = 0.76) does not come close.
- **After correcting for 11 trials, the result is consistent with luck.** It also did worse than SPY in 2021-2023.
- **It is sensitive to costs:** +10.1%/yr at 2× slippage.

## Where this leaves the research programme
- 11 pre-registered trials have been run, and none advance. One trial is left in the budget.
- The honest summary: no rule tested here, whether day trading, overnight, calendar or reversal, has shown an edge over buy-and-hold SPY that survives fair data, realistic costs and a multiple-testing correction.
