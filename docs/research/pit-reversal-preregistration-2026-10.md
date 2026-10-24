# Trial 11: short-term reversal on a point-in-time S&P 500 (pre-registration, 2026-10-23)

This was written and committed **before any backtest of `tA_st_reversal_pit`**. The batch is "pit-reversal-2026-10" (one candidate). It brings the trial registry to N = 11 of the 12-trial budget.

## Why
`tA_st_reversal` (Tier A) made +20.1%/yr on today's S&P 500 list, but a loser-buying rule run on a survivor list is exactly where survivorship bias is largest. This trial asks whether any of that survives when the strategy can only buy stocks that were in the index on the day.

## Candidate
`strategies/tA_st_reversal_pit.yaml` has the **same frozen parameters** as `tA_st_reversal`:
- 5-day return rank, ascending
- 10 positions, equal weight
- exit after 4 bars held (5 sessions)

There are only two changes:
- **Universe:** the 669 tickers that were ever S&P 500 members from 2015 on and have price data (ADR A32).
- **Entry:** `eq: [{universe: sp500_member}, 1]` instead of `always: true`. Membership comes from the previous close.

## Fixed test settings
These are the same as `tierA-2026-10` (see `tierA-preregistration-2026-10.md`), plus `force_sell_missing: true`:
- **Delisted holdings (A33):** sold at the last close minus slippage and 50 bp, or written off when the stock fell ≥ 50% in its last 5 bars.
- **Costs and account:** tiered costs (2× stress reported); `settlement: none`; $10k; daily bars, filled at the next open; kill switch at 20% with a 20-session pause.
- **Window:** 2016-03-01 to 2023-12-31. The 2024+ hold-out stays sealed.
- **Gate:** `run_validation(protocol=True)` with `GateConfig.protocol_2026()` and `evaluate_batch` (the DSR uses N from the registry and V from protocol trials).

**"Advance" requires all of these:**
- the gates: history, walk-forward, trades, sensitivity, DSR ≥ 0.95
- a one-sided T-bill edge p < 0.05. Holm with a batch of 1 equals the raw p. The DSR carries the multiple-testing correction across all 11 trials.
- a SPY overlay alpha t > 2, **or** a 50/50 Sharpe above SPY's

**Universe for the verdict:** every cleaned symbol, including real collapses (A33 as amended).
- **Sensitivity (reported, not a verdict):** the same run without the 21 symbols in `sp500-pit-exclusions-2026-10.md`.
- **Comparison (reported):** `tA_st_reversal`'s Tier A result on today's list. The difference is the measured survivorship bias.

## Known limitations, stated in advance
- **Residual survivorship.** 19 former members have no prices at all, and member-day coverage is 99.52% for 2016-2023 (`sp500-pit-coverage-2026-10.md`). Missing names lean toward failures, so any remaining bias favours the strategy.
- **Corporate actions.** Some Alpaca-sourced series may still contain unadjusted corporate actions.
- **Membership file.** Membership comes from a community dataset: 352 of 364 index changes matched Wikipedia within 20 days.
