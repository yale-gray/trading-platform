# Evidence quality, base rates and a testing protocol for a LIST of candidate methods (quality-lead, 2026-10-23)

Research only. Web sources are cited by URL. Where a search did not confirm a claim, it is marked UNVERIFIED. I did not read every paper in full; several numbers come from abstracts or secondary summaries and are flagged.

## 1. Base rates: how many day traders make money?

Short answer: very few, and the ones who do are rare and hard to identify in advance.

| Study | Market / period | What it found | Source |
|---|---|---|---|
| Barber, Lee, Liu, Odean, Zhang, "Learning, Fast or Slow" (RAPS 2020; earlier "Do Day Traders Rationally Learn About Their Ability?") | Taiwan, every trade, 1992-2006 | Aggregate day-trader performance is negative. 74% of day-trading volume comes from traders with a history of losses. 97% of day traders are predicted to lose in future. Only a small group (under 3% of day traders, under 10% of volume) predictably earns profits net of costs. More than 75% quit within two years. | https://ideas.repec.org/a/oup/rasset/v10y2020i1p61-93..html ; https://faculty.haas.berkeley.edu/odean/papers/day%20traders/day%20trading%20and%20learning%20110217.pdf |
| Chague, De-Losso, Giovannetti, "Day Trading for a Living?" | Brazil equity futures, everyone who started 2013-2015 | Of those who persisted over 300 days, 97% lost money. Only about 1.1% earned more than the Brazilian minimum wage and about 0.5% more than a bank teller (versions differ: 0.4-0.5%). No evidence of learning. Best trader about US$310/day with a daily standard deviation of US$2,560, so even the winner is hard to tell from luck. | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101 ; https://repositorio.fgv.br/items/e87d04fc-4b4c-4a56-ab5a-1c84c1afde1b/full |
| Jordan & Diltz, "The Profitability of Day Traders" (FAJ 2003) | US, 1990s bull-market brokerage accounts | Day traders lost money at about twice the rate they made it. Roughly 1 in 5 was more than marginally profitable. Profit was tied to the Nasdaq rally. (Secondary sources quote 35-36% profitable; I could not confirm that against the paper.) | https://ideas.repec.org/a/taf/ufajxx/v59y2003i6p85-94.html |
| Beckmeyer, Branger, Gayda, "Retail Traders Love 0DTE Options... But Should They?" | US SPX options, Jan 2021-Feb 2023 | Retail lost money on 0DTE options. Average daily retail loss about $358k after May 2022. About 60% of the losses were transaction costs. Sellers did better than buyers. Figures are from press coverage, not the paper (UNVERIFIED against SSRN). | https://www.bnnbloomberg.ca/day-traders-lose-358-000-per-day-gambling-on-zero-day-options-1.1910634 ; https://cxoadvisory.com/individual-investing/retail-0dte-option-trader-performance |
| ESMA CFD product intervention analysis (2018) | EU retail CFD accounts | 74-89% of retail CFD accounts lost money. Regulators restricted leverage and banned binary options. | https://www.esma.europa.eu/node/84933 |

I searched for a 2020-2024 Robinhood intraday-performance study and did not find a clean, citable one. Do not quote one. The pattern in the studies above is consistent across countries, decades and instruments, and costs are the main mechanism: the winners are a tiny, persistent minority, and nobody (including the winners themselves) can tell in advance who they are.

Caveats on these base rates: they describe humans trading discretionary rules in cash-like retail accounts. They are not a proof that every mechanical rule fails. They are a strong prior: a random day-trading rule has a very low chance of net edge, so the burden of proof sits on the candidate.

Plain-English takeaway for the owner: if you picked a random person who day trades, they would most likely lose money, and the few who win do not stay winners reliably. Our four failed rules are exactly what this prior predicts.

## 2. Publication decay and replication

### 2a. General (cross-section) literature

