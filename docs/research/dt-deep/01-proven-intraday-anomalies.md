# DT deep-dive 01: academically documented intraday / short-horizon anomalies (2026-10-23)

Author: strategy-lead. Research only, no code. Angle: the best-documented anomalies, scored for how well they fit OUR system.

**Evidence labels.** `[V]` = I confirmed it in a source fetched/searched this session (URL given). `[M]` = from my memory of the literature, not re-verified; treat as a lead, check before pre-registering.

## Our constraints that decide feasibility
- Long-only, no leverage, no options, no shorting. Any "long-short" anomaly keeps at most its long leg, usually the weaker half.
- The day-trading account flattens at the close. Anything that holds overnight (overnight drift, FOMC drift, earnings premium, 1-5 day reversal) needs a **small schema addition: an account/sleeve that may carry positions past 16:00**. The daily-bar swing accounts presumably already do; the 15m intraday engine does not.
- Signal on bar t fills at bar t+3 open (45 min lag). Anything that needs a fill within minutes of an event is mis-modelled. Rules keyed to a fixed clock time (close, open) are fine because the signal is known earlier.
- 5 bps slippage per side = 10 bps per round trip. At 250 trades/yr that is 25%/yr of drag. **Any rule must show roughly >12 bps gross per round trip at daily frequency to survive.** Our own SPY/QQQ opening drive made ~2.7 bps gross.
- Cash account, T+1: fine for one position per day in an ETF; limits stock sleeves to about 5 entries per day.

## Summary ranking (evidence strength x feasibility)

| Rank | Methodology | Evidence | Post-publication | Trades/yr | Feasibility in our system | Risk | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | Short-term reversal (1-5 day), long leg, large caps | Strong, decades [V/M] | Weakened; still some in large caps | 100-600 | Daily bars, overnight hold: small schema addition if not already supported | Med | Test first |
| 2 | Pre-FOMC drift (SPY, hold ~24h before announcement) | Strong in-sample, 1994-2011 [V] | Largely vanished after 2015 [V] | ~8 | Overnight hold + FOMC date list (static data) | Low | Cheap test; expect fail |
| 3 | Overnight drift (buy close, sell open, SPY/QQQ) | Very strong gross [V] | Persists gross; costs kill it [V] | ~250 | Overnight hold; costs decisive | Med | Test with realistic cost; likely marginal |
| 4 | Turn-of-month (SPY, last day to ~3rd day) | Moderate, mixed decay [V] | Contested | ~12 round trips | Fits as-is on daily bars | Low | Cheap test, but may only reduce exposure vs buy-and-hold |
| 5 | Zarattini-Aziz-Barbon intraday momentum, "noise area" (SPY), long side only | Claimed Sharpe 1.33 net [V] | Too new; third-party replication weaker [V] | 60-120 | 15m bars ok; long-only halves the rule; 45 min lag hurts | Med-High | Worth one pre-registered test |
| 6 | Earnings announcement premium (hold stock into earnings) | Moderate [V] | Mixed; strongest in small caps [V] | ~1,000-2,000 events | **New data: earnings calendar**; overnight hold | Med | Needs data purchase/scrape |
| 7 | ORB on "stocks in play" (relative volume), long side | Paper Sharpe 2.8 [V, secondary] | Independent replication: net zero [V] | 500-3,000 | 15m bars ok; paper uses 5m bars and leverage | High | Low odds |
| 8 | Overnight-gap fade, long gap-downs, exit at close | Reversal documented [V] | Gone in S&P 500 large caps [V] | 200-1,000 | Fits | Med | Low odds |
| 9 | Baltussen et al. last-half-hour hedging momentum | Strong across 60 futures [V] | Already failed in our round 2 | ~250 | Fits | Med | Do not re-propose |
| 10 | Heston-Korajczyk-Sadka same-half-hour periodicity | Strong stats [V] | Unknown | Thousands | Cross-sectional long-short; costs fatal | High | Reject |
| 11 | Post-earnings announcement drift, short horizon | Classic, but... | Dead in large caps since ~2006 (contested) [V] | 500+ | New data: earnings surprise | Med | Reject for S&P 500 |
| 12 | Intraday pairs / stat-arb | Moderate (daily pairs) [V] | Declining | Thousands | **Needs shorting** | High | Infeasible |
| 13 | VWAP trend / reversion | No standalone academic evidence | n/a | n/a | Fits | Med | Use VWAP only as an exit inside #5 |
| 14 | MOC/closing-auction imbalance | Documented [M] | Unknown | ~250 | **New data: imbalance feed** | Med | Infeasible today |

