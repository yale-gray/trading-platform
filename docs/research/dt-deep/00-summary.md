# Day-trading deep research: summary and test list (2026-10-23)

This summary merges five lead reports:

| # | Report | Lead | Covers |
|---|---|---|---|
| 01 | [01-proven-intraday-anomalies.md](01-proven-intraday-anomalies.md) | strategy-lead | Proven anomalies |
| 02 | [02-event-and-data-driven.md](02-event-and-data-driven.md) | data-lead | Event and data-driven trading |
| 03 | [03-high-risk-instruments-and-costs.md](03-high-risk-instruments-and-costs.md) | execution-lead | High-risk instruments and costs |
| 04 | [04-cutting-edge-ml-ai.md](04-cutting-edge-ml-ai.md) | platform-lead | ML and AI |
| 05 | [05-evidence-and-testing-protocol.md](05-evidence-and-testing-protocol.md) | quality-lead | Evidence quality and testing protocol |

The leads worked from abstracts and secondary sources, and some figures are from memory. Each report tags claims as verified or memory: check the tag before pre-registering. This is education and system design, not financial advice.

## What the research says, in five lines
1. **Base rate.** About 97% of persistent retail day traders lose money (Taiwan, Barber et al.; Brazil, Chague et al. 2019). Retail 0DTE and CFD studies agree. Our 4 failures are the expected outcome.
2. **The popular intraday papers are not independently replicated.** One outside replication of opening-range breakout found the gross edge but about zero net of costs, the same as our result.
3. **Published edges shrink.** They fall about 26% out of sample and about 58% after publication (McLean & Pontiff). Most surviving short-term edges sit in small caps, overnight holds or short selling, all of which our day-trading account excludes.
4. **Our 5 bps/side cost is about 5-10x too high for SPY/QQQ, but costs are not the only problem.** At 1 bp, the best round-2 rule made +0.4%/yr against SPY's +15%/yr.
5. **Testing a list of ideas will "find" a winner by luck unless corrected for.** The fix: freeze the list, count every trial (the 4 failures included), use a deflated Sharpe ratio, and keep a final hold-out that is touched once.

## Prerequisites (do before any new test)
| # | Item | Why | Effort |
|---|---|---|---|
| P1 | **Per-instrument cost table** (see the table below) | A flat 5 bps decided past verdicts for ETFs; small stocks may cost more | S |
| P2 | **Gate additions:** trial registry (N), deflated Sharpe, Holm across candidates, a higher minimum trade count | Stops a lucky winner from passing | S-M |
| P3 | **Final hold-out:** develop on 2016-2023, open 2024-2026 once for at most 2 finalists | Round 2 already saw 2016-2026 for SPY/QQQ | S |
| P4 | **Two benchmarks:** absolute edge vs T-bills (t-stat), *and* value as an overlay on holding SPY | A part-time strategy can have a real edge yet lose to always-invested SPY | S (ADR) |
| P5 | **Overnight-hold ("swing") account** in addition to flatten-at-close | 5 of the top 8 ideas need it; reports 01, 02 and 04 all asked | M (ADR) |

Proposed costs, per side (report 03; some figures are estimates). Regulatory fees add about 0.25 bp on sells; costs double in the first and last 15 minutes.

| Instrument | Base | Stress |
|---|---|---|
| SPY, QQQ | 1 bp | 2 bp |
| Mega-cap stocks | 1.5 bp | 3 bp |
| Rest of S&P 500 | 3 bp | 6 bp |
| Leveraged/inverse ETFs | 3 bp | 5 bp |
| BTC/ETH | 15 bp | 30 bp |

## Ranked methodologies to test

### Tier A: test next (cheap; fits as-is, or needs only P5 or a small addition)
| Rank | Methodology | Rule in one line | Evidence | Needs | Prior odds |
|---|---|---|---|---|---|
| A1 | **Short-term reversal (1-5 days), long leg** | Buy the S&P 500 stocks that fell most over the last 1-5 days, hold a few days | Strongest, decades (Lehmann; Lo-MacKinlay; Nagel 2012) | P5, daily bars | Low-medium: weakened in large caps |
| A2 | **Overnight drift** | Buy SPY/QQQ at the close, sell at the next open | Very strong gross (Lou-Polk-Skouras; Kelly-Clark) | P5, P1 | Gross yes; net is cost-decided |
| A3 | **Noise-area intraday momentum, long only** | Buy SPY when it breaks above its usual intraday move range for that time of day, trailing VWAP exit | Zarattini-Aziz-Barbon 2024, claimed Sharpe 1.33 net; unreviewed | New indicator (time-of-day "noise band"), VWAP | Low: long side only, 45-min lag |
| A4 | **Gap-down reversal** | Buy S&P 500 stocks that open sharply down, sell at the close | Berkman et al. 2012; weak for large caps | Fits now (15m) | Low |
| A5 | **Turn-of-month** | Hold SPY from the last trading day to the 3rd | Moderate, contested decay | Fits now (daily) | Edge per day yes; beats SPY overall no |
| A6 | **Pre-FOMC / macro-day drift** | Hold SPY the ~24h before Fed announcements | Strong 1994-2011; vanished after 2015 | P5, FOMC date list | Low: about 85 events |