- McLean & Pontiff (J. Finance 2016), 97 published return predictors: returns fall about 26% out-of-sample (before publication) and about 58% after publication. The 26% is read as an upper bound on data-mining bias; the extra roughly 32% is investors trading on the finding. https://ivey.uwo.ca/media/3775549/pontiff.pdf
- Harvey, Liu, Zhu (RFS 2016): with hundreds of tested factors a new factor should clear t above about 3.0, not 2.0. Most claimed findings are likely false under their model. https://www.nber.org/papers/w20592
- Hou, Xue, Zhang, "Replicating Anomalies" (RFS 2020), 452 anomalies: 65% fail |t| of 1.96 once microcaps are neutralised (NYSE breakpoints, value-weighted). 82% fail at the multiple-testing hurdle of 2.78. Trading-friction anomalies fail most (96%). Momentum, value, investment and profitability held up best. https://ideas.repec.org/a/oup/rfinst/v33y2020i5p2019-2133..html
- Counterpoint: Jensen, Kelly, Pedersen (J. Finance 2023) argue most factors replicate and work across 93 countries. https://www.nber.org/papers/w28432 . I searched for an Andrew Chen paper to the same effect (it is mentioned in one summary) but could not confirm its title (UNVERIFIED).
- Honest reading: the replication debate is about slow, cross-sectional factors that are mostly driven by small, illiquid stocks. Intraday timing rules on liquid ETFs are a different animal and have almost no independent replication in the academic literature. Absence of a critique is not confirmation.

### 2b. Popular intraday papers: replication status

| Paper | Claim | Independent replication found? | Status |
|---|---|---|---|
| Gao, Han, Li, Zhou, "Market Intraday Momentum" (JFE 2018): first half-hour return predicts last half-hour, SPY 1993-2013 | Significant in-sample and (per the authors) out-of-sample | No dedicated post-publication decay study found. A QuantConnect write-up re-implements it but on a narrowed ETF set, so not like-for-like. https://www.quantconnect.com/research/15348/intraday-etf-momentum/ | Unreplicated by independent academics (as far as search shows). Our round-2 test (zero gross edge on SPY/QQQ 2016-2026) is itself weak evidence of decay. |
| Baltussen, Da, Lammers, Martens, "Hedging Demand and Market Intraday Momentum" (JFE 2021): 60+ futures 1974-2020, last-30-min predicted by return so far | Strong, tied to gamma hedging | No out-of-sample test found. Mechanism implies the effect depends on option-market structure that has changed (0DTE growth), so it could strengthen or weaken. https://www3.nd.edu/~zda/intramom.pdf | Unreplicated; mechanism-dependent. Note the signal is measured from the previous close, not from the open (our round-2 rule used "up on the day"; the design details matter). |
| Zarattini, Aziz, "Can Day Trading Really Be Profitable?" (SSRN 2023): ORB on QQQ 2016-2023, high leverage | About 33%/yr with 4x leverage; 24% hit rate, +0.13 R/trade | One non-peer-reviewed blog replication (Krueger, MQL5, Jan 2015-Jun 2026, five index CFDs): gross result reproduces on NQ (+0.131 R, 23.2% hit), but after spread and slippage NQ is +0.002 R and the other four are negative; none distinguishable from zero. The author sells trading products, so treat as suggestive only. https://www.mql5.com/en/blogs/post/776235 ; paper https://papers.ssrn.com/abstract=4416622 | Gross edge reproduced, net edge not. Matches our own finding (gross 2-3 bps, net negative). |
| Zarattini, Barbon, Aziz, "Beat the Market" (SSRN/SFI 2024): SPY intraday momentum, noise-area boundaries, vol-targeted leverage | 19.6%/yr, Sharpe 1.33, 2007-early 2024 | None found beyond TradingView scripts. The abstract does not state a held-out period; leverage and volatility targeting inflate return figures. https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy | Unreplicated. Sharpe 1.33 on one index with rules chosen on the same period is the classic overfitting profile. |
| Sullivan, Timmermann, White (J. Finance 1999): technical rules with the Reality Check | Famous rules looked good until data-snooping was controlled; later work found little left in mature indices | Re-examination: rules only significant in younger markets (Nasdaq Composite, Russell 2000), not DJIA/S&P 500. https://c.mql5.com/forextsd/forum/211/Re-Examining%20the%20Profitability%20of%20Technical%20Analysis%20with%20Whitets%20Reality%20Check%20and%20Hansen9s%20SPA%20Tes.pdf (link as returned by search) | Direct precedent for our situation. |

