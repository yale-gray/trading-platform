# Cutting-edge ML / AI for intraday trading: what is real, what is hype (2026-10-23)

Author: platform-lead (research only, no code). Angle: newest credible evidence, 2020 to 2026.

**Evidence caveat.** Findings below come from web-search abstracts and summaries. Full papers were not read. Where a number comes from a secondary source it is marked "(secondary)". Anything marked UNVERIFIED should be checked against the original before it goes into an ADR.

**Context.** Four pre-registered hand-written rules failed on 10.6y of 15-min data (`daytrading-2026-10.md`, `daytrading-round2-2026-10.md`). The pattern: gross edge of about 0 to 3 bps per trade, costs of 10 bps per round trip (5 bps slippage a side). Anything new must clear that bar. The honest framing for the owner: **ML does not remove the cost problem. It can only help if it finds a bigger or more concentrated edge per trade, or lets us trade less.**

## Summary table

| # | Method | Evidence quality | Fit to our system | Effort | Risk of fooling ourselves | Verdict |
|---|---|---|---|---|---|---|
| 1 | ML cross-sectional return prediction (GBM/NN on features) | Strong for monthly (peer-reviewed); intraday on 5-min is peer-reviewed but small-cap/cost-sensitive | Good: a score column feeding YAML rules, retrained walk-forward | M (daily/overnight horizon) to L (intraday) | High (overfitting) | **TEST**, at a slow horizon first |
| 2 | Deep learning on limit-order books (DeepLOB etc.) | Academic benchmarks degrade on new data | None: we have no LOB data and no latency | L+ (impossible with our data) | n/a | **AVOID** |
| 3a | LLM news/headline sentiment scored per stock | Peer-reviewed-ish, moderate; decays as adoption grows | Good: Alpaca news plus a Claude score as a column | M | High (training contamination) | **TEST**, only with a forward (post-cutoff) window |
| 3b | LLM trading agents (FinMem, TradingAgents, etc.) | Weak: short windows, fail on long horizons | Poor (non-declarative, costly, non-reproducible) | L | Very high | **AVOID** (use LLM as a feature, not as the trader) |
| 4 | Reinforcement learning (FinRL etc.) | Weak: mostly authors' own backtests | Poor fit to YAML rules | L | Very high | **AVOID** |
| 5 | Regime detection (HMM, volatility regimes) as on/off switch | Mixed; fine as a risk filter, not an alpha source | Excellent: one indicator column | S | Medium | **TEST** (cheap), as a gate on a rule that already has an edge |
| 6 | Overfitting controls: deflated Sharpe, PBO, purged/CPCV, meta-labeling | DSR/PBO: well-established theory. Meta-labeling/CPCV: thin independent evidence | Strengthens our gate | S to M | Reduces it | **ADOPT DSR/PBO now**; meta-labeling later |
| 7 | Other: ML on market-level intraday from the cross-section; overnight/close effects; earnings/news timing | Peer-reviewed, ETF-level, after costs | Good: SPY/QQQ only, few trades | M | Medium | **TEST** (best fit to our cost problem) |

## Common thread: why this probably does not rescue 15-min day trading

1. Our 15-min SIP data is the same information everyone has. Published ML intraday edges are mostly in small, illiquid names or in the first hours after a news shock, exactly where 5 bps slippage and a 3-bar (45 min) fill delay destroy them.
2. Our fill lag (signal at bar t, fill at bar t+3 open) throws away the fast part of any short-horizon signal. A model with a 15-minute prediction horizon is nearly worthless to us.
3. Cost per round trip is about 10 bps. A model needs predicted moves well above 10 bps after the fill lag, with trade counts of a few hundred a year, not 1,200.
4. Therefore the experiments below favour **slower horizons (next-day, overnight, multi-day), few trades, high-liquidity ETFs, and ML as a ranking/filter**.

---

## 1. ML cross-sectional return prediction

**Plain English.** Feed many stock features (past returns, volatility, volume, liquidity) into gradient-boosted trees or a neural net, and have it predict which stocks will outperform next. Rank, buy the top few.

