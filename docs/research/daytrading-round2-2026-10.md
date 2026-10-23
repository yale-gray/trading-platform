# Day trading, round 2: slower ETF ideas (pre-registration, 2026-10-23)

Round 1 (`stocks_dt_dip`, `stocks_dt_breakout`) failed: about 1,200 round trips a year, and the costs swamped a tiny or absent edge (`daytrading-2026-10.md`). Round 2 makes three changes:
- **Far fewer trades:** at most one entry per ETF per day.
- **Tiny-spread instruments:** SPY and QQQ instead of 500 stocks.
- **Rules from published research,** not invented here.

## Candidates (files committed before any backtest)
| id | Rule | Basis | Expected trades |
|---|---|---|---|
| `etf_dt_late_momentum` | If up on the day at the 14:45 bar, buy for the last 30 min (fill 15:30, flatten 16:00) | Gao et al. 2018; Baltussen et al. 2021 | ≤2/day, ~250/yr |
| `etf_dt_opening_drive` | If up after the first 30 min, buy (fill 10:30); exit if the close falls below today's open, else flatten at 16:00 | Zarattini & Aziz 2023 (simplified) | ≤2/day, ~250/yr |

## Test protocol (fixed now)
- **Data:** Alpaca SIP 15m bars for SPY and QQQ, 2016-03-01 to 2026-10-22.
- **Each candidate** runs as its own $10k account, with the same settings as the daytrading preset:
  - cash account, T+1 settlement
  - flatten at the close
  - fractional shares
  - 5 bps slippage a side, no fees
  - kill switch at 20%, paused for 20 sessions
  - decision lag of 2 bars
- **Gate:** the same as round 1: 70/30 OOS, walk-forward 3y/1y/1y in 15m bars, ±20% sensitivity, ≥30 trades.
  - Note: the parameters are bar indices, so the sweep shifts the decision time.
- **The verdict uses the 5 bps cost.**
  - A 1 bp per side run is reported as a diagnostic only, as a realistic SPY/QQQ spread. It cannot turn a fail into a pass.
- **No tuning:** a changed number is a new strategy id.