Bottom line on 2b: the literature has not independently confirmed intraday timing edges on SPY/QQQ. The only replication I found nets to zero after costs. Authors of the ORB/intraday-momentum papers are practitioners who sell training or funds; none of these papers includes a spread-based cost model.

## 3. The multiple-testing problem for a LIST of methods

Why it matters: Bailey et al. show that after trying only 7 configurations you expect to find a 2-year backtest with Sharpe above 1 even when the true Sharpe is 0. https://scholarworks.wmich.edu/math_pubs/40/ (Notices of the AMS 2014) . We are about to test a list, and four rules have already been tried. The count of trials N includes those four plus every parameter variant touched.

Tools and what each is for:
- Deflated Sharpe Ratio (Bailey & Lopez de Prado, JPM 2014): the probability that the true Sharpe exceeds the best Sharpe you would expect from N luck-only trials, adjusting for skew and kurtosis and track length. https://papers.ssrn.com/abstract=2460551 . Rule of thumb of DSR above 0.95 is a convention, not from the paper.
- White's Reality Check (2000) and Hansen's SPA (2005): bootstrap tests of whether the best of N rules beats a benchmark after accounting for snooping. SPA is generally more powerful and less sensitive to poor candidates. https://ideas.repec.org/p/bro/econwp/2001-06.html . Needs aligned return series for every candidate (including losers), which is exactly what we can store.
- Bonferroni / Holm across candidates: simple, conservative, easy to audit. With N candidates use alpha/N (Bonferroni) or step-down (Holm). A t-statistic hurdle near 3 (HLZ) is the practical equivalent for tens of tests.
- Minimum backtest length: more trials need longer history. Intraday gives many bars but few independent days, and market regimes are the real sample size (10.6 years is only about 2,700 days and a handful of regimes).
- A true hold-out used once. Our code already enforces this structurally for the split (OosRegistry, one evaluation per config hash).

## 4. Recommended multi-strategy testing protocol (compatible with our gate)

1. Freeze the list and the budget before any run. Commit a file listing every candidate id, rule, parameters, instrument, the cost model and the budget N (suggest N at most 12 candidates and zero re-tunes; a changed number is a new id and counts toward N). Log the four failed rules as trials 1-4 so the total N includes them.
2. Split the data three ways, not two. Development: 2016-03 to 2023-12. Final hold-out: 2024-01 to the latest date (about 2.8 years). Keep the current 70/30 OOS inside the development period only for gate purposes. The final hold-out is never touched during development; the existing DataView machinery should refuse to load it.
3. Triage on development data with a cheap, honest filter before the gate: gross edge per trade in bps against a matched null (random-direction or random-time entry in the same windows, as in the Krueger replication), with block-bootstrap confidence intervals. Drop anything whose gross edge is less than about 2x the round-trip cost under the spread-based cost model. This prevents spending the gate on rules that cannot clear costs.
4. Run the existing six-gate suite on survivors (history, walk-forward, OOS vs benchmark, trade count, sensitivity, forward). Save the daily net-return series of ALL candidates, passing or failing.
5. Correct for the list: compute the Deflated Sharpe Ratio for each survivor using N from step 1 and the cross-candidate Sharpe variance. Run White's Reality Check or Hansen's SPA on the saved return series against the chosen null (cash for absolute edge; SPY for the relative test). Require Holm-adjusted p below 0.05 (or t above 3 for the best candidate). Only candidates that pass BOTH the gate and the multiplicity test advance.
6. Use the final hold-out exactly once, for at most two pre-named finalists, with parameters frozen. Pass requires net Sharpe above 0 with a confidence interval excluding the cost drag and no degradation worse than 50% of the development Sharpe. If it fails, the result is final; no re-run on the same window.
7. Forward paper trading (existing gate 6, 90 days minimum) with the live cost model, before any claim. Treat paper fills as optimistic; compare realised slippage with the model.
8. Write up all N results, including failures, in a single table. Publish the count. The report must say how many candidates were tried.

