# Event-driven and alternative-data day trading (data-lead research, 2026-10-23)

Research only, no code. Angle: which event/data-driven day-trading ideas have real evidence, what data each needs, and whether our system (long-only, cash T+1, flatten at close, signal at bar t fills at bar t+3 open, 5 bps/side) can test them.

## Read this first: three structural facts about our system

1. **Our fill lag is 45 minutes on 15-minute bars.** A signal on bar t fills at bar t+3's open. Most event edges (headline reaction, earnings gap, gap fade) are largest in the first minutes after the event. By the time we fill, most of the move is gone. For any event idea, the pre-registered test must say exactly when the signal is known and when the fill happens. Several ideas below only work if that lag is relaxed for event days (a PM decision, not an engineer's).
2. **Flatten-at-close removes the best-documented event effects.** Earnings announcements mostly land after the close or before the open (a trade-press claim that 95% of US firms report in the overnight session cites Kellogg; unverified). The reaction is an overnight gap we do not capture. Same for FOMC pre-announcement drift (24 hours before). Testing these needs an "event-day overnight hold" exemption.
3. **Long-only drops half of every news edge.** Published news-sentiment and post-earnings edges are largest on the short side (bad news drifts down in small stocks). We can only use the long leg or the "avoid" side.

Evidence-quality labels: **A** = peer-reviewed, long sample, replicated; **B** = peer-reviewed or credible working paper, single sample or decayed; **C** = thesis, practitioner or vendor/blog claim; **U** = from memory, not re-verified this session.

## Summary ranking (evidence x feasibility in OUR system)

| Rank | Methodology | Evidence | Net-of-cost evidence | Data free? | Feasibility in our system | Risk | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | M1 Stocks-in-play opening-range breakout (long leg, S&P 500 only) | B (SSRN 2024; one author team) | Claimed Sharpe 2.8 net of commissions, on a 1,000-stock mixed-cap universe with 1-min bars and 5-min ranges | Mostly (needs pre-market/first-bars volume: verify Alpaca) | Small addition (relative-volume screen) but 15m bars and 45-min lag are a poor match | High | Test, expect weak |
| 2 | M2 Overnight-gap reversal (buy large gap-downs, skip/ignore gap-ups; long-only) | B-/C (one JFQA paper plus a thesis) | Unknown for US large caps | Yes (daily + 15m already held) | As-is for a daily open-to-close version; small addition for a 15m version | Medium | Cheapest test; run first |
| 3 | M3 Earnings-day reaction trade (day-1 gap continuation/fade), using EDGAR 8-K timestamps | B (PEAD decayed; day-1 effects better documented) | Mostly gross | Yes via SEC EDGAR (see data table); yfinance dates are patchy | Small addition (event calendar) plus needs open-fill exemption | High | Worth testing on daily data |
| 4 | M4 LLM-scored news headlines (Alpaca/Benzinga news) | B (Lopez-Lira & Tang, 2023-2025) | Authors say returns fall as LLM use grows; tradable part is the slow drift, mainly small caps and negative news | Likely yes (Alpaca news, back to 2015) but free-tier limits unconfirmed | Small addition plus a hard look-ahead problem (model memorized the past) | Medium | Forward paper test only |
| 5 | M5 Macro-announcement days (FOMC/CPI/jobs) long SPY | A for the old premium; B for decay | Premium does not survive costs on 8 days/yr power | Yes (dates are public) | Needs overnight-hold exemption; otherwise no signal | Low | Cheap, low power; likely dead |
| 6 | M6 Pre-market movers / gap-and-go (premarket volume + catalyst) | C | None credible | Probably (Alpaca SIP includes extended hours; verify our stored bars) | New data pull if we stored regular hours only | High | Fold into M1/M2 |
| 7 | M7 Unusual volume / volatility breakout (non-opening) | B- | Unknown | Yes | As-is | High | Overlaps tested failures |
| 8 | M8 Retail attention / herding fade (Robinhood, Reddit) | B (Barber et al.), C (Reddit) | Mostly a short-side or avoid signal | No (Robinhood popularity data discontinued; Reddit API paid) | Needs new data and shorting | High | Skip |
| 9 | M9 Short-interest / squeeze screens | B for monthly horizons, C for squeezes | None | FINRA files free but twice monthly with a lag | Small addition, but S&P 500 has low short interest | Very high | Skip |
| 10 | M10 Index inclusion / rebalancing | A (decayed to near zero) | Effect ~0.3% for additions last decade | Partly (S&P change lists public; Russell lists not free) | Needs overnight hold | Medium | Skip |
| 11 | M11 Options-driven effects (0DTE gamma, OPEX pinning) | C (mostly vendor) | None | No (gamma exposure is a paid product) | Needs new data | Medium | Skip |
| 12 | M12 Analyst upgrades/downgrades | B (Barber et al. 2001, 2010) | Net of costs ~zero | Partly (Benzinga headlines in Alpaca news) | Small addition | Medium | Skip |
| 13 | M13 Short-horizon PEAD | A, but dead for large caps (Martineau 2022) | No | Needs EPS surprise (not free in clean form) | Holding days, not day trading | Medium | Skip |