### Tier B: needs a bigger build (one at a time, if Tier A shows anything)
| Rank | Methodology | Needs | Effort | Note |
|---|---|---|---|---|
| B1 | **ML market timing from the cross-section:** lagged 15m returns of 500 stocks predict SPY/QQQ | Model-score indicator column, walk-forward retraining | M | Best fit to the cost problem (Aleti-Bollerslev-Siggaard 2025, net of costs) |
| B2 | **Regime filter** (volatility percentile, then HMM) on a rule with gross edge, e.g. opening drive | Indicator column | S | A filter, not an edge source; null = random day filter |
| B3 | **Stocks-in-play ORB** (top relative volume at the open) | Relative-volume ranker; ideally 5-min bars and pre-market volume | M | Paper Sharpe 2.8, but small caps and no costs; replication about zero net |
| B4 | **LightGBM next-day stock ranking** | ML pipeline, purge/embargo, survivorship control | M | Survivorship leak risk with today's S&P list |
| B5 | **Crypto intraday momentum** (BTC/ETH) | ccxt 15m crypto, 15 bps fees, define the crypto "session" | S-M | Shen-Urquhart-Wang 2022; no net-of-cost test |
| B6 | **Earnings-day reaction** (SEC EDGAR 8-K timestamps) | Event calendar, overnight-hold exemption | M | Most earnings edges have died in large caps |
| B7 | **LLM-scored news** (Alpaca news + Claude score) | News pipeline, LLM budget | M | **Forward paper test only:** backtests before the model's cutoff are contaminated |

### Tier C: high risk, test only as clearly labelled experiments
| Methodology | Why risky | Recommendation |
|---|---|---|
| Leveraged-ETF close rebalancing (TQQQ/SQQQ/SPXL) | 3x ETFs fell 82-91% in 2022; the paper's edge ends in 2011 | Test later; report the worst day |
| Inverse ETFs as a short-selling proxy (SH/SDS/SQQQ) | Decay, tail risk | Only if a short-side idea shows edge |
| Volatility-targeting / Kelly sizing overlay | Can't rescue a negative edge; full Kelly is ruinous | Use only on a rule that already passes |

### Avoid (evidence is weak or negative, or the risk is unacceptable)
- **0DTE options:** retail lost more than $70M, mostly to costs; no free history.
- **Futures, perpetual leverage, crypto funding/basis carry:** big build, liquidation risk.
- **Deep learning on order books, RL/FinRL, LLM "agent" traders:** no data or latency for the first; no independent live evidence for the others.
- **Intraday pairs/stat-arb:** needs shorting.
- **Index-inclusion trades:** decayed to about 0.3%.
- **Short squeezes, Reddit/Robinhood attention, analyst-upgrade trades:** no net edge, or no free data.
- **Short-horizon PEAD in large caps, same-half-hour periodicity, crypto calendar effects, closing-auction imbalance:** dead, cost-fatal or data-mined, or the feed isn't available.

## Account rules note
The $25k pattern-day-trader rule was removed: the SEC approved FINRA's Rule 4210 change on 2026-04-14, effective 2026-06-04, with brokers phasing in until 2027-10-20. Check the broker before relying on it. Our simulated cash account (T+1) is unaffected.

## Recommended next step
1. Do P1-P4 first (one small wave).
2. Decide P5.
3. Pre-register Tier A as one frozen batch:
   - Budget N = 12, counting the 4 past failures.
   - Fixed parameters from the papers.
   - Per-instrument costs.
4. Run the batch on 2016-2023.
5. Open the 2024-2026 hold-out once, for at most 2 finalists.
6. Run 90 days of forward paper trading.
7. Publish every result, including failures.
