# DT deep-dive 03: high-risk instruments, realistic costs, account mechanics (2026-10-23)

Research only, no code. Author: execution-lead. Simulator-only (hard rule 1) applies to everything below.
Evidence labels: **[P]** peer-reviewed or working paper, abstract seen via search; **[S]** secondary summary or practitioner/marketing source; **[K]** my own background knowledge, not re-verified this session (check before relying). Search tools returned abstracts and summaries, not full papers, so numbers are as reported by the cited pages.

## Summary table

| # | Idea | Evidence after costs | Worst case | System needs (exact) | Effort | Free-data testable? | Recommendation |
|---|---|---|---|---|---|---|---|
| 1a | Leveraged ETF close-rebalancing front-run (long SPXL/TQQQ late if up, SQQQ if down; or SSO/SDS) | One paper: gross +0.66%/trade, mostly 2008, ends 2011; no net-of-cost evidence; effect likely arbitraged | 3x ETFs lost 82-91% peak to trough in 2022 | Nothing new for long-only on TQQQ/SQQQ/SPXL (just more symbols in 15m feed). Inverse ETF = bearish exposure without shorting | Small (1-2 days) | Yes (Alpaca 15m bars exist for these ETFs) | **Test** (cheap, long-only, same engine) |
| 1b | Hold leveraged ETFs over multiple days/trend | Volatility decay; works only in trends | -82% TQQQ, -91% SOXL (2022) | Daily data | Small | Yes | **Test as risk-managed variant only (e.g. SMA-gated)**; otherwise avoid |
| 2a | Buy 0DTE options (retail style) | Retail lost >$70M over ~2 yrs, >$50M of it transaction costs [S] | 100% of premium each trade | Options chains/quotes (OPRA), greeks, expiry settlement, contract-multiplier ledger | Large; and free historical SPX chain data is not available | **No** (no free intraday option history) | **Avoid** |
| 2b | Sell 0DTE (iron condors/credit spreads) | Practitioner win rates 67-70% [S]; academic read (via blog) Sharpe about 0, fat tails | Defined-risk loss several times a typical win; gap days | Same as 2a plus margin model, assignment | Large | **No** | **Avoid / test later** only if we buy a data set |
| 3 | Intraday futures (MES/MNQ/ES/NQ) | No independent after-cost evidence; pros use for cost/margin/tax/24h reasons | Leverage; MES about $35k notional per contract vs $10k account | Futures data (continuous contracts, roll), margin model, contract multiplier, leverage in SimBroker, short selling | Large | Partly (free ES 1-min history is poor; ETF proxy SPY/QQQ gives same signal) | **Test later via SPY/QQQ proxy; don't build futures** |
| 4a | Crypto intraday momentum (first-half-hour predicts last-half-hour, Bitcoin) | Published [P] (Shen, Urquhart, Wang 2022); mechanism = liquidity provision; no net-of-cost tests seen | Crypto tail: -50% days possible; exchange outage | ccxt intraday OHLCV (already available), crypto fee model (spot taker fees much higher than ETF spread) | Small-medium | Yes | **Test** (with 10-25 bps/side realistic spot fees, see cost section) |
| 4b | Crypto calendar/time-of-day/weekend effects | Baur et al.: time-varying, no persistent pattern [P] | n/a | ccxt | Small | Yes | **Avoid** (data-mined risk) |
| 4c | Funding-rate / basis carry (long spot, short perp) | Crypto Carry (Schmeling, Schrimpf, Todorov) [P]: carry can exceed 40%/yr; a 2025 survey reports Sharpe 6.45 full sample, ~4 since 2024, negative in 2025 [S, unverified] | Exchange failure, liquidation of the short leg, basis blowups (Oct 2025-type cascades) [K] | **Shorting + perp contracts + funding-payment ledger + margin/liquidation**; not intraday at all | Large | Funding-rate history is free from exchanges via ccxt | **Avoid for now** (not day trading, needs short and leverage) |
| 4d | Perp leverage directional | Retail-dominated, negative-sum after costs [K] | Liquidation = 100% loss | perps | Large | - | **Avoid** |
| 5 | Intraday short selling (fade gap-ups, short breakdowns) | Borrow on liquid S&P names is cheap (general collateral) [S]; edge itself unproven here | Unlimited loss, squeezes; borrow fee spikes 34% to 195% in 3 days on a hard-to-borrow name [S] | **Short positions in SimBroker (negative qty, margin, borrow fee accrual, Rule 201 gate)**, margin account | Medium | Yes for S&P 500 GC names (borrow cost can be modelled flat) | **Test later** (needs ADR); cheaper first step is inverse ETFs (1a) |
| 6 | Vol-targeting / Kelly sizing | Vol targeting lifts Sharpe for equities (0.40 to ~0.5) and cuts tails; negligible for other classes [P] (Harvey et al. 2018) | Full Kelly: ~50% chance of a halving in idealised model; overbetting is worse than underbetting [S] | Position sizing hook in strategy/risk layer; realized-vol estimate | Small | Yes | **Test** as a risk overlay on the slow (daily) strategies, not as a cure for day-trading |
| 7 | Cost realism | See cost section | - | Cost model upgrade (per-instrument bps) | Small | Yes | **Do** (changes how we read every earlier fail) |
| 8 | Base rate for retail day traders | Overwhelmingly negative (below) | - | - | - | - | Context only |

