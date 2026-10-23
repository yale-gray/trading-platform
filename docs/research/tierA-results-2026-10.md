# Tier A batch "tierA-2026-10": results (2026-10-23)

- Pre-registration: [tierA-preregistration-2026-10.md](tierA-preregistration-2026-10.md), commit `774b2bd`.
- Development window: 2016-03-01 to 2023-12-31. The 2024+ hold-out was **not opened**.
- Verdict cost: tiered (A23). This is education and system design, not financial advice.

**Verdict: no candidate advances. The hold-out stays sealed.** The trial registry now holds N = 10 of the 12-trial budget.

## Results (development window, $10,000 start; SPY over the same window: +13.9%/yr, Sharpe 0.80–0.83, worst drop −34% to −35%)

| id | Per year | Total | Worst drop | Sharpe | Trades | Stress (2× slippage) per year | Edge vs T-bills (ann., t) | Raw p | Holm p | SPY overlay alpha (t) | 50/50 Sharpe | DSR | Failed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `tA_st_reversal` | **+20.1%** | +318% | −31.7% | 0.94 | 3,723 | +16.2% | +19.5% (2.31) | 0.011 | 0.063 | +9.5% (1.50) | 0.97 | 0.69 | Holm, DSR; gate 3 OOS Sharpe −0.31 |
| `tA_pre_fomc` | +1.6% | +12.9% | −2.9% | 0.72 | 62 | +1.4% | +1.5% (2.28) | 0.011 | 0.063 | +1.4% (**2.20**) | 0.86 | 0.49 | Holm, DSR |
| `tA_turn_of_month` | +3.9% | +34.8% | −10.8% | 0.53 | 93 | +3.6% | +3.8% (1.48) | 0.070 | 0.280 | +1.5% (0.59) | 0.83 | 0.26 | Holm, DSR |
| `tA_overnight` | +1.9% | +15.8% | −23.8% | 0.23 | 3,833 | −3.3% | +1.0% (0.28) | 0.39 | 1.00 | −3.0% (−1.03) | 0.64 | 0.07 | Holm, DSR, overlay |
| `tA_noise_momentum` | −0.8% | −6.4% | −13.9% | −0.13 | 1,908 | −6.1% | −0.9% (−0.50) | 0.69 | 1.00 | −2.1% (−1.33) | 0.66 | 0.01 | WF, Holm, DSR, overlay |
| `tA_gap_reversal` | −4.1% | −28.1% | −53.8% | −0.22 | 4,235 | −10.4% | −3.3% (−0.72) | 0.77 | 1.00 | −6.7% (−1.47) | 0.43 | 0.00 | WF, Holm, DSR, overlay |

Notes on the columns:
- **50/50 Sharpe** is a 50/50 mix of SPY and the strategy.
- **Failed** lists the gates the candidate failed (WF = walk-forward).
- **Gates 1, 4 and 5** (history, trades, sensitivity) passed for every candidate. Gate 5 is uninformative for the clock-only rules (pre-registered caveat).
- **Symbols dropped for missing data:** about 10 recent additions per S&P 500 universe (e.g. GEV, SOLV, RDDT, plus DOW for the 15m data).

## Reading
- **Short-term reversal looks great but is not trustworthy.**
  - It made +20%/yr against SPY's +14%.
  - Its universe is *today's* S&P 500. "Buy the week's biggest losers" from a list that survived to 2026 is exactly where survivorship bias is largest: losers that later collapsed were removed from the list and are missing from the test.
  - In the last 30% of the window (2021-2023) its Sharpe was −0.31 with a 36% drop.
  - It also misses the corrected thresholds (Holm 0.063, DSR 0.69).
  - Treat it as an upper bound, not an edge. A fair test needs point-in-time index membership.
- **Pre-FOMC drift is the most interesting honest near-miss.**
  - It is tiny (+1.6%/yr) but steady: worst drop −2.9%, and the SPY-overlay alpha t-stat of 2.20 clears the overlay test.
  - It fails the multiple-testing corrections (Holm 0.063, DSR 0.49).
  - With 62 events, a backtest cannot settle it.
- **Turn-of-month** is positive but statistically weak.
- **Overnight drift** dies under realistic or stressed costs.
- **The noise-band momentum rule** (long-only, no leverage) and **gap-down reversal** lose money.

## Post-run corrections (disclosed)
The first run (same rules and numbers) exposed two measurement bugs. Both were fixed in `deff99f` (ADR A31) and applied equally to all six candidates before this run. No strategy file or threshold changed.

1. **The equal-weight per-symbol cap ignored `max_positions`** (it was 1/500 instead of 1/10 or 1/5). `tA_st_reversal` and `tA_gap_reversal` made **0 trades** in run 1.
2. **The T-bill edge charged idle cash the T-bill rate even though the simulator pays no interest.** It is now measured as if idle cash earned T-bills. This raised the edge of mostly-in-cash rules: `tA_pre_fomc` went from −0.0% (p = 0.52) to +1.5% (p = 0.011), and `tA_turn_of_month` from p = 0.17 to p = 0.07.
   - The run-1 DSR (pre-FOMC 0.66, turn-of-month 0.39) also differs from run 2, because V is computed from the batch.

## Implications
- Across 10 pre-registered trials (4 day-trading, 6 Tier A), nothing beats holding SPY after an honest multiple-testing correction.
- The two near-misses are slow calendar rules, not day trading.
- Next options, if any:
  - (a) Point-in-time S&P 500 membership, to re-test reversal fairly. That would be a new trial id.
  - (b) Treat pre-FOMC as a forward paper experiment only. It is a few days a year, and more history can't be created.
  - (c) The remaining 2 trials of the budget go to the Tier B idea with the best cost profile (ML market timing from the cross-section).