**Evidence.**
- Gu, Kelly and Xiu, "Empirical Asset Pricing via Machine Learning," Review of Financial Studies 2020. Trees and neural nets beat linear models, driven by nonlinear interactions among momentum, liquidity and volatility signals. This is a **monthly** horizon on a broad universe, peer-reviewed. https://papers.ssrn.com/abstract=3159577
- Avramov, Cheng and Metzker, "Machine Learning vs. Economic Restrictions." Deep-learning profits come mostly from hard-to-arbitrage stocks, high-limits-to-arbitrage periods, and attenuate when microcaps, distressed and high-volatility stocks are excluded. **They deteriorate further with reasonable trading costs.** https://cris.huji.ac.il/en/publications/machine-learning-vs-economic-restrictions-evidence-from-stock-ret/
- Intraday: Huddleston, Liu and Stentoft, Journal of Financial Econometrics 2023: ML ensembles on lagged constituent returns at **5-minute** frequency; reported Sharpe 0.98 after transaction costs. Whether their cost model matches 5 bps a side plus a 45-minute fill lag is unverified and unlikely. https://ideas.repec.org/a/oup/jfinec/v21y2023i2p485-527..html
- Echo-state-network intraday cross-section (arXiv 2504.19623): short horizons (10 min) have the lowest costs; not peer-reviewed, benchmark is a random forest. https://arxiv.org/pdf/2504.19623