Honest overall read: across 14 ideas, **none** is a strong candidate to beat SPY buy-and-hold after 10 bps round-trip cost. The strongest evidence belongs to effects that are either (a) cost-sensitive at daily frequency (overnight), (b) decayed (FOMC, PEAD, gap fade), or (c) have only a long leg available to us. The best use of this list is a small number of cheap, pre-registered, falsifiable tests, not a search for a winner.

---

## 1. Short-term reversal (1-5 day), long-only large-cap version
- **Rule:** Each day at close rank the S&P 500 by prior 1-day (or 5-day) return, exclude stocks with news-like moves (e.g. > 3 sigma) optionally; buy the worst decile (long leg only), hold 1-5 days, equal weight.
- **Evidence:** Lehmann (1990), Jegadeesh (1990), Lo-MacKinlay (1990) [M]. Nagel, "Evaporating Liquidity," RFS 2012: reversal profits are the payoff to liquidity provision, time-varying, predictable by VIX [V: https://nber.org/papers/w17653]. A working paper on large stocks reports 27 bps per 5-day hold (about 13.5%/yr gross, Sharpe 0.6) for the long-short decile spread [V: https://business.columbia.edu/sites/default/files-efs/pubfiles/11568/str1.pdf]. Collin-Dufresne and Daniel (preliminary): for the 100 largest firms only about 10% of shocks are temporary, half-life about 2.5 days [V, same source family]. De Groot-Huij-Zhou: works net of costs only when restricted to larger stocks [V via Quantpedia: https://quantpedia.com/strategies/short-term-reversal-in-stocks].
- **Gross vs net:** Gross about 13% on the spread; the long leg alone carries maybe half of it. Net of 10 bps round trip and 100+ trades a year, probably near zero. Khandani-Lo (2007) documented a steep decline in efficacy [V].
- **Decay:** Yes, large-cap reversal has weakened since the 2000s; the return is concentrated in stress periods (2008, 2020) [V, Nagel].
- **Trades/yr:** with a 3-day hold and 5 positions, about 400-600 round trips. Using a 5-day hold: about 250.
- **Data:** daily bars we already have.
- **Feasibility:** fits on daily bars; the signal at close, fill at next open (no lookahead). Needs overnight holds (swing sleeve). Not "day trading", but it is the swing alternative requested.
- **Risk:** Medium. Buys falling knives; correlated drawdowns in crashes.
- **Pre-registered test:** S&P 500 (current list, flagged survivorship-biased), 2016-2026 daily. Signal at close t: 5-day return, buy the 5 lowest (long-only), fill t+1 open, sell at t+6 open (5-day hold), 5 bps/side. Variation fixed now: also a version gated by VIX above its 252-day median (Nagel). No parameter tuning. Gate: net Sharpe above SPY's over the OOS 30%, and gross mean per trade above 15 bps. Honest prior: fail on net.

## 2. Pre-FOMC announcement drift
- **Rule:** Buy SPY at the close ~1 trading day before a scheduled FOMC announcement (2:00 pm), sell shortly before the announcement (~2pm). About 24 hours long.
- **Evidence:** Lucca and Moench (JF 2015): about 49 bps average excess return in the 24h before announcements, 1994-2011 [V: https://conference.nber.org/conf_papers/f64395/f64395.pdf]. Kurov-Wolfe-Gilbert extend to 2019: drift **essentially disappeared after 2015** [V: https://www.skidmore.edu/economics/documents/KurovWolfeGilbert-TheDisappearingPre-FOMC-Announce-Drift-200914.pdf]. Neuhierl-Weber find a different post-announcement "monetary momentum" [V via VoxEU].
- **Gross / net:** 49 bps x 8 meetings = about 4%/yr gross in-sample with only ~8 trades, so cost is trivial. Post-2015 about zero.
- **Trades/yr:** 8. Statistical power is very low (about 85 events in 10.6y).
- **Data:** Fed meeting calendar (static, hand-enterable); 15m SPY bars.
- **Feasibility:** needs an overnight-hold sleeve and a small calendar table. Fine.
- **Risk:** Low per trade, tiny capital utilisation.
- **Pre-registered test:** SPY, 2016-2026 FOMC scheduled meetings (about 85). Buy at the 15:45 bar of the day before (fill 16:00 equivalent: bar close), sell at the 13:30 bar of announcement day. Verdict is statistical: mean return and t-stat, split pre/post 2020. Expect nothing; the value is closing the question. Not a portfolio strategy by itself (idle 97% of the time).

## 3. Overnight drift (close-to-open)
- **Rule:** Buy SPY (or QQQ) at the close, sell at the next open; flat intraday.
- **Evidence:** Cliff-Cooper-Gulen (2008), Kelly-Clark (2011), Lou-Polk-Skouras JFE 2019 [V: https://researchonline.lse.ac.uk/id/eprint/87481]. Practitioner figure: $1 held overnight in SPY for 30 years grew to about $17 vs about $1.20 intraday [V: https://www.advisorperspectives.com/articles/2025/03/11/working-night-shift-still]. Alpha Architect: SPY 1993-2020, overnight beats intraday even net of spread and $0.01/share commission but with low statistical robustness [V: https://alphaarchitect.com/trading-costs-wipe-out-the-overnight-return-anomaly/]. QuantConnect: fees and slippage cancel the edge for a daily strategy [V: https://www.quantconnect.com/research/15296/overnight-anomaly/].
- **Lou-Polk-Skouras "tug of war" extension:** momentum profits accrue overnight, most other anomalies intraday; clienteles differ [V]. Alpha Architect's own tests found no support for a distinct intraday-momentum result [V]. Cross-sectional version is long-short, so not usable for us.
- **Decay:** The raw SPY overnight premium persists in the sample, but it is mostly a bull-market feature; it is the whole equity premium compressed into the night.
- **Trades/yr:** 250.
- **Math for us:** overnight SPY has averaged roughly 3-5 bps per night in recent decades [M]. Costs at 5 bps per side are 10 bps per night. **Mathematically cannot work at our default cost**; at a realistic 0.5 bp/side (auction fills), net maybe +2-4 bps per night.
- **Data:** SPY/QQQ 15m or daily.
- **Feasibility:** overnight sleeve; fills at the close/open auction (proxy: last bar close, first bar open). The 3-bar lag is irrelevant because the clock-based signal needs no information.
- **Risk:** Medium (gap risk, concentrated in one asset).
- **Pre-registered test:** SPY, buy at 16:00 bar close, sell next 09:30 open, 2016-2026. Report at 5 bps (verdict), 1 bp and 0.5 bp (diagnostics only), and the version "only when SPY closed above its 200-day average". Gate against SPY buy-and-hold. Expected to fail at 5 bps by arithmetic; the real question is whether anyone can fill at < 1 bp.

## 4. Turn-of-the-month (TOM)
- **Rule:** Hold SPY from the close of the last trading day of the month through the close of the 3rd trading day of the next month; cash otherwise.
- **Evidence:** Ariel (1987), Lakonishok-Smidt (1988), McConnell-Xu (2008) [M]. Maberly-Waggoner (Atlanta Fed WP 2000): effect disappears after 1990 in S&P futures [V: https://www.atlantafed.org/research/publications/wp/2000/11.aspx]. A practitioner note based on data through Q3 2024 claims it persists and that holding only four TOM days earns an annual return only 0.89% below being invested every day [V secondary: https://www.etftrends.com/etf-strategist-channel/turn-month-effect/]. Read that as: TOM is a good idea for **reducing exposure**, not for beating buy-and-hold.
- **Trades/yr:** 12 round trips; at 10 bps each that is about 1.2%/yr of cost. Affordable.
- **Data:** daily SPY; trading calendar.
- **Feasibility:** fits as-is on daily bars (if the daily account allows multi-day holds; schema same as above).
- **Risk:** Low. Sits in cash ~80% of days; it will badly trail SPY in a bull market (an explicit Rule 4 violation risk: "measured against buy-and-hold").
- **Pre-registered test:** SPY daily 2016-2026, buy at close of the last trading day, sell at close of day +4 (about 4 days), 5 bps. Compare return per unit of time invested against SPY, and total return vs SPY. Expect: higher Sharpe per day invested, lower total return. Not a fail of the idea but fails the "beat SPY" gate; consider reporting as an exposure-scaled comparison.

## 5. Intraday momentum with "noise area" and VWAP trailing stop (Zarattini-Barbon-Aziz)
- **Rule (paper):** For SPY, compute the average absolute move from the open at each time of day over the prior 14 days; the "noise area" is open +/- that move (adjusted for overnight gap). Check every 30 minutes; if price breaks above the upper boundary go long, below the lower go short; trailing stop at the boundary or VWAP, whichever is tighter; flat at the close [V: https://concretumgroup.com/exclusive-interview-with-carlo-zarattini-combining-quantitative-and-discretionary-trading/, https://de.tradingview.com/script/CUpWCZhe-Concretum-Bands].
- **Evidence:** SFI Working Paper 24-97: 2007 to early 2024, net total return 1,985%, 19.6% annualised, Sharpe 1.33 [V: https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy]. Not peer reviewed. Leverage up to 4x is, to my memory, part of the headline [M]. A third-party TradingView implementation found smaller returns and a low Sharpe [V, anecdotal].
- **Related published basis:** Gao-Han-Li-Zhou JFE 2018: first half-hour return (from previous close) predicts the last half-hour; R^2 about 2%; 1993-2013 [V: https://profiles.wustl.edu/en/publications/market-intraday-momentum/]. Baltussen-Da-Lammers-Martens JFE 2021: 60+ futures, 1974-2020, driven by option-dealer and leveraged-ETF gamma hedging [V: https://pure.eur.nl/en/publications/hedging-demand-and-market-intraday-momentum/]. Our round 2 "late momentum" test used an in-day return rather than the from-previous-close return and found nothing. The paper's version is different (previous-close anchored bands, many entries earlier in the day), so it is **not** the same rule, but the evidence of decay for the family is a worry.
- **Skeptic flags:** long and short sides combined; only half is available to us. Leverage inflates headline return. Out-of-sample = nothing since publication yet. Costs in the paper are described as commission-based [M].
- **Trades/yr:** the paper trades on perhaps a third to half of days: 60-120 long-side round trips.
- **Data:** SPY 15m bars (have). The paper uses 1-minute or 30-min; use 30-min boundaries from our 15m bars.
- **Feasibility:** fits as-is except long-only restriction. Lag: decision at a 30-min boundary fills 45 min later; this erodes a breakout rule materially. Flag it in the test, do not fudge it.
- **Risk:** Medium-high; whipsaw days.
- **Pre-registered test:** SPY 2016-03 to 2026-10. Noise area: average over prior 14 days of |price at time-of-day / open - 1|, multiplier 1.0, bands anchored on max(open, prev close) for the upper bound. Evaluate at 10:00, 10:30, ..., 15:30. Enter long at first close above upper band (fill at t+3 bars, our lag); exit at close below VWAP or the upper band, or 16:00. One entry per day. Costs 5 bps (verdict), 1 bp (diagnostic). Report separately: trade count, gross bps per trade, and long-only vs. what a short side would have added (diagnostic from the same data, never traded). Gate as before.

## 6. Earnings announcement premium
- **Rule:** Buy a stock at the close before its scheduled earnings date, sell at the next close (or open).
- **Evidence:** Frazzini and Lamont (NBER w13090): stocks earn abnormal returns around scheduled announcement dates, linked to attention-driven retail buying and volume surges [V: https://www.nber.org/papers/w13090]. Global out-of-sample study: more than 11% annualised, 1991-2010, strongest in the smallest firms, a contrast to the US finding that the largest had the largest premium [V: https://alphaarchitect.com/2016/09/28/introducing-the-global-earnings-announcement-premium/]. Evidence of arbitrageurs competing it away [V].
- **Decay:** unclear; small-cap effect. In S&P 500 names the premium is likely a few bps to tens of bps, with huge idiosyncratic variance (earnings gaps of +/-5%).
- **Trades/yr:** with 500 stocks x 4 = 2,000 candidate events; at ≤5 slots per day that is capped (earnings cluster in a few weeks).
- **Data:** **new: historical earnings dates** (yfinance gives a patchy `earnings_dates`; a paid source or SEC 8-K scrape is better; not currently in the system). Survivorship issues for dates of delisted names.
- **Feasibility:** needs the new table and an overnight sleeve.
- **Risk:** Medium-high per trade (gap risk), diversifiable.
- **Pre-registered test:** current S&P 500 names, 2016-2026, buy at close before announcement (BMO announcers: close two days before... define by announce time flag), sell at the following close, equal weight, up to 5 per day, 5 bps. Report average abnormal return vs same-day SPY. Gate: abnormal return > 10 bps net, t > 2, positive in both halves.

## 7. Opening-range breakout on "stocks in play" (Zarattini-Barbon-Aziz 2024)
- **Rule:** Each day pick the top 20 stocks by relative volume in the first 5 minutes (volume / 14-day average), price above $5 and average volume above 1M. Trade the direction of the first 5-min candle: enter at the second candle's open, stop at the opposite end of the first candle, exit at the close; position sized by risk.
- **Evidence:** Paper: top-20 version 1,637% total, 41.6% annualised, 2016-2023, Sharpe about 2.8, near-zero beta [V, secondary: https://www.letsdatascience.com/news/traders-adopt-orb-strategy-for-stocks-in-play-19394da2]. **The headline uses leverage and, per an independent review, charges commission but no spread/slippage and has no out-of-sample** [V: https://www.mql5.com/en/blogs/post/776235, which is for the related Zarattini-Aziz index ORB]. That replication: gross +0.13R per trade on NQ reproduced, net of spread/slippage about 0R, negative on four of five indices, and negative in all markets in 2015-2017 [V]. Cost was 6-12% of risk per trade.
- **Why it differs from our failed round 1:** our breakout was a plain opening-range rule on all 500 stocks with no volume filter and 15-min bars. This one filters hard on volume (picks news/earnings stocks, where costs are higher, not lower) and uses 5-min candles and a first-candle stop.
- **Feasibility:** we have 15m bars, so "first candle" becomes first 15 min; changes the rule. Can be done at long side only (about half of trades). 45 min lag more or less destroys the entry timing (entry at 10:15 instead of 9:35).
- **Risk:** High; our prior is fail. 
- **Pre-registered test:** if wanted: top 10 stocks by 09:30-09:45 relative volume (vs 14-day average of same bar), long-only when the first 15m bar is up; fill at t+3 (10:15 open); stop at first bar low (checked on bar closes); flat at 16:00; 5 bps. Expect: edge competes with 45-min lag. Low priority; I would not spend an agent on this.

## 8. Overnight-gap fade (long gap-downs)
- **Rule:** Buy S&P 500 stocks that opened down more than X% (X = 1.5 sigma of 20-day daily vol) vs prior close; sell at the close.
- **Evidence:** Overnight-to-intraday reversal: Berkman-Koch-Tuttle-Zhang (2012): retail-driven opening price pressure [V via secondary]; but Branch and Ma review 2011-2017 shows it weakened and disappeared among S&P 500 stocks, persisting in small stocks [V: https://www.cxoadvisory.com/?p=31761]. A 2011-2024 thesis reports robust reversal across all years but sample/universe not clear [V, weak: https://www.utupub.fi/bitstreams/88057d2a-1ee8-4c05-9592-c535a21b502c/download]. Practitioner Sharpe about 2 is gross [V, discount].
- **Decay:** yes for large caps.
- **Trades/yr:** 200-1,000.
- **Feasibility:** fits as-is. Entry lag a problem: the 3-bar lag fills at 10:15, after most of the opening reversal has happened; that alone could eat the edge.
- **Risk:** Medium; gap-downs are often real news (earnings), where continuation is likelier.
- **Pre-registered test:** 501 stocks, signal from the 09:30 bar's open vs prior close, require no earnings that day (needs the earnings table from #6, else skip the filter and report it), buy fill at t+3, sell 16:00; 5 bps. Gate gross > 15 bps per trade.

## 9. Last-half-hour hedging momentum (Baltussen et al.)
Already tested as `etf_dt_late_momentum`; no edge. The differences from the paper (they use the rest-of-day return in index futures, 60 markets) matter little to the verdict. Listed only so nobody re-proposes it. Possible untested variant: condition on high VIX or option-expiry days when gamma hedging is largest; this is a new rule with about 40 trade-days a year, but the same costs-vs-edge issue (published effect size is R^2 of a few per cent).

## 10. Same-half-hour periodicity (Heston-Korajczyk-Sadka)
- **Evidence:** JF 2010, NYSE stocks 2001-2005: returns in a half-hour predict returns in the same half-hour on following days for 40+ days; not explained by size, volume, or S&P membership [V: https://ar5iv.arxiv.org/html/1005.3535]. Short-run (sub-hour) reversal from bid-ask bounce.
- **Why rejected:** it is a cross-sectional long-short signal that requires trading many names every half hour; the edge is a few bps, and the sample (2001-2005, pre-decimal-era microstructure, pre-HFT) is not representative. Cost would swamp it at our 10 bps round trip. Long-only reduces to a noisy filter. No test recommended. (Its one useful message: time-of-day effects are persistent per stock, which could be a feature for something else.)

## 11. Post-earnings announcement drift (PEAD), short horizon
- **Evidence:** classic Bernard-Thomas [M]. Martineau, "Rest in Peace PEAD": gone in large stocks since 2006, driven by decimalisation and HFT [V: https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again]. Two 2025 accepted papers contest this; the dispute turns on research design [V, same source; not read].
- **Feasibility:** needs earnings surprise (EPS vs consensus) data we do not have; the S&P 500 is exactly the segment where it died. Reject unless the data purchase happens for #6 anyway; then a 1-5 day post-announcement test is cheap.

## 12. Intraday pairs trading / statistical arbitrage
- **Evidence:** Gatev-Goetzmann-Rouwenhorst (RFS 2006): up to 11% excess per year, 1962-2002 daily data [V: https://nber.org/papers/w7032]; returns lower post-1988 and in later replications; 2020-2024 Finnish study: smaller and shrinks in liquid stocks [V]. Intraday versions: I did not find a source I could verify (Krauss 2017 survey [M]).
- **Infeasible:** every pair needs a short leg. A long-only "buy the laggard of the pair" is exactly short-term reversal (#1). Reject as pairs; covered by #1.

## 13. VWAP trend / reversion
- No standalone peer-reviewed edge found in this search. The credible use is as a trailing exit inside #5. As a standalone "price vs VWAP" rule it is a restatement of dip-buying / breakout that already failed. Not recommended.

## 14. Closing-auction (MOC) imbalance
- Idea: published closing imbalances predict the final-minute move. Needs NYSE/Nasdaq imbalance feed data (paid, not in the system); 15m bars cannot see it. Not verified in this session [M]. Infeasible with current data.

---

## Cross-cutting observations
1. **The 45-minute fill lag is itself a design constraint**, not a footnote. Anything whose edge lives in the minutes after the signal (ORB, gap fade, breakout) is structurally handicapped. Clock-keyed rules (overnight, TOM, FOMC, day-before-earnings) are immune. This favours the overnight/multi-day families.
2. **Costs.** Our 5 bps/side is conservative for SPY/QQQ (real spread about 0.1-0.5 bp) but fair for mid-cap stocks. If only index ETFs are traded, the 1 bp diagnostic should be reported next to the verdict, but must never rescue a fail (existing protocol).
3. **Beat-buy-and-hold is the hard constraint (Rule 4).** Clock-based partial-exposure strategies (TOM, FOMC, overnight-only) can only beat SPY by being much higher return per unit time; they will almost certainly look good on Sharpe-per-exposure and bad on total return. Decide in advance whether the gate for those is "risk-adjusted per exposure" (and if so, an ADR) rather than discovering this after the run.
4. **Survivorship** (current S&P 500 list) flatters stock-level rules (#1, #6, #7, #8). ETF rules (#2-#5) are clean.
5. **Effect sizes.** Documented bps per trade for these effects are typically 2-30 bps gross; the break-even at 10 bps round trip is the cut line. Only #1 (long leg of reversal, in stress regimes) and #5 (if the paper's claim holds) plausibly clear it.

## Suggested minimal programme (if the PM wants to test anything)
- Wave A, cheap: #1 (daily reversal, 1 engineer), #3 + #2 + #4 together as one "calendar/overnight ETF" sleeve study (needs only the hold-overnight flag and a FOMC date list).
- Wave B, only if #1 shows gross edge > 15 bps: add the earnings table and run #6/#8.
- #5 is the single intraday rule worth a pre-registered run, since it differs materially from the failed round-2 rules.
- Do not spend effort on #7, #9-#14.

## Source list (all fetched/searched 2026-10-23)
- Zarattini-Barbon-Aziz, SFI 24-97: https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy
- Concretum interview / bands: https://concretumgroup.com/exclusive-interview-with-carlo-zarattini-combining-quantitative-and-discretionary-trading/ ; https://de.tradingview.com/script/CUpWCZhe-Concretum-Bands
- ORB stocks in play summary: https://www.letsdatascience.com/news/traders-adopt-orb-strategy-for-stocks-in-play-19394da2 ; replication: https://www.mql5.com/en/blogs/post/776235
- Lou-Polk-Skouras: https://researchonline.lse.ac.uk/id/eprint/87481 ; https://alphaarchitect.com/2015/02/overnight-momentum-vs-intraday-momentum/
- Overnight costs: https://alphaarchitect.com/trading-costs-wipe-out-the-overnight-return-anomaly/ ; https://www.quantconnect.com/research/15296/overnight-anomaly/ ; https://www.advisorperspectives.com/articles/2025/03/11/working-night-shift-still
- Gao et al.: https://profiles.wustl.edu/en/publications/market-intraday-momentum/ ; Baltussen et al.: https://pure.eur.nl/en/publications/hedging-demand-and-market-intraday-momentum/
- Heston-Korajczyk-Sadka: https://ar5iv.arxiv.org/html/1005.3535
- Pre-FOMC: https://conference.nber.org/conf_papers/f64395/f64395.pdf ; https://www.skidmore.edu/economics/documents/KurovWolfeGilbert-TheDisappearingPre-FOMC-Announce-Drift-200914.pdf ; https://voxeu.org/article/predictable-movements-asset-prices-around-fomc-meetings
- Short-term reversal: https://nber.org/papers/w17653 ; https://business.columbia.edu/sites/default/files-efs/pubfiles/11568/str1.pdf ; https://quantpedia.com/strategies/short-term-reversal-in-stocks
- Turn of month: https://www.atlantafed.org/research/publications/wp/2000/11.aspx ; https://www.etftrends.com/etf-strategist-channel/turn-month-effect/
- PEAD: https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again
- Earnings premium: https://www.nber.org/papers/w13090 ; https://alphaarchitect.com/2016/09/28/introducing-the-global-earnings-announcement-premium/
- Gap reversal: https://www.cxoadvisory.com/?p=31761
- Pairs: https://nber.org/papers/w7032
