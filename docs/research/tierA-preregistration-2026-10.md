# Tier A batch "tierA-2026-10": pre-registration (2026-10-23)

This was written and committed **before any backtest of these ids**. It follows the protocol in [dt-deep/00-summary.md](dt-deep/00-summary.md) and ADR-0001 A23-A30. Audit 008 corrections were applied before any results were seen.

## Trial budget
- N budget = 12.
- 4 earlier failures are already in the registry: `stocks_dt_dip`, `stocks_dt_breakout`, `etf_dt_late_momentum`, `etf_dt_opening_drive`.
- This batch adds 6, so N = 10 after it. Two trials remain in the budget.

## Candidates (files in `strategies/`)

| id | Idea | Bars | Account | Universe |
|---|---|---|---|---|
| `tA_st_reversal` | Hold the 10 worst 5-day performers, sell after 5 sessions | daily | overnight (daily) | current S&P 500 (survivorship-biased) |
| `tA_overnight` | Buy SPY/QQQ at 15:45, sell at the next 09:30 | 15m, clock-only | swing (no flatten) | SPY, QQQ |
| `tA_noise_momentum` | Buy SPY above its time-of-day noise band; exit below the band or VWAP, or at the close | 15m | flatten at close | SPY |
| `tA_gap_reversal` | Buy stocks that gap down at least 3%; hold to the close | 15m | flatten at close | current S&P 500 (survivorship-biased) |
| `tA_turn_of_month` | Hold SPY from the last trading day to the 3rd | daily, clock-only | overnight (daily) | SPY |
| `tA_pre_fomc` | Hold SPY 13:45 to 13:45 over the day before a scheduled FOMC statement | 15m, clock-only | swing | SPY |

## Fixed test settings (all candidates)

**Account**
- $10,000 simulated, one strategy per account, 100% to that strategy.
- Per-symbol cap = the strategy's own maximum weight.
- `settlement: none` (margin account, no leverage, A30).
- Fractional shares.
- Kill switch: 20% drawdown, 20-session pause.

**Costs:** `cost_profile: tiered` (A23). This is the verdict cost. `tiered_stress` (2x slippage) is reported alongside.

**Timing:** decision lag 2 bars for 15m data-driven rules; 0 for clock-only rules (A28); daily rules fill at the next open.

**Data and window**
- Alpaca SIP 15m bars, and yfinance daily bars.
- Development window 2016-03-01 to 2023-12-31; every bar from 2024-01-01 on is dropped first (A25).
- Symbols without data are dropped from a universe and listed in the results.

**Validation**
- `run_validation(protocol=True, holdout_start=2024-01-01)`.
- `GateConfig.protocol_2026()`: history ≥ 7 years, walk-forward 2y/1y/1y with at least 3 windows, at least 60 round trips, ±20% sensitivity, DSR ≥ 0.95.
- `evaluate_batch` applies Holm at α = 0.05 to the one-sided T-bill edge p-values.

**"Advance to hold-out" requires every one of these:**
- history
- walk-forward
- trades
- sensitivity
- DSR
- Holm-adjusted T-bill edge p < 0.05
- SPY overlay value: alpha t > 2 **or** a 50/50 SPY+strategy Sharpe above SPY's

**Afterwards**
- At most 2 finalists open the 2024+ hold-out, once each.
- A finalist must then paper-trade for 90 days before any further step.
- Every result is published, including failures. Gate 3 (OOS vs SPY) is reported but does not veto.

## Known limitations, stated in advance
- **Survivorship:** `tA_st_reversal` and `tA_gap_reversal` use today's S&P 500 list, so any result is an upper bound.
- **Gate 5:** clock-only rules (`tA_overnight`, `tA_turn_of_month`, `tA_pre_fomc`) use literal bar indices, so gate 5 tells us nothing about them.
- **Small samples:** `tA_pre_fomc` has about 60 events and `tA_turn_of_month` about 95 in 2016-2023, so both have low power.
- **Simplifications:** `tA_noise_momentum` drops the paper's short side, leverage, volatility targeting and 1-minute checks, so it is a weaker version of the published rule.