**Pitfalls.**
- Look-ahead through features (using a bar's close in a feature that is "known" only after), and through target construction with overlapping labels.
- Survivorship bias: our universe is the current S&P 500 (ADR A13). A model that learns "stocks that ended up in the S&P" has a free, fake edge. This is worse for ML than for simple rules because the model can find the leak.
- Data snooping across hyperparameters and feature sets. Cost blindness: most papers use mid-price or small fixed costs.
- Concentration of profit in the smallest/most volatile names; our universe is mega/large caps only, so expect smaller effects than the papers.

**Compute and data.** 860MB of 15-min bars is small. LightGBM on about 500 stocks x 10y x 26 bars/day is tens of millions of rows at the intraday level. Trainable on a laptop in minutes. Daily-horizon version is trivial.

**Fit to our architecture.** A `ml_score` indicator column written by an offline walk-forward trainer (train on years 1..k, score year k+1, step forward, strictly per-fold). YAML rules then say e.g. `ml_score_rank <= 5 and time >= 09:45`. The gate already handles OOS and walk-forward. Key rule: **the score column for any bar must come from a model trained only on data before that bar, with an embargo longer than the label horizon.** Store the model version and fold in the column metadata.

**Effort.** M for a daily/overnight horizon using existing daily data and the intraday-aggregated features. L for a true intraday model with fold plumbing, embargoes and a point-in-time universe.

**Risk.** High. Most of the risk is silent leakage rather than modelling.

**Recommendation: TEST, slow horizon first.**

**First pre-registrable experiment (E1).** Target: next-day open-to-close or close-to-next-close return rank across the 501 stocks, label purged by 1 day. Features: from 15-min bars aggregated to day-level features (overnight gap, first-hour return, last-hour return, realized vol, volume surge, 1/5/20-day returns). Model: LightGBM, fixed hyperparameters written before running, expanding-window walk-forward with annual refit, 5-day embargo. Trade: long-only top 5 by score, signal at 15:30 close, fills per our lag rules, flatten rule per strategy spec (if holding overnight is not allowed by the engine this becomes open-to-close only). Pass condition fixed in advance: net of 5 bps/side, OOS Sharpe above SPY's and deflated Sharpe probability above 0.95 given the number of configurations tried (record N). Also report the **gross** edge in bps per trade and a placebo run with shuffled labels (must return about 0). Survivorship control: also run on the subset of stocks that were already in the S&P 500 in 2016 and still are, and flag any difference.

---

## 2. Deep learning on limit-order-book data (DeepLOB, transformers)

**Plain English.** Neural nets read the full order book (bids/asks at several price levels, updated tick by tick) and predict the next few seconds' mid-price direction.

**Evidence.** DeepLOB (Zhang, Zohren, Roberts) reported strong results on a benchmark dataset. The LOBCAST benchmark of 15 models reproduced results on FI-2010 and then found that all models drop significantly on unseen LOBSTER data, raising doubts about real-world use. https://arxiv.org/abs/2308.01915v1 , original https://arxiv.org/pdf/1808.03668.pdf . How costs are treated in LOBCAST's profit analysis is not confirmed.

**Why it does not transfer to us.**
- We have no order-book data (Alpaca SIP bars are OHLCV; LOB history is expensive).
- The signals live at millisecond-to-second horizons. Our fill lag is 45 minutes, and retail latency plus a 5 bps cost make them irrelevant. Predicting a 1-tick move is worth less than the spread.
- The edge in this literature is mostly in predicting moves smaller than the bid-ask spread, which a taker cannot capture.

**Effort.** Not feasible with current data. **Recommendation: AVOID.** No experiment.

---

## 3. LLM-based trading

### 3a. LLM headline/news sentiment as a feature

**Plain English.** Ask an LLM "is this headline good, bad or irrelevant for the stock?" and use the score as a signal.

**Evidence.**
- Lopez-Lira and Tang, "Can ChatGPT Forecast Stock Price Movements?" Scores predict next-day returns; later versions (v5/v6) say the drift is concentrated in **small stocks and negative news**, that predictive power rises with model size, and that **strategy returns decline as LLM adoption rises** (an efficiency-erosion warning). Headline claims of about 90% hit rate refer to the initial non-tradable reaction. https://arxiv.org/abs/2304.07619v5 and https://arxiv.org/pdf/2304.07619v6 (search summary only; methodology not verified).
- Chen, Kelly and Xiu, "Expected Returns and Large Language Models": LLM news embeddings predict next-day returns across 16 equity markets; prices respond slowly to news, with largest gains where narratives are complex; the abstract cites limits to arbitrage. https://arxiv.org/html/2502.21206v2 (authorship not confirmed by the snippet; confirm before citing).
- He, Lv, Manela and Wu (2025): ChronoBERT, trained only on text available at each point in time, gets a Sharpe comparable to state-of-the-art, which suggests look-ahead bias is **modest** for that task. (secondary: described in search summary.)

**Pitfalls.**
- **Training contamination.** The LLM may have memorised what happened after a headline. Evidence: GPT-4o can recall S&P 500 levels and individual stock prices before its cutoff (arXiv 2504.14765, https://arxiv.org/html/2504.14765v2); Somoza, Didisheim and Fraschini (Economics Letters 2025) find bias scales with data frequency, model size and aggregation; Gao, Jiang and Yan (arXiv 2512.23847, https://arxiv.org/pdf/2512.23847v1) build a "Lookahead Propensity" score. A May 2026 paper (arXiv 2605.24564, https://arxiv.org/abs/2605.24564) cuts in-sample backtest returns by up to 67.1% on memorised dates when penalising recall. **For us: Claude's training cutoff is the dividing line. Any backtest period before it is contaminated for the scorer. Only data after the model's cutoff is clean, and that is a short window.**
- Headlines also come with timestamps that are hard to align to the bar (publication vs. availability). Edge is mostly in the first minutes, which our 45-minute fill lag misses.
- Alpaca's free news API: coverage, history depth and timestamp reliability need checking (UNVERIFIED here).
- Cost: scoring hundreds of thousands of headlines with a frontier model is real money; use a small model plus cache.

**Fit to our architecture.** A `news_score` column per stock per bar (latest score within the prior N bars, decayed), with the score computed once and stored with model id and timestamp. The YAML rule consumes it like any indicator. Scoring must be done blind: prompt contains only headline text and ticker, no dates or prices.

**Effort.** M. **Risk.** High (contamination). **Recommendation: TEST, forward-only.**

**First experiment (E2).** Pre-register: (a) score all Alpaca headlines for the 501 stocks from the latest period after the scoring model's documented training cutoff, with a blind prompt (no dates). (b) Signal: top-decile positive-score names, entering at the next 15-min open after publication plus our standard lag, hold to close. (c) Primary metric: mean net bps per trade and t-stat vs random same-day same-stock entries. (d) Contamination control: run the same on a pre-cutoff window and on a window of headlines with the company name masked; if pre-cutoff results are much better than post-cutoff, treat the pre-cutoff results as contaminated. (e) Pass requires post-cutoff net positive with a deflated-Sharpe adjustment. Expect the sample to be small; if fewer than about 300 trades post-cutoff, the conclusion is "insufficient data", not "works."

### 3b. LLM trading agents (FinMem, FinAgent, TradingAgents, FinGPT-style)

**Plain English.** An LLM reads news, prices, filings and decides what to buy, often with memory and multiple "analyst" roles.

**Evidence.**
- FINSABER (arXiv 2505.07078): previously reported LLM advantages deteriorate over 20 years and 100+ symbols; FinMem and FinAgent were originally evaluated on about 6 months and 5 to 6 symbols; authors cite survivorship, look-ahead and data-snooping bias; LLM strategies were too conservative in bull markets and too aggressive in bear markets. https://arxiv.org/html/2505.07078v2
- Live benchmarks avoid leakage but are short: LiveTradeBench (50-day live, 21 LLMs; high LMArena scores do not imply better trading) https://arxiv.org/html/2511.03628v1 ; "When Agents Trade" (two months; agent architecture mattered more than the LLM backbone) https://arxiv.org/pdf/2510.11695 ; Alpha Arena Season 1 (crypto perps, Oct to Nov 2025; huge dispersion, GPT-5 about -40%, DeepSeek about +10% in early snapshots; a few weeks, not statistically meaningful) (news summaries). StockBench: secondhand abstract says most LLM agents struggle to beat buy-and-hold (UNVERIFIED protocol).

**Pitfalls.** Contamination, non-determinism (same prompt, different action), cost, no way to run our gate meaningfully (a stochastic decision-maker has no fixed "parameters" to sensitivity-test).

**Recommendation: AVOID as a trader.** Use the LLM only as a bounded feature extractor (3a), where outputs are logged and frozen. This also informs the Phase-6 strategy builder: the builder should emit declarative YAML that runs through our gate, never "let the LLM decide trades live."

**Earnings-call analysis** is a sub-case. Same contamination issue (transcripts and subsequent price moves are widely discussed online), event-driven so few trades. Treat as a variant of E2 using post-cutoff earnings only. Not separately sourced here.

---

## 4. Reinforcement learning (FinRL and similar)

**Plain English.** An agent learns by trial and error in a simulated market to maximise reward.

**Evidence.** FinRL-Meta (NeurIPS 2022 datasets track) is mainly a library and benchmark; its own text lists low signal-to-noise, survivorship bias and backtest overfitting as core obstacles, and a sim-to-real gap. Its reported ensemble Sharpe 1.53 is the authors' own backtest. https://arxiv.org/pdf/2211.03107 . Gort et al. (arXiv 2209.05559) propose estimating the probability of backtest overfitting for DRL crypto agents and rejecting overfitted ones, tested on 10 coins over a short 2022 window. https://arxiv.org/pdf/2209.05559 . I found no independent, long-horizon, cost-inclusive, live validation of RL for equities.

**Pitfalls.** Extremely high variance across random seeds, reward hacking (agent exploits simulator quirks, e.g. fill assumptions), tiny effective sample size (one history), and results that cannot be audited through a declarative-rule gate.

**Fit.** Poor. An RL policy is a black box, not a YAML rule.

**Recommendation: AVOID.** If ever revisited, run it as a score-producing model under the same walk-forward gate as E1 and compare to LightGBM. It would have to beat a boring model to justify the complexity.

---

## 5. Regime detection (HMMs, volatility regimes)

**Plain English.** Estimate whether the market is in a calm or turbulent state and switch strategies on or off accordingly.

**Evidence.** Nystrup, Madsen, Lindström and coauthors have peer-reviewed work on regime-based dynamic allocation (Quantitative Finance 2018; Journal of Asset Management 2016). Their usual finding is that regimes help mainly by **reducing risk/drawdown**, less by raising returns; out-of-sample numbers not verified here. https://ideas.repec.org/a/gam/jrisks/v7y2019i2p66-d239237.html . Practitioner caution: more states fit history better and destabilise out of sample (non-peer-reviewed, https://www.luxalgo.com/library/concept/hidden-markov-markov-switching-regimes.md ). FINSABER's conclusion that regime-awareness matters more than architectural complexity is consistent (https://arxiv.org/html/2505.07078v2). The Aleti-Bollerslev-Siggaard result (section 7) also says most excess performance comes from high-uncertainty periods.

**Pitfalls.** HMM states are re-estimated with hindsight (smoothed probabilities use future data; only *filtered* probabilities are legitimate). State labels flip between refits. A regime filter on a rule with no edge cannot create one.

**Fit.** Excellent and cheap: a `regime` column (e.g. trailing realized vol percentile, VIX-like level from SPY, or filtered 2-state HMM probability), usable in YAML as `regime == high_vol`. No ML needed to start; a simple vol percentile is the null model the HMM must beat.

**Effort.** S. **Risk.** Medium (mostly forking-paths: many ways to define a regime).

**Recommendation: TEST, as a gate on something that already shows gross edge.** Our rules do not, so this is a modifier for E3/E4, not stand-alone.

**First experiment (E5).** Use `etf_dt_opening_drive` (gross +2 to 3 bps per trade) as the base rule. Pre-register one regime definition: SPY trailing 20-day realized vol above its trailing 252-day median = "high-vol". Trade only in high-vol days (known at prior close). Compare to unconditional, report trade count, gross bps, net bps. Null: a coin-flip day filter with the same trade count (1,000 shuffles). Also report the filtered 2-state HMM variant as a second, separately labelled strategy id. Expected outcome stated up front: net still negative at 5 bps; the point is to see if the gross edge in high-vol days exceeds about 10 bps.

---

## 6. Overfitting controls: ensembles, meta-labeling, CPCV, deflated Sharpe

**Plain English.**
- **Deflated Sharpe ratio (DSR):** a Sharpe ratio corrected for how many strategies you tried and for non-normal returns. Answers "how likely is this Sharpe just the best of N lucky tries?"
- **Probability of backtest overfitting (PBO):** via combinatorially symmetric cross-validation, the chance the best in-sample configuration is below median out of sample.
- **Purged / combinatorial purged CV:** cross-validation that removes training samples whose labels overlap the test window and leaves an embargo gap, then forms many train/test path combinations.
- **Meta-labeling:** a first rule says when to trade (side); a second model decides whether to take that particular trade (size/skip). It reduces trade count, which matters for our cost problem.

**Evidence.**
- DSR: Bailey and López de Prado, Journal of Portfolio Management 2014. https://papers.ssrn.com/abstract=2460551 . PBO: https://papers.ssrn.com/abstract=2326253 . Both have strong theoretical grounding and wide use; caveats: you must record N (trials) and trial variance honestly, and CSCV shuffles blocks so it complements rather than replaces walk-forward.
- Meta-labeling and CPCV: described in López de Prado's *Advances in Financial Machine Learning* (2018). My searches found **no independent out-of-sample studies** showing meta-labeling improves net performance; one small controlled experiment on EURUSD ticks found no reliable edge for classic rules (MQL5 article, low-quality source: https://www.mql5.com/en/articles/23310). Treat as plausible engineering practice, not proven alpha.

**Pitfalls.** Meta-labeling a primary rule with zero edge just produces a confident model of noise. Overlapping labels leak across folds unless purged. The need for a trial counter is the part people skip.

**Fit.** This is the strongest immediate fit. Our gate is already stricter than most. Additions:
1. A persistent **trial registry**: every strategy id, parameter sweep point and ML configuration ever run increments N. Compute DSR per candidate from it.
2. Purge + embargo in any ML fold (embargo at least the label horizon + our 3-bar fill lag).
3. PBO across the parameter sweep we already run for sensitivity.

**Effort.** S (DSR and trial registry), M (purged/CPCV splitter). **Risk.** Lowers it.

**Recommendation: ADOPT DSR + trial registry now (before any ML). Meta-labeling: LATER, only after a primary rule shows positive gross edge.**

**First experiment (E6, a retrospective).** Compute the DSR and PBO for the four failed strategies and their sweeps using the existing results, with N set to the actual number of variants run. Expected: DSR near 0 for all four. This validates the tooling on known failures before it is trusted on candidates.

---

## 7. Other 2024 to 2026 ideas worth knowing

1. **ML on the cross-section to predict the *market's* intraday return (best fit).** Aleti, Bollerslev and Siggaard, Management Science 71(9), 2025: lagged high-frequency cross-sectional returns on the "factor zoo," regularised ML, predict aggregate intraday market returns; strategies in liquid ETFs have sizeable OOS Sharpe ratios and alphas **after transaction costs**; most of the excess comes from high-uncertainty periods and a few tail-risk and liquidity factors. https://pubsonline.informs.org/doi/epdf/10.1287/mnsc.2023.01657 . Why it matches us: SPY/QQQ only (tight spreads), few trades, our 501-stock panel is exactly the input. Caveats: factor construction at high frequency needs point-in-time membership; claims depend on their cost model and their execution timing relative to our 3-bar lag.

   **First experiment (E3).** Predict SPY's next-30/60-minute (or rest-of-day) return from the lagged 15-min returns of the 501 stocks (ridge/elastic-net, fixed grid, expanding walk-forward, purged). Trade SPY long-only when the predicted return exceeds a pre-set threshold, fill at t+3 open, flatten at close. Report gross bps/trade, trade count, net at 5 bps and 1 bp (diagnostic only, as in round 2). Pass: same gate. Falsifier: if the signal's IC (correlation of prediction with the realised return *starting at t+3*) is not clearly positive, stop; the lag has eaten it. Effort M.

2. **Published intraday-momentum claims should be treated sceptically.** Zarattini, Aziz and Barbon "Beat the Market" (SPY, Swiss Finance Institute WP 24-97) report 19.6%/yr, Sharpe 1.33 net for 2007 to 2024 with trailing stops; I found no independent replication, and our own simplified round-2 test of this family found nothing after costs. A faithful (not simplified) replication of the paper's exact rule, including its dynamic volatility bands and trailing stop, is a legitimate cheap test: **S to M**, new strategy id, pre-registered. Source: https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy . Note most of the authors are affiliated with trading-education businesses; weigh accordingly.

3. **Overnight / close-to-open and slower horizons.** Not an ML topic, but the single most consistent lesson across both rounds and the ML literature: our cost per round trip makes sub-day trading a losing proposition unless gross edge exceeds about 10 bps. Several ML results survive costs only at daily-plus horizons. If E1 shows an edge, it will most likely be at the overnight or multi-day horizon, which conflicts with the "flatten at close" rule; that is a product decision for the PM (an ADR), not something to bypass.

4. **Efficiency erosion.** Lopez-Lira and Tang report LLM-news returns decline as LLM adoption rises. Expect any LLM-sentiment edge to be weaker in 2025 to 2026 data than in the papers' samples. Plan for the small post-cutoff window noted in 3a.

---

## Recommended order of work (value per effort)

| Rank | Experiment | Effort | Why |
|---|---|---|---|
| 1 | E6: DSR + trial registry + PBO, retro-applied to the four failures | S | Cheap, strengthens every later test, no new data |
| 2 | E3: cross-sectional ML predicts SPY/QQQ intraday (Aleti-Bollerslev-Siggaard style) | M | Few trades, tight spreads, peer-reviewed, uses the data we have |
| 3 | E1: LightGBM stock ranking at a next-day horizon, walk-forward | M | Largest literature; slow horizon respects costs; needs survivorship control |
| 4 | E5: regime gate (vol percentile, then HMM) on the opening-drive rule | S | Cheap; tells us whether any sub-regime has more than 10 bps gross |
| 5 | E2: LLM headline score, post-cutoff window only | M | Plausible edge, but contamination makes it the most delicate; small sample |
| 6 | Faithful replication of Zarattini-Aziz-Barbon | S to M | Cheap sanity check on the one high-profile retail claim |

Avoid: DeepLOB-type models, LLM-as-trader agents, RL.

## Implementation guardrails (apply to every ML experiment)

1. Pre-register in a committed file before running: features, target, model class, hyperparameter grid, fold scheme, embargo, cost model, pass criteria, N counter entry.
2. Score column is strictly point-in-time; a unit test asserts the model for bar t was trained only on data ending at least (label horizon + 3 bars + embargo) earlier.
3. Report gross edge in bps per trade first. If gross is below about 10 bps, say so in the first line. This is the number that has decided every result so far.
4. Use a placebo (shuffled labels) and a naive baseline (e.g. 5-day momentum rank) in every ML report; the model must beat the baseline, not just zero.
5. Run the survivorship check: current-S&P-membership universe flatters results (ADR A13), and ML can learn the leak.
6. For LLM scoring: blind prompts (no dates, no prices), frozen outputs stored with model id, evaluation only after the documented training cutoff, contamination probe by masking tickers.
7. No real money, no live broker, secrets in `.env` only (hard rules 1 and 5). LLM API keys for scoring belong in `.env`.

## Open questions for the PM

1. Is overnight holding (breaking "flatten at close") on the table for a successful E1? Many ML edges live at 1 to 5 day horizons.
2. Can we obtain a point-in-time S&P 500 membership list (or accept the survivorship flag permanently) before E1 and E3?
3. What is Alpaca news coverage and history depth for our 501 symbols, and are timestamps publication time? (Not verified; needed for E2.)
4. Which Claude model and documented cutoff will be used to score news, and is the post-cutoff window long enough for at least about 300 trades?
5. Budget: LLM scoring cost ceiling per experiment.
6. Should DSR and the trial counter become a mandatory sixth gate item (ADR)?

## Sources (all via search results; abstracts/summaries only)

- Gu, Kelly, Xiu (RFS 2020): https://papers.ssrn.com/abstract=3159577
- Avramov, Cheng, Metzker: https://cris.huji.ac.il/en/publications/machine-learning-vs-economic-restrictions-evidence-from-stock-ret/
- Huddleston, Liu, Stentoft (JFEC 2023): https://ideas.repec.org/a/oup/jfinec/v21y2023i2p485-527..html
- Aleti, Bollerslev, Siggaard (Management Science 2025): https://pubsonline.informs.org/doi/epdf/10.1287/mnsc.2023.01657
- Echo state network intraday: https://arxiv.org/pdf/2504.19623
- DeepLOB: https://arxiv.org/pdf/1808.03668.pdf ; LOBCAST: https://arxiv.org/abs/2308.01915v1
- Lopez-Lira and Tang: https://arxiv.org/pdf/2304.07619v6
- Chen, Kelly, Xiu: https://arxiv.org/html/2502.21206v2
- LLM look-ahead/memorization: https://arxiv.org/html/2504.14765v2 ; https://arxiv.org/pdf/2512.23847v1 ; https://arxiv.org/abs/2605.24564
- FINSABER: https://arxiv.org/html/2505.07078v2
- LiveTradeBench: https://arxiv.org/html/2511.03628v1 ; When Agents Trade: https://arxiv.org/pdf/2510.11695
- FinRL-Meta: https://arxiv.org/pdf/2211.03107 ; DRL backtest overfitting: https://arxiv.org/pdf/2209.05559
- DSR: https://papers.ssrn.com/abstract=2460551 ; PBO: https://papers.ssrn.com/abstract=2326253
- Regime-based allocation: https://ideas.repec.org/a/gam/jrisks/v7y2019i2p66-d239237.html
- Zarattini, Aziz, Barbon: https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy

This is education and system design, not personal financial advice.