## Methodologies in detail

### M1. Stocks-in-play opening-range breakout
- **Rule (plain English):** Each day, rank stocks by how much more volume they trade in the opening minutes than usual (relative volume), keep the top 20, and trade the breakout of the opening range in the direction of the first move, with a stop and a close-of-day exit.
- **Evidence:** Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy for the U.S. Equity Market" (SSRN 4729284; I did not read the paper's text this session because the PDF would not parse). Secondary summaries: a QuantConnect recreation uses the 1,000 most liquid stocks priced above $5 with ATR above $0.50, ranks by volume in the first 5 minutes versus the prior 14 days, trades the top 20 with a 5-minute opening range, and reports a Sharpe of about 2.4 and near-zero beta ([QuantConnect research](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/)). A study of the top 20 stocks in play is quoted at Sharpe 2.81 and 36% alpha; the search result did not tie that figure to the paper, so treat as C. My recollection (U) is a 2016-2023 sample and a reported return over 1,000% gross of leverage, with the strongest results coming from the small, volatile end of the universe and from short trades.
- **Gross / after-cost:** Authors report after-commission figures; spreads for small names are the hole. Independent replications are scarce.
- **Decay:** Too new to know. Published 2024; our own 10.6 years of ORB on S&P 500 names (round 1) showed no gross edge, but that was an unfiltered all-500 breakout, not a stocks-in-play filter.
- **Trades/yr:** About 20 names x 250 days x one entry = up to 5,000 a year in the paper's design. Our cash account caps this near 5 entries a day (about 1,250 a year), which is the same cost regime that killed round 1.
- **Data needed:** Volume in the first N minutes versus a 14-day average; daily ATR and price filter. We hold 15m SIP bars, so the first "bar" is 15 minutes, not 5. Pre-market volume is a better "in play" signal and needs extended-hours bars (verify).
- **Feasibility:** Small addition. Missing pieces: the relative-volume ranker and a stop-order model. Mismatch: 15-minute resolution and a 3-bar fill lag. In S&P 500 only, there are few truly "in play" names and spreads are tight, but the effect lives in volatile small caps we cannot test.
- **Risk:** High (gap risk on stops, cluster losses on market-wide reversal days).
- **Pre-registered test:** Universe = our 501 stocks. Signal known at the close of the 09:30 bar (relative volume of first 15m versus mean first-15m volume of prior 14 sessions; top 5 per day; close above the 09:30 bar high). Fill at the 10:15 bar open (lag 3 bars; no exception). Exit at 15:45 or a stop at the opening-range low. Long only. Report gross and 5 bps net, versus SPY buy-and-hold and versus the same rule on a random 5 stocks per day (control). Gate as in rounds 1 and 2. Success needs a positive gross edge above 10 bps per round trip, otherwise it fails before costs.