Budget guidance: every additional candidate raises the Sharpe a lucky rule needs. With N around 12, the expected best Sharpe from pure noise on about 10 years is roughly 0.6 to 0.8 annualised, so a candidate with net Sharpe near 0.5 should not impress anyone. State the exact expected-max-Sharpe figure from the DSR formula once the cross-candidate variance is measured.

## 5. Gaps in OUR intraday gate and recommended changes

| Gap | Why it matters | Recommended change |
|---|---|---|
| Walk-forward windows 3y/1y/1y in bars on 15-minute data | Only about 7 windows over 10.6 years, each one a year-long regime sample. "Sharpe above 0 in 60% of windows" with so few windows is high variance (one window flips the result). | Keep, but also report per-year and per-regime results, and add a block-bootstrap CI on the whole-period net Sharpe. Treat the window fraction as diagnostic, not the only test. |
| Sensitivity on bar-index parameters | The +-20% sweep on bar indices shifts decision time by whole bars and also interacts with the 2-bar decision lag. It is not equivalent to testing a smoother parameter. It passed for rules that lose money, so it measures stability, not edge. | Keep as a check against fragility. Do not let it count as evidence of an edge. Add a time-of-day shift test (-1, 0, +1 bars) for time-based rules. |
| Survivorship bias | Current S&P 500 list flatters stock rules (acknowledged as A13). Irrelevant for SPY/QQQ, material for the 500-stock candidates. | For stock universes, use point-in-time constituents or restrict to ETFs. If neither is available, label the result "upper bound". |
| SIP missing-bar days (e.g. 2019-08-12) | Missing bars create artificial gaps and can create or destroy signals. | Add a data-quality gate: reject days with missing regular-session bars, count excluded days, and report results with and without them. |
| Flat 5 bps per side | Too high for SPY/QQQ (spread is about 1 cent on about $500, i.e. roughly 0.2 bps; realistic all-in under 1 bp for small size), too low for small caps and open/close auctions. It decided round 2 against the strategies, so the cost model is the main driver of the verdict. | Replace with a spread-based model: half-spread by instrument and time of day (wider in the first and last minutes) plus a small impact term, calibrated from quote data. Run the verdict at the central estimate and report a 2x stress. Keep the 5 bps as a sensitivity row, not the headline. Pre-register which model decides, since choosing after seeing results is itself snooping. |
| Benchmark = SPY buy-and-hold Sharpe | A strategy that is in the market 10-20% of the day cannot match a 100%-invested index in return, and the Sharpe comparison is mixed with exposure. But the owner's real alternative IS holding SPY, so it is a fair "should I do this" test. It is a poor "does this signal contain information" test. | Use two separate questions. (a) Absolute edge: net mean return per trade and daily net excess return over cash, with t-statistic, DSR and the multiplicity tests above. (b) Incremental value: alpha from regressing strategy returns on SPY, plus the Sharpe of a SPY + strategy overlay (hold SPY, add the day-trading sleeve as a small funded or margin-neutral overlay) versus SPY alone. A day-trading rule is only worth building if (a) is significant and (b) adds Sharpe after costs. Gate 3 as it stands should stay as a "is it better than just holding" check. |
| Opportunity cost of cash | Cash earns about 4-5% now (T-bills), which is a meaningful hurdle. | Compute excess returns over the T-bill rate, not raw returns. |
| Walk-forward uses same engine settings throughout | Does not test regime/volatility-structure changes (e.g. 0DTE era from 2022). | Report results split at known structural breaks (2020 volatility, 2022 daily 0DTE) as descriptive splits, pre-declared. |
| No explicit multiple-testing control | The gate checks one strategy at a time. | Add the DSR and SPA/Holm step in section 4 as gate 7 ("list-adjusted"). |
| Trade-count gate of 30 | 30 trades is far too few for an intraday edge in bps (standard error of the mean per trade is huge). | Require enough trades for the per-trade t-stat to be computable: roughly (cost-sized edge / per-trade volatility)-based minimum, in practice a few hundred. Report the t-statistic of the net mean per trade. |