## 1. Leveraged and inverse ETFs

**Plain English.** A 3x fund must hold 3x its net assets in exposure at the close. If the index rose today, the fund must buy more at the close; if it fell, it must sell. Cheng & Madhavan (2009, J. Investment Management 7(4)) argue this daily re-leveraging pushes the last-hour price and volatility in the direction of the day's move [P, abstract only; Fed note FEDS 2013-48 summarises the same: https://www.federalreserve.gov/pubs/feds/2013/201348/ ; paper: https://joim.com/dynamics-leveraged-inverse-exchange-traded-funds]. Shum et al. (Review of Finance 2016) find late-day volatility correlates with the ratio of estimated rebalancing trades to volume, strongest on volatile days (2006-11 sample), and test buying SSO/SDS after a 2:30pm move: average gross +0.66% per trade, cumulative gross 119%, returns concentrated in late 2008, 35% of trades lost [S via https://www.cxoadvisory.com/volatility-effects/front-running-leveraged-etfs-at-the-end-of-the-day ; https://ideas.repec.org/a/oup/revfin/v20y2016i6p2379-2409..html].

**Honest read.** This is the same family as our failed `etf_dt_late_momentum` (Gao et al. "market intraday momentum"). A 2008-heavy, gross-of-cost, pre-2011 result is weak evidence for 2016-2026. Rebalancing flow is now a known, front-run quantity and the leveraged AUM base is much larger, so either the effect is bigger (more flow) or arbitraged away. Unknown; testable cheaply. Because our own SPY late-momentum test showed ~0 bps gross, expect the same unless the signal is amplified by the leveraged-ETF-flow size estimate (use flow = L(L-1) x AUM x day return, which needs leveraged ETF AUM data; approximating it with the day's return alone is the lazy variant).

**Volatility decay (holding).** Daily reset makes multi-day return path-dependent: in choppy markets a 3x fund loses even if the index is flat. Cheng & Madhavan note value destruction for buy-and-hold [P]. Concrete tail: SOXL fell about 90% (peak 2021-12-27, trough 2022-10-14) while the semiconductor index fell about 35%; TQQQ fell about 82% (2021-11 to 2022-12); SOXL did not regain its peak until April 2026 [S: https://finance.yahoo.com/markets/options/articles/soxl-lost-90-percent-in-2022-220016228.html ; https://chartrow.com/quote/soxl/drawdown ; https://www.etf.com/sections/features/why-investors-are-dumping-tqqq-soxl-despite-huge-gains]. With a $10k account a 3x position moves like $30k of exposure: one -10% index day is about -30% of account.

**Costs.** TQQQ/SPXL/SOXL spreads are 1-3 bps in normal times [K], wider than SPY but fine; SQQQ/SOXS are cheaper per share but price is tiny and noisy (reverse splits). Expense ratios ~0.9%/yr are irrelevant intraday.

**What we need.** Long-only works: SQQQ/SPXS replace shorting. Add the symbols to the 15m feed. No leverage logic in our engine (the leverage is inside the product). Reverse/forward splits must be adjusted in data (Alpaca adjustments; verify). Effort: small.

**Recommendation.** Test (a) the close-rebalance rule on TQQQ/SQQQ with 3 bps/side and 5 bps stress, pre-registered; (b) NOT multi-day holding except as an SMA-trend-gated daily variant. Treat blow-up risk as a first-class output: report max single-day loss.

## 2. 0DTE and short-dated options

**Plain English.** A 0DTE option expires today. Buyers want a cheap lottery ticket on the index; sellers (iron condors, credit spreads) collect premium and hope SPX stays in a range.

**Evidence.** Beckmeyer, Branger, Gayda (March 2023, "Retail Traders Love 0DTE Options... But Should They?"): retail lost consistently; average about $184k/day over the sample, $358k/day after daily expirations began 2022-05-16; total >$70M over roughly two years, and per a summary more than $50M of that is transaction costs (spreads) [S: https://www.bloomberg.com/news/articles/2023-04-21/day-traders-lose-358-000-per-day-gambling-on-zero-day-options ; https://www.wealthmanagement.com/equities/retail-investors-are-love-same-day-options ; https://cxoadvisory.com/individual-investing/retail-0dte-option-trader-performance]. A different summary quotes $125M / $350k/day [S: https://neudata.co/literature-reviews/zero-day-to-expiry-options-a-losing-bet-for-retail-traders], so the dollar totals vary by write-up; the sign and the cost share are consistent. Sellers: platform data says condors/butterflies were 78% of 0DTE positions with 67-70% win rates [S: https://optionalpha.com/bots/0dte-research-insights-bot]; a blog summary of an academic analysis says the P&L is symmetric, fat-tailed, Sharpe near zero, and static hold-to-expiry rules fail [S: https://breakingnasdaq.beehiiv.com/p/the-0dte-illusion]. High win rate is not profit: small wins, rare large losses.

**Tail risk.** Buyer: lose 100% of premium, daily. Seller: a $5-wide SPX condor risks $500/contract minus credit, about 4-5x a typical win; on a trend/gap day the whole week of gains goes. A $10k account can hold only a few contracts, so one max loss is a few % to 10%+ of equity.

**What we'd need.** Option chain history (OPRA bid/ask by strike, intraday), a pricing/greeks model or real quotes, multi-leg order and expiry/settlement (cash-settled SPX vs physical SPY), contract-multiplier ledger in Decimal, margin for defined-risk spreads, and a spread model (0DTE option spreads are wide in % terms). Alpaca does offer options trading but we have no historical chain data and hard rule 1 forbids order code; Black-Scholes-from-VIX synthetic pricing would be fiction for spread costs, which is the whole story here. Effort: large (weeks) plus a paid data set.

**Recommendation.** Avoid. Not testable honestly on free data. Revisit only if we buy chain data and have an ADR.

## 3. Intraday futures (ES, NQ, MES, MNQ)

**Plain English / why pros use them.** Near-24h market, one liquid venue, spread of one tick, no pattern-day-trader rule, 60/40 US tax treatment [K], capital efficiency from margin, easy short side, no borrow. MES = $5 x S&P index; tick 0.25 pt = $1.25 [S: https://www.ninjatrader.com/futures/blogs/what-are-micro-e-mini-s-p-500-futures-mes/ ; https://tradezero.com/en-us/support/questions/what-are-the-contract-specifications-for-the-micro-e-mini-s-and-p-500-mes-tradezero-america]. Brokers quote about $50 intraday margin and about $2,863 overnight initial margin per MES (as of 2026-09-16, TradeZero) [S]. At an index near 6,500-7,000 [K, approximate] one MES is roughly $33-35k of notional, 3x+ a $10k account. So the "cheap margin" is the danger: one MES with a 1.5% adverse move loses about $500 (5% of the account) in minutes; a 5% crash day loses about $1,700.

**Costs.** Spread about 1 tick (~0.3-0.4 bp of notional); commissions roughly $0.5-1.5 per side per contract (~0.2-0.4 bp) [K]. So all-in about 1 bp round trip, similar to SPY. Futures are not cheaper than SPY on spread; they are cheaper in leverage and tax.

**Evidence.** The Brazil study (section 8) is exactly retail mini-index-futures day trading and found 97% of persistent traders lost [P]. No independent after-cost study of a profitable futures day rule came up. Since SPY/QQQ have almost identical price action to ES/NQ, our SPY 15m tests already answer the signal question.

**Needs.** Futures history with roll/continuous contract, contract multiplier, margin/liquidation model, leverage, overnight/session handling, short. Effort: large. 

**Recommendation.** Do not build. If a SPY/QQQ rule passes, rerun in a futures-style cost/leverage model later.

## 4. Crypto intraday

**4a Momentum.** Shen, Urquhart & Wang (Financial Review 57(2), 2022), "Bitcoin intraday time series momentum": first half-hour predicts last half-hour, using volume as proxy for session time; they attribute it to liquidity provision, not informed trading [P/S: https://research.birmingham.ac.uk/en/publications/bitcoin-intraday-time-series-momentum/]. This is the crypto cousin of Gao et al., which failed in our SPY data. Because crypto has no close, "last half-hour" must be defined (e.g. UTC 23:30 or US-equity close); definition choice is a hidden degree of freedom, so pre-register it. A 2020-2025 thesis compares momentum vs mean-reversion on Bitcoin [S: https://www.theseus.fi/handle/10024/902903], low quality.

**4b Calendar effects.** Baur, Cahill, Godfrey, Liu: time-of-day effects vary over time with no persistent pattern, Monday return effect, lower weekend volume; the authors read this as efficiency [P: https://research-repository.uwa.edu.au/en/publications/bitcoin-time-of-day-day-of-week-and-month-of-year-effects-in-retu/]. A 2024 paper (2015-2021 data) finds Friday/intraday anomalies but not pervasive [S: https://ideas.repec.org/a/spt/apfiba/v14y2024i4f14_4_3.html]. High data-mining risk; skip.

**4c Funding/basis carry.** Schmeling, Schrimpf, Todorov, "Crypto Carry" (BIS WP 1087; Management Science): futures-spot basis exceeds 40%/yr at times, driven by leveraged trend-chasing demand and scarce arbitrage capital [P: https://www.bis.org/publ/work1087.pdf]. A 2025 survey reports Sharpe 6.45 (2020-25), 4.06 from 2024, negative in 2025, mean ~8% [S, I could not verify: https://arxiv.org/pdf/2510.14435]. The decline matches crowding. It is a slow carry trade, not day trading, and needs a short perp leg with margin; tail risk is exchange failure and liquidation of the short leg during a squeeze [K].

**4d Perp leverage.** 10-100x leverage on a 24/7 market; liquidation wipes the position. No honest edge claim; avoid.

**Costs (crucial).** Spot taker fees at retail tiers are typically 10-40 bps per side (Coinbase/Kraken-type, low volume) and ~10 bps on Binance-type before discounts [K, verify against the venue you model]; spreads on BTC/ETH are ~1 bp but altcoins 5-30 bps. So crypto intraday starts about 20-80 bps round-trip behind, an order of magnitude worse than SPY. Only slow or high-edge ideas can survive. Crypto price data via ccxt is a public-OHLCV feed; no look-ahead issues beyond our existing lag rule.

**Needs.** ccxt intraday OHLCV (have), fee model per venue, 24/7 calendar (no "flatten at close": need a flatten rule), T+1 irrelevant (crypto settles instantly).

**Recommendation.** Test 4a once, pre-registered, BTC and ETH, with 15 bps/side fees and a 5 bps stress. Avoid 4b/4d. 4c: avoid now.

## 5. Intraday short selling

**Rule.** Sell borrowed shares high, buy back lower. SEC Rule 201 (alternative uptick, effective 2010): once a stock drops 10% from the prior close, shorts are allowed only above the national best bid for the rest of that day and the next day [S: https://www.sec.gov/news/press/2010/2010-26.htm]. So shorting after a crash is restricted but still possible via limit orders; in the sim, model it as no-fill unless price > bid, or simply skip.

**Costs.** General-collateral names (Apple-type, i.e. most S&P 500) have low borrow fees, annualized and accrued daily on settled positions [S: https://www.interactivebrokers.com/campus/traders-insight/securities/short-selling/the-risks-of-shorting-series-part-ii-borrow-fees/]. I did not find a verified number; typical GC is ~0.3-1%/yr [K], i.e. under 0.01 bp per intraday trade, negligible. Intraday-only shorts may not incur a borrow fee at all at some brokers [K]. The real costs are hard-to-borrow names (fee 34% to 195% in three days on one example [S]) and squeezes, which large caps rarely suffer but do (e.g. gap-ups on earnings).

**Tail.** Loss is unbounded in principle; intraday on an S&P 500 name a +20% gap is the realistic worst case, -20% of the position. With 5 positions of 20% each this is -4% of equity per event; but one event-gap on a full-size short is -20%.

**What it unlocks.** Fade gap-ups, symmetric breakout/breakdown, market-neutral pairs. Evidence that gap fades pay after cost is not in this session's search; our own dip strategy showed a gross +3%/yr only, so do not expect large unlock. Inverse ETFs (SH, SDS, SQQQ) give a bearish expression without any engine change, with a 1-3 bp spread cost.

**Needs.** Negative quantities in SimBroker/portfolio, short-margin (Reg T 50% initial; Alpaca needs a margin account and minimum equity [K: $2,000]), borrow-fee accrual, short-proceeds accounting in Decimal, a hard cap on squeeze risk (stop-loss, position cap), and a rule against shorts on ADR-flagged hard-to-borrow names. Execution-lead estimate: medium effort (2-4 tasks), needs an ADR because it touches ledger rules and hard rule 1 sits near (no real orders, fine).

**Recommendation.** Test later; first run the inverse-ETF proxy (section 1).

## 6. Volatility targeting and position sizing

**Vol targeting.** Scale exposure inversely to recent realized volatility. Harvey, Hoyle, Korgaonkar, Rattray, Sargaison, Van Hemert (JPM Fall 2018; 60+ assets, daily data back to 1926, includes costs): raises Sharpe for equities and credit (US equities about 0.40 to 0.48-0.51), negligible for bonds/FX/commodities, and reduces tail events and drawdowns across all classes; it also acts like momentum because vol rises when risk assets fall [P: https://alphaarchitect.com/volatility-targeting-improves-risk-adjusted-returns/ ; https://quantpedia.com/the-impact-of-volatility-targeting-on-equities-bonds-commodities-and-currencies]. Moreira & Muir (2017) is the other anchor but my search did not retrieve it; cite from [K] only.

**Kelly.** Full Kelly maximises log wealth but in the idealised model has about a 50% chance of a halving; overbetting is worse than underbetting, and with estimated (noisy) edges practitioners use half or quarter Kelly [S: https://en.wikipedia.org/wiki/Kelly_criterion ; https://www.arxiv-vanity.com/papers/1710.01787/ ; https://edoc.hu-berlin.de/18452/14923]. 

**Does it rescue thin edges?** No. Sizing multiplies both the edge and the cost: with negative expectancy after costs, leverage multiplies the loss (our strategies lost 19%/yr at 1x). Kelly for a negative-edge bet is zero. Vol-targeting raises Sharpe by only ~0.1 on equities; day-trading rules had Sharpe -2 to -4. It can make a positive-edge strategy blow up if the edge was overestimated (typical after backtesting, 10 trials). Use it as a risk overlay (cap exposure when 20d vol is high; cap per-position loss) and as a way to cut leveraged-ETF blow-up risk (e.g. TQQQ weight = target vol / (3 x index vol)).

**Needs.** A sizing hook (shares = f(equity, realized vol)) in strategy or risk; fractional shares already exist. Effort: small. 

**Recommendation.** Test as an overlay on daily/ETF trend and on any leveraged-ETF idea; never as the thing that makes a strategy pass.

## 7. Realistic costs and account mechanics

Quoted spreads. State Street reports SPY average bid-ask spread 0.29 bps (Bloomberg, 2023-10-31) and 0.27 bps (June 2024), 1-cent dollar spread, ~2,600 shares displayed on each side [S: https://prd-ams.ssga.com/library-content/pdfs/etf/us/us-spy-brochure.pdf ; https://ssga.com/library-content/products/fund-docs/etfs/us/insights-investment-ideas/spy-capital-markets-perspective-q2-2024.pdf]; ETF.com cites ~0.03 bps [S]. These are issuer marketing, daily-average quoted spreads, and the NBBO touch, not what a 15-minute-bar open fill gets. A $10k order (roughly 15 SPY shares, tiny vs the ~2,600 shares displayed) has no meaningful market impact. Half-spread cost to cross: about 0.15 bp. Add queue/latency/slippage vs bar open: our fill uses the t+3 bar open, which is a printed price, so the true cost is the half-spread plus the intrabar noise that comes from an unknown stale quote; ~0.5 bp is a prudent cost for SPY, 1 bp conservative.

Large caps. Mega caps (AAPL, MSFT, NVDA, AMZN): ~0.5-1.5 bps quoted spread; the rest of the S&P 500 median ~2-4 bps; the least liquid ~5-10 bps [K, from market-microstructure experience; not verified this session]. Widest at the open (first 15 min) and close, roughly 2-3x midday [K]. Effective costs for retail marketable orders are often better than quoted spread because of price improvement from wholesalers [K].

Alpaca. Commission-free for API self-directed accounts; only regulatory fees on sells: SEC fee $22.90 per $1M of sale principal (=0.23 bp) and FINRA TAF $0.000119/share, max $5.95 (negligible); also a CAT fee on buys and sells (tiny) [S: https://alpaca.markets/support/regulatory-fees ; https://docs.alpaca.markets/docs/regulatory-fees]. Alpaca reserves the right to charge if order flow is non-retail. Alpaca routes retail flow with payment for order flow, not verified in my search; PFOF implies price improvement vs NBBO is usually positive but not guaranteed [K].

Our 5 bps/side assumption. For SPY/QQQ it is roughly 5-15x too high on the first-order estimate; that is why the 1 bp diagnostic run turned Opening Drive from -36% to +4.4%. For the median S&P stock it is about 2x too high; for the worst S&P names and for crypto it is about right or too low. This is a design problem: a flat 5 bps both overpunishes ETFs and underpunishes small names/crypto. Even at the diagnostic 1 bp SPY, late momentum lost 1.6%/yr and opening drive earned +0.4%/yr vs SPY +15.4%: costs aren't the only problem, the edge is not there.

Cash vs margin, PDT. FINRA retired the pattern-day-trader $25k minimum equity requirement (Rule 4210 amendments approved April 2026, effective 2026-06-04, brokers may phase in until 2027-10-20), replacing it with an intraday margin standard [S: https://www.investor.gov/introduction-investing/investing-basics/glossary/pattern-day-trader ; https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/ ; https://www.kslaw.com/insights/articles/finra-adopts-sweeping-changes-to-margin-requirements-for-day-trading]. A margin account at a broker that has moved gets intraday buying power without PDT counting; confirm your broker's status. Our cash-account model avoids all of it but pays the T+1 settlement constraint (the "5 entries a day" cap in the round-1 report). A margin account would lift the cap and enable shorting but also leverage; keep leverage 1.0 in any test and model margin only for settlement-free recycling (an engineering option: "unsettled-funds recycling allowed"). Note: this is a simulation setting; no real-broker code (hard rule 1).

### Recommended cost model (per side, in bps, applied to fill price)

| Instrument | Base | Stress |
|---|---|---|
| SPY, QQQ | 0.5-1 | 2 |
| Mega-cap stocks (top ~30 by dollar volume) | 1.5 | 3 |
| Rest of S&P 500 | 3 | 6 |
| Leveraged/inverse ETFs (TQQQ, SQQQ, SPXL, SOXL) | 2-3 | 5 |
| Crypto spot BTC/ETH (retail taker fee + spread) | 15 | 30 |
| Other crypto | 30 | 60 |
| Sell-side regulatory (stocks/ETFs) | 0.25 on sells (SEC fee) | 0.25 |
| Opening 15 min and last 15 min | x2 multiplier | x2 |

Keep 5 bps flat as the pre-registered verdict for round 1/2 history (already committed). For new strategies pre-register the table above as the verdict cost and 2x as the stress.

## 8. Base rate: retail day traders

- Barber, Lee, Liu, Odean, Taiwan 1992-2006 (all stock trades): aggregate day-trader performance is negative, the vast majority are unprofitable, unprofitable traders quit, and one version reports more than 75% quit within two years [P: https://faculty.haas.berkeley.edu/odean/papers/day%20traders/day%20trading%20and%20learning%20110217.pdf ; https://www.econ.puc-rio.br/uploads/adm/seminarios/Learning_fast_or_slow_180606.pdf]. A small persistent group is profitable even after fees, which is a skill question, not an average.
- Chague, De-Losso, Giovannetti, "Day Trading for a Living?" (Brazil mini-index futures, 2013-2015 entrants): among those persisting at least 300 days, 97% lost money, only 0.4% earned more than a bank teller (about $54/day), the best earned about $310/day with a daily standard deviation of $2,560, and no evidence of learning [P: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101 ; https://www.cxoadvisory.com/investing-expertise/day-trading-a-bust/ ; https://quantpedia.com/retail-day-trading-is-an-uphill-battle/]. Only about 8% of 19,646 starters persisted 300+ days, so the 97% is of a survivor group; survivorship runs in favour of traders and they still lost. Caveat: Brazil futures, 2013-15, may be unrepresentative.
- Take-away: before costs retail day traders are roughly zero-sum; after costs and with behavioural errors, the base rate of persistent profit is under ~3-5%. Our four failures (-19%, -20%, -7%, -4%/yr at 5 bps) are consistent with the base rate. Prior for a new rule passing a 10-year gate: low, perhaps under 5-10%; a pass should be treated as suspect until out-of-sample on fresh data.

## Overall ranking (what is worth testing on fake money, free data)

1. **Cost-model upgrade** (per-instrument bps table). Not a strategy, but changes the reading of all prior fails (cheap).
2. **Leveraged-ETF close rebalancing** (TQQQ/SQQQ/SPXL, long-only, 15m bars). Strongest "instrument-level" idea; weak but non-zero published support.
3. **Bitcoin/ETH intraday momentum (Shen-Urquhart-Wang)** with realistic 15 bps/side fee. Published, 24/7, data free, but expect fee drag to kill it.
4. **Vol-targeted sizing overlay** on the slow strategies / leveraged-ETF idea (reduces tail; Harvey et al.).
5. **Inverse-ETF "short" proxies** (SH/SDS/SQQQ) as a stepping stone to real shorting, then a short-selling ADR if the proxy shows an edge.

Not worth testing: 0DTE options (no free data, negative-sum evidence), futures build-out, perp leverage, crypto calendar effects.

## Open questions for the PM
1. Is a flat bps model acceptable for new pre-registrations, or do we implement the per-instrument table (needs a small cost-model change in sim)?
2. Should the leveraged-ETF candidate be run under the same 5 bps verdict as rounds 1-2 for comparability, with the table as a diagnostic, or the reverse?
3. Do we want a short-selling ADR now (medium effort) or only after the inverse-ETF proxy test?
4. Are leveraged ETFs acceptable in the product given the owner is new to investing (tail: -82% to -91% in 2022)? Recommend simulation-only labelling plus a per-day-loss readout.
5. Crypto fee tier for the sim: which venue do we model? (Needs a verified fee schedule; my 10-40 bps figures are from memory.)
6. Verify before use: Alpaca's margin/shorting requirements and current PFOF disclosure; SOXL/TQQQ split adjustment in Alpaca bars; the Crypto Carry survey Sharpe numbers.
