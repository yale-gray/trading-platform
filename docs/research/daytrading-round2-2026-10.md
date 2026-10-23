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

## Results (run 2026-10-23, after the pre-registration commit 7a8d6f9)

**Verdict: both fail the gate. The day-trading account stays off.**

All figures are a $10,000 start over 2016-03 to 2026-10. SPY buy-and-hold over the same span: +360.5%, +15.4%/yr, worst drop −35.1%, Sharpe 0.93.

| | Total | Per year | Worst drop | Sharpe | Round trips | Win rate | Avg trade | Kill switch |
|---|---|---|---|---|---|---|---|---|
| Late momentum, 5 bps | −51.7% | −6.6% | −51.8% | −4.08 | 2,840 | 26% | −10.2 bps | 3× |
| Late momentum, 1 bp (diag.) | −15.6% | −1.6% | −15.8% | −0.97 | 2,890 | 43% | −2.3 bps | 0 |
| Opening drive, 5 bps | −36.3% | −4.1% | −37.4% | −1.52 | 2,769 | 39% | −6.4 bps | 2× |
| Opening drive, 1 bp (diag.) | +4.4% | +0.4% | −9.7% | 0.15 | 2,810 | 49% | +0.7 bps | 0 |

### Gate
| Gate | Late momentum | Opening drive |
|---|---|---|
| 1 history | pass | pass |
| 2 walk-forward | **fail** (Sharpe>0 in 0% of windows) | **fail** (0%) |
| 3 OOS vs SPY | **fail** (Sharpe −5.92 vs 1.27) | **fail** (Sharpe −1.53 vs 1.27; drawdown ok) |
| 4 trades | pass | pass |
| 5 sensitivity | pass | pass |

### Reading
- **Late momentum has no edge** in this data: the average trade before costs is about 0 bps. Published intraday-momentum effects measure from the *previous close*. They have also been reported to weaken after publication, and here they did not show up.
- **Opening drive has a gross edge of about 2–3 bps a trade.** That is positive, but even at a realistic 1 bp spread it makes +0.4%/yr against SPY's +15%/yr.
- Cutting trades to about 270 a year did reduce the damage: −4 to −7%/yr instead of −19%/yr. But there is no edge left to keep.
- Across both rounds, four pre-registered day-trading rules have been tested. None comes close to buy-and-hold. Holding SPY all day captures the market's rise; these rules sit in cash most of the day and miss it.