## 6. Red flags in day-trading marketing, blogs and YouTube

1. A backtest with no costs, no spread, or commission only (the ORB paper's critic notes no spread or slippage). Anything under a few bps for stock round trips is fantasy.
2. No out-of-sample period, or an out-of-sample period chosen after the fact.
3. High leverage reported as "annual return" with no drawdown or ruin probability. Leverage scales return, drawdown and risk-of-ruin together.
4. One instrument and one period (for example QQQ 2016-2023, a historic bull run).
5. Win rate or reward-to-risk quoted without expectancy per trade net of costs. A 24% hit rate with a positive R only works if costs are small.
6. "Parameters were not optimised" with no count of what was tried. Ask how many strategies were looked at first.
7. Selected examples: screenshots of winning days, live-trading equity curves with no broker statement, paper-trading results presented as live.
8. The seller makes money from courses, signals, prop-firm evaluation fees or affiliate brokers, not from trading. Also, prop-firm evaluations are designed so most buyers fail.
9. Survivorship in the seller's own track record (only the lucky account is shown). Base rates in section 1 say roughly 97% of persistent day traders lose.
10. Claims of guaranteed or consistent returns, urgency, "secret" patterns, or Sharpe above about 2 for a retail-executable intraday strategy.
11. Backtests on current index members, or on data bars that include after-hours or adjusted prices without saying so.
12. Hindsight-friendly language, such as "stocks in play" selected using the same day's volume or gap, which uses information not available at the entry time (lookahead). Check any selection variable against our hard rule 2.

(I did not locate SEC/FINRA day-trading investor alerts in this search, so this list is based on the evidence above plus standard backtest-overfitting practice, not a regulator checklist.)

## 7. What this means for the candidate list

- Prior: low. Four rules failed and the published intraday-edge evidence is mostly unreplicated or nets to zero after costs.
- The two things most likely to change a verdict are cost modelling (spread-based, per instrument and per time of day) and the benchmark definition. Both must be fixed before any new run.
- Categories with the best independent evidence in the wider literature are slower and cross-sectional (momentum, value, quality), not intraday timing. If a candidate list includes both, hold the intraday ones to the stricter bar above.
- Success should be defined in advance as "passes gate plus list-adjusted test plus single-use hold-out", and the honest default is "no candidate qualifies".

## Open questions for the PM
1. Do we have quote (bid/ask) data, or only bars? Spread-based costs need either historical quotes or a documented spread table.
2. Is the final hold-out 2024-onward acceptable? 2025-2026 alone is under 2 years, which is thin for statistical power. It also means round 2 results (which already used data through 2026-10) are contaminated for any rule resembling those two.
3. Should the day-trading sleeve be judged as a standalone account, or as an overlay on SPY holdings (changes which hurdle applies)?
4. What is the testing budget N, and who holds the trial counter (suggest the PM, in docs/process-log.md)?
5. Is point-in-time S&P 500 membership available? If not, restrict stock-universe candidates to ETFs.