### M2. Overnight-gap reversal (gap fade), long-only
- **Rule:** If a stock gaps down a lot at the open versus the prior close, buy the open and sell at the close (expecting partial gap fill). Also useful as a filter: skip buying large gap-ups.
- **Evidence:** Berkman, Koch, Tuttle & Zhang (2012, JFQA 47(4)) find overnight returns are positive and then reverse during the trading day, concentrated in stocks with retail attention and costly to arbitrage, and larger in high retail-sentiment periods ([Cambridge](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/paying-attention-overnight-returns-and-the-hidden-cost-of-buying-at-the-open/F9AAD159B512C651F09D5D52011D88E0)). A University of Turku thesis on US stocks 2011-2024 finds a persistent negative overnight-to-intraday relation in all fourteen years (C: thesis). A Johannesburg Stock Exchange thesis reports profitability after 1.3% per trade of costs (C, wrong market). A 2026 arXiv paper on Nasdaq micro futures found gap-continuation signals fail after frictions ([arXiv 2605.04004](https://arxiv.org/pdf/2605.04004), C, wrong asset).
- **Gross / after-cost:** I found no source giving net returns for US large caps.
- **Decay:** Unknown. The mechanism (retail buying at the open) has plausibly grown with retail share.
- **Trades/yr:** Depends on the gap threshold; 1% gap-downs in S&P 500 stocks may be a few thousand name-days per year, so a cap of 5 a day gives about 1,250.
- **Data needed:** Prior close and the open; both already in our daily data. A 15m version needs the first bar.
- **Feasibility:** As-is for a daily open-to-close test (that ignores our lag by construction, so it measures the edge ceiling). The 15m version with our lag fills at 10:15 and misses the first 45 minutes, where much of the reversal likely happens. Report both.
- **Risk:** Medium. Gap-downs on real bad news keep falling (adverse selection); this is why a news filter (M4) matters.
- **Pre-registered test:** Daily data, 2016-2026. Buy at the open every stock with gap < -2% and no earnings that day (use M3 calendar), sell at the close; max 5 per day, ranked by gap size. Control: all stocks with gap > -0.5%. Report mean open-to-close return in bps against 10 bps round-trip cost; a gross mean under 15 bps ends the test. Then repeat with the 15m lag rule.

### M3. Earnings-day trading (day-1 reaction) and calendar
- **Rule:** After a company reports, trade the direction of the first-day move (continuation) or fade a large gap, entering after the open and flattening at close.
- **Evidence:** The earnings announcement premium (stocks earn more around announcement dates) is documented by Frazzini & Lamont (2007) and globally by Barber, De George, Lehavy & Trueman (2013, JFE) ([summary](https://alphaarchitect.com/2016/09/28/introducing-the-global-earnings-announcement-premium/)); a working paper reports it faded after 2004, with weekly return 23.9 bps lower afterward ([The Disappearing Earnings Announcement Premium](https://www.iimb.ac.in/ARC2020/Papers/The_Disappearing_Earnings_Announcement_Premium.pdf), B-, draft). The premium is a multi-day holding effect, not an intraday one. Day-1 continuation versus fade for large caps: I have no verified source (U: the post-announcement drift literature suggests continuation for large surprises; the attention-reversal literature in M2 suggests fade for attention names). Unresolved.
- **Gross / after-cost:** Premium arbitrage needs about 100% monthly turnover per the same summary, i.e. cost-heavy.
- **Trades/yr:** Each stock reports 4 times a year, so about 2,000 events a year across 501 stocks; about 500 if we take only large surprises.
- **Data needed:** Earnings date AND timing (before open, after close). Free sources: SEC EDGAR 8-K Item 2.02 filings with acceptance timestamps (reliable, free, 2001+; U: I know the submissions API exists but did not test it this session). yfinance `earnings_dates` is patchy: one open-source fetcher reports it is often empty for small or new names and does not reliably say pre or post market ([example fetcher](https://gitea.com/infrastructure_SC/SimplyTrading/src/branch/fix/float-profile-fallback/backend/quorum/earnings_fetcher.py)). Alpaca news timestamps can serve as a cross-check. EPS surprise values are not free in clean historical form.
- **Feasibility:** Small addition (event table from EDGAR). The reaction happens at the open after the report, so with our lag we enter at 10:15 at the earliest; day-1 gap capture requires an exemption for overnight holding or a new "enter at open" mode.
- **Risk:** High (single-stock gap and re-gap).
- **Pre-registered test:** Event table: all S&P 500 8-K 2.02 dates 2016-2026 mapped to the first regular session after the release. Rule: if open-vs-prior-close gap > +3%, buy at the 10:15 bar open (lag rule intact), flatten at 15:45; separate arm for gap < -3% (buy the dip) as the fade version. Control: same rule on non-earnings days with equal gap. Metric: mean return in bps versus 10 bps cost, and versus the control.

### M4. News-sentiment trading (Alpaca news, LLM-scored headlines)
- **Rule:** Score each headline with an LLM (-1 to +1). Buy stocks with strongly positive fresh headlines; avoid or (not for us) short strongly negative ones; hold to the close.
- **Evidence:** Lopez-Lira & Tang, "Can ChatGPT Forecast Stock Price Movements?" (arXiv 2304.07619, versions to October 2025). Abstract (v6): GPT-4 captures the initial market reaction with about 90% portfolio-day hit rate, but that part is not tradable; the scores also predict later drift, especially for small stocks and negative news; forecasting ability rises with model size; strategy returns decline as LLM adoption rises ([abstract](https://arxiv.org/abs/2304.07619)). I could not confirm Sharpe or return magnitudes (not in the abstract). Evidence quality B; the headline claim is older-sample, small-cap, and partly short-side.
- **After-cost:** Not credibly established. Small-cap and negative-news concentration is exactly what a long-only S&P 500 system cannot harvest.
- **Decay:** Explicitly reported by the authors.
- **Trades/yr:** Hundreds to thousands depending on threshold; controllable.
- **Data:** Alpaca News API: historical news back to 2015, ~130+ articles a day, source Benzinga ([launch post](https://alpaca.markets/blog/introducing-news-api-for-real-time-fiancial-news/)). Launch-era free limit was 200 calls a minute; current news-specific limits not stated in docs I could find, and a forum thread asks about missing 2016 news (unresolved). Free Basic market data is IEX-only real-time; historical SIP is free if at least 15 minutes old ([Alpaca forum](https://forum.alpaca.markets/t/extended-hours-errors-and-data/14891) and [docs](https://docs.alpaca.markets/us/docs/about-market-data-api)). LLM scoring cost for ~1.3 million headlines over 10 years is not trivial; Haiku-class scoring is feasible but not free.
- **Feasibility:** Small addition (news ingester, scorer, join to bars). **Hard problem: look-ahead contamination.** A model trained through 2026 has seen how 2016-2024 stories played out, so a backtest on those years is not trustworthy. Lopez-Lira's own v6 had to use post-cutoff headlines for this reason. Our model's cutoff is mid-2026, so clean historical data is almost nil.
- **Risk:** Medium.
- **Pre-registered test:** Do not backtest. Forward-test only: from a committed date, score every Alpaca headline for S&P 500 names live, log the score and timestamp before seeing returns, simulate buying top-decile positive scores at the next 15m bar open plus lag, flatten at close, for at least 6 months. Gate: positive mean net return over a matched random-headline control and over at least 300 events. A cheaper interim: a non-LLM keyword control (e.g. "upgrade", "beats", "raises guidance") to see if news timing alone carries anything.

### M5. Macro-announcement days (FOMC, CPI, jobs)
- **Rule:** Be long SPY into scheduled announcement days (the old "announcement premium"), or only in the afternoon before FOMC.
- **Evidence:** Savor & Wilson (2013, JFQA): 1958-2009 announcement days averaged 11.4 bps excess return versus 1.1 bps otherwise (A). Lucca & Moench (2015, JF): a large share of the equity premium accrues in the 24 hours before FOMC announcements (A). Kurov, Wolfe & Gilbert (2020, Finance Research Letters) find the pre-FOMC drift essentially disappeared after 2015 ([paper](https://www.skidmore.edu/economics/documents/KurovWolfeGilbert-TheDisappearingPre-FOMC-Announce-Drift-200914.pdf)). Option-implied premiums are significantly smaller than realized ones ([search summary of Savor & Wilson follow-ups](https://www.nber.org/papers/w24432.pdf)). I found nothing on post-2015 CPI behavior.
- **After-cost:** About 10 bps a day gross on about 8 FOMC + 12 CPI + 12 jobs days is ~2.4% a year, before the gain we would give up by being out of the market otherwise. Not interesting versus buy-and-hold.
- **Trades/yr:** About 8 to 30.
- **Data:** Dates are public (Fed calendar, BLS schedule); free. Needs no new market data.
- **Feasibility:** Needs overnight hold (the effect is 24 hours before 14:00 ET). An intraday-only version (buy at 10:00, sell at 14:00) has no published support. Statistical power with 8 events a year is poor.
- **Risk:** Low per trade, but also low payoff.
- **Pre-registered test:** SPY close-to-14:00 return on FOMC days 2016-2026 (about 85 days) versus all other days; require t-stat > 2 and mean above 5 bps after 1 bp costs, in both halves of the sample. Expect fail (post-2015 decay).

### M6. Pre-market movers / gap-and-go
- **Rule:** Trade stocks that are up big pre-market on a catalyst and high volume, buying the first pullback or breakout.
- **Evidence:** Almost entirely practitioner (C). One trading-education site claims the typical large gap-up reverses, so gap-and-go is a minority bet on finding the gaps that continue ([site](https://daytradingtoolkit.com/strategies/gap-and-go-day-trading-strategy)); treat as marketing. The only systematic evidence I found points the other way (M2).
- **Data:** Alpaca staff say all data includes extended hours; a forum reply says the IEX feed is thin in extended hours (8:00-17:00 ET) ([forum](https://forum.alpaca.markets/t/extended-hours-errors-and-data/14891)). Whether our stored 15m SIP bars include 04:00-09:30 is unknown to me. This needs a one-symbol, one-day fetch to settle. Full-market SIP pre-market bars should exist; the free plan blocks only the latest 15 minutes.
- **Feasibility:** Small addition if we re-pull with extended hours; this also feeds M1 (pre-market relative volume) and M2 (gap known at 09:29). Not a standalone test.
- **Risk:** High; thin books and wide spreads in the pre-market make the 5 bps cost assumption optimistic.
- **Test:** Fold into M1 as a variant: replace the first-bar volume with pre-market volume.

### M7. Unusual volume / volatility breakouts (not at the open)
- **Rule:** Buy intraday when volume and range jump versus the prior weeks and price breaks the day's high.
- **Evidence:** Related to volume-attention literature and 52-week-high momentum (George & Hwang, U); no verified intraday net result. Round 1's breakout failed on the same bars (no gross edge).
- **Data/feasibility:** As-is. Overlaps with failed tests; a new id would have to be materially different (volume-conditioned only).
- **Risk:** High. **Test:** Only worth running as the control arm of M1.

### M8. Retail attention and herding (Robinhood, Reddit/WSB)
- **Rule:** Fade or avoid stocks that retail traders pile into.
- **Evidence:** Barber, Huang, Odean & Schwarz (2022, JF), "Attention-Induced Trading and Returns: Evidence from Robinhood Users": intense retail buying is followed by lower returns, about -3% over five days for the top herded stocks (-6% for the most extreme), and larger reversals over a month, per secondary summaries ([CXO Advisory](https://www.cxoadvisory.com/?p=34654); B, I did not read the primary text). Bradley, Hanousek, Jame & Xiao (2024, RFS), "Place Your Bets?": WSB due-diligence posts predicted returns before the GameStop episode, and predictability disappeared afterward ([summary](https://academicnewsletter.sufe.edu.cn/info/351572)).
- **After-cost:** The Robinhood effect is multi-day and requires shorting; not a day trade.
- **Data:** Robinhood user-count data was discontinued in 2020 (U). Reddit's API is now paid and Pushshift historical access is restricted (U). Not free.
- **Feasibility:** Needs new data and shorting. A free proxy for attention is abnormal volume and extreme prior-day return (which M2 already uses).
- **Risk:** High. **Test:** none separate; use M2's attention proxy.

### M9. Short-interest and squeeze screens
- **Rule:** Buy heavily shorted stocks showing a volume surge, hoping for a squeeze.
- **Evidence:** Rapach, Ringgenberg & Zhou (2016, JFE) show aggregate short interest predicts market returns monthly (A, but it says high short interest predicts lower market returns, a market-timing signal, not a squeeze screen) ([summary](https://academicnewsletter.sufe.edu.cn/info/356349)). I found nothing credible for squeeze trading after costs; squeezes are rare, lottery-like and small-cap.
- **Data:** FINRA publishes short interest twice a month with a delay (U: from memory, not confirmed in this session). Stale data by construction.
- **Feasibility:** Small addition, but S&P 500 names have low short interest. **Risk:** Very high. **Test:** skip.

### M10. Index inclusion and rebalancing events
- **Evidence:** Greenwood & Sammon (2025, JF), "The Disappearing Index Effect": average abnormal return for S&P 500 additions fell from 7.4% in the 1990s to 0.3% over the past decade; deletions similar ([IDEAS](https://ideas.repec.org/a/bla/jfinan/v80y2025i2p657-698.html)). Pre-announcement front-running and MidCap-to-500 migration explain much of it. Russell reconstitution (late June) is a closing-auction liquidity event; we lack historical Russell membership.
- **Verdict:** The edge is gone and the effective date requires holding overnight. Skip.

### M11. Options-driven effects (0DTE gamma, OPEX pinning)
- **Evidence:** Mostly vendor material. One summary cites a study finding no evidence that higher 0DTE open-interest gamma raises index volatility (it associates with lower intraday volatility); another practitioner analysis reports lower true range in the first hour on high positive open gamma ([RT Gamma](https://rtgamma.substack.com/p/open-gamma-vs-real-time-gamma-which-drives-spx-0dte-intraday-moves-more), C). Vendor claims of "pinning" and dealer-flow support are mechanism arguments, not tests (C). The peer-reviewed paper I would expect on intraday momentum and hedging demand (Baltussen, Da, Marquering) was not retrieved; I recall it as a SPY/ETF last-30-minute effect and our round 2 found no such edge (U).
- **Data:** Gamma exposure is built from options open interest and quotes; not free. **Feasibility:** needs new data. **Verdict:** skip. A free regime variable (volatility index level at the open) is worth adding as a filter only.

### M12. Analyst upgrades and downgrades
- **Evidence:** Barber, Lehavy, McNichols & Trueman (2001, JF): daily-rebalanced consensus-recommendation portfolios earn over 4% annual abnormal gross, which largely vanishes net of trading costs ([IDEAS](https://ideas.repec.org/a/bla/jfinan/v56y2001i2p531-563.html)). Barber, Lehavy & Trueman (2010): 1986-2006 sample, announcement-day abnormal returns for rating changes were small (as summarized by search; magnitudes should be checked in the paper).
- **Data:** IBES not free; Benzinga headlines in Alpaca news contain rating changes and could be parsed.
- **Verdict:** Gross-only evidence. Skip, but it is a free by-product of M4's pipeline if we want to test it later.

### M13. Short-horizon post-earnings drift
- **Evidence:** Martineau (2022, Critical Finance Review), "Rest in Peace Post-Earnings Announcement Drift": for large stocks the drift has been absent since about 2006 ([CFR](https://nowpublishers.com/article/Details/CFR-0122)). Later papers dispute the design, and a 2026 paper argues the decline reflects less informative earnings news rather than more arbitrage ([Sage](https://journals.sagepub.com/doi/10.1177/0148558X261439734)). Contested, but not favorable for S&P 500 names.
- **Feasibility:** Holding days, not day trading; needs EPS surprise. Skip.

## Data sources table

| Data | Source | Free? | In our system today? | Notes |
|---|---|---|---|---|
| 15m regular-hours bars, SIP | Alpaca | Yes (historical SIP free if >15 min old; free plan real-time is IEX-only) | Yes (501 stocks + SPY/QQQ, 2016-2026) | Free plan: 200 historical calls/min ([docs](https://docs.alpaca.markets/us/docs/about-market-data-api)) |
| Pre-market / after-hours bars | Alpaca (same endpoint) | Yes, per staff; IEX feed is thin out of hours | Unknown, likely not stored | Needs a one-day fetch to confirm; needed for M1, M2, M6 |
| Daily bars | yfinance | Yes | Yes | Gap = open/prior close from daily is adequate for M2 |
| News headlines 2015+ | Alpaca News API (Benzinga) | Probably free; limits not confirmed | No | ~130 articles/day; check completeness for 2015-2016 (forum report unresolved) |
| LLM scoring | Anthropic API (Haiku class) | No (cost) | No | Look-ahead contamination for any pre-2026 backtest |
| Earnings dates + timing | SEC EDGAR 8-K Item 2.02 acceptance timestamps | Yes (U: not tested) | No | Most reliable free option; yfinance `earnings_dates` is patchy |
| EPS surprise / consensus | Paid vendors | No | No | Needed only for M13 |
| FOMC/CPI/jobs dates | Fed and BLS calendars | Yes | No | Trivial |
| Short interest | FINRA | Yes, twice monthly with lag (U) | No | Stale for daily use |
| Retail attention (Robinhood, Reddit) | n/a | No | No | Discontinued/paid |
| Options gamma / OI | Vendors (SpotGamma etc.) | No | No | |
| Index change lists (S&P) | Public announcements / Wikipedia | Partly | No | Russell lists not free |
| Analyst ratings | IBES / Benzinga headlines | No / partly | No | |

## Recommended order of work (for the PM)
1. **Data check (cheap, unblocks M1/M2/M6):** fetch one day of SPY and one volatile stock from Alpaca with extended hours; see if pre-market SIP bars exist and whether 15m bars include them.
2. **M2 gap reversal on daily data** (zero new data) to measure the ceiling edge; if gross mean under about 15 bps, stop the whole gap family.
3. **M3 event table from EDGAR**, then run M3 and use it to exclude earnings days from M2.
4. **M1 stocks-in-play** only if (1) shows pre-market data and M2 shows any gross edge.
5. **M4 as a forward paper test** only; start logging scores now so data accumulates while we decide.

Honest prior: our earlier results (four fails, with gross edges of 0 to 3 bps against 10 bps of cost) suggest event ideas will also fail on S&P 500 large caps. The published event edges concentrate in small caps, in short trades and in holding periods our rules forbid. The most likely useful outcome is a filter (skip earnings days, skip gap-ups) that improves the existing swing/daily strategies rather than a new day-trading account.

## Open questions
1. Do we want an "event-day overnight hold" exemption (M3, M5, M10)? Without it, the best-documented effects are untestable.
2. Is a smaller fill lag acceptable on event days (e.g. t+1 open on a 15m bar)? At t+3 we enter 45 minutes late.
3. Does our stored 15m dataset include pre-market bars?
4. Are we willing to spend on LLM scoring for a forward test (M4)?
5. Is the 501-stock S&P 500 universe the only one we will accept? Most stocks-in-play and news-drift evidence is in small and mid caps.
