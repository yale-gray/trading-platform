# Index-fund strategy rework (2026-10-21)

## Pre-registration (written before any candidate was run)

Problem: on real data `core_momentum` made 2.4%/yr vs SPY 14.1%, and `mean_reversion` 1.7%/yr.
Hypotheses and the four candidates fixed in advance (no parameter search; parameters are the
defaults below, the gate's own +-20% sweep is the only variation):

1. `index_mom_equal`: core_momentum rules, equal weight (sleeve `vol_scaled=false`). Tests whether inverse-vol sizing caused the loss.
2. `index_trend_core`: SPY + QQQ, each held while above its 200-day average, else its slot goes to IEF (monthly, equal weight).
3. `index_dual_momentum`: SPY vs EFA by 12-month return, held only if that return is > 0, else IEF (monthly).
4. `index_equity_mom`: top 2 of SPY/QQQ/IWM/EFA/EEM by 126-day return above the 200-day average, else IEF (no bonds/gold/sector in the universe).

Setup: cached real data only (`MarketData(offline=True)`), 2007-01-03 to 2026-10-20, fees 0 bp
plus 5 bp slippage (equity default), caps from the Part 1 fix (single-strategy caps, no crypto cap),
production gate (`WalkForwardConfig.defaults("equity")`, default `GateConfig`, 30% OOS read once).
Gate 6 (forward paper trading) is pending by design. "Pass" below means gates 1-5. The gate is run
with the kill switch off (strategy edge, ADR A16); full-period numbers are shown both ways.
Selection rule: pick the best candidate only if it passes gates 1-5, otherwise recommend the fallback.

## Results (4 candidates tried, 0 passed)

Full period 2007-01-03 to 2026-10-20, kill switch off (strategy edge). SPY buy-and-hold over the same window: CAGR 10.9%, max drawdown -55.1%, Sharpe 0.63, Sortino 0.89.

| Candidate | CAGR | Max DD | Sharpe | Sortino | Trades | G1 | G2 | G3 | G4 | G5 |
|---|---|---|---|---|---|---|---|---|---|---|
| index_mom_equal | 3.8% | -30.2% | 0.34 | 0.46 | 215 | pass | FAIL | FAIL | pass | FAIL |
| index_trend_core | 9.4% | -33.7% | 0.67 | 0.93 | 38 | pass | FAIL | FAIL | pass | pass |
| index_dual_momentum | 6.1% | -33.7% | 0.47 | 0.64 | 33 | pass | FAIL | FAIL | pass | pass |
| index_equity_mom | 7.2% | -30.6% | 0.53 | 0.72 | 103 | pass | FAIL | FAIL | pass | FAIL |

With the kill switch on (20% drawdown, permanent for the run): index_mom_equal 0.9%/yr (tripped 2011-09-26), index_trend_core 5.4% (2018-12-24), index_dual_momentum 1.1% (2011-11-23), index_equity_mom -0.1% (2008-06-10). A tripped switch in a 20-year backtest stops all buying for the rest of the run, so the switch-on numbers mostly measure when it tripped.

Gate details (walk-forward = 10 windows; OOS = 2020-11-05 to 2026-10-20, read once):

- index_mom_equal: G2 Sharpe>0 in 40% of windows (need 60%), drawdown<=SPY in 50% (need 60%). G3 OOS Sharpe 0.67 vs SPY 0.97, Sortino 0.94 vs 1.42; drawdown 15.4% vs 24.5% ok. G5 collapsed at low mom_lookback and the low corner. Why: equal weight helps (2.4% to 3.8%/yr) but the 16-name universe with 3 slots still rotates into bonds/gold/sectors and whipsaws (215 trades); weak edge, fragile to the lookback.
- index_trend_core: G2 Sharpe>0 in 70% ok, drawdown<=SPY in only 30%. G3 OOS Sharpe 0.87 vs 0.97, Sortino 1.24 vs 1.42, drawdown 24.9% vs 24.5% (all three marginally short). G5 no variant collapsed. Why: closest to SPY (9.4% vs 10.9%/yr, better full-period Sharpe 0.67 vs 0.63, drawdown -34% vs -55%), but the 2020-26 OOS window is a strong bull market where SPY's own drawdown (24.5%) is mild, so a trend filter has little to protect and pays its whipsaw cost.
- index_dual_momentum: G2 Sharpe>0 in 80% ok, drawdown 50%. G3 OOS Sharpe 0.61 vs 0.97, Sortino 0.88 vs 1.42, drawdown 24.1% vs 24.5% ok. G5 pass (5 variants, 1 skipped). Why: switching to EFA/IEF forgoes US large-cap leadership; only 33 trades.
- index_equity_mom: G2 Sharpe>0 in 60% ok, drawdown 40%. G3 OOS Sharpe 0.65 vs 0.97, Sortino 0.91 vs 1.42, drawdown 24.9% vs 24.5%. G5 collapsed at the low corner (Sharpe). Why: more holdings of weaker indexes (IWM, EFA, EEM) dilute SPY/QQQ returns and 103 trades add cost.

Common cause: gate 3 asks for a higher OOS Sharpe and Sortino than SPY with no more drawdown, over a 2020-2026 window in which SPY is hard to beat risk-adjusted. Gate 2's drawdown test (windows with drawdown <= SPY) also fails for every candidate. All four lower the full-period drawdown a lot (-30% to -34% vs -55%) but give up return.

## Recommendation

No candidate passes gates 1-5 (gate 6 untested). Number tried: 4, so any pass would have needed a multiple-testing discount. Do not promote any of them. Fallback for the index-fund core: a simple SPY/QQQ buy-and-hold (no signals, no turnover, gate-3 trivially equals the benchmark). If the owner wants some drawdown protection, `index_trend_core` is the least-bad (it gave back 1.5%/yr for a 21-point smaller worst drawdown and failed gate 3 only narrowly), but it is not validated.

Findings to carry forward:
- The kill switch, as a permanent 20% fund drawdown halt, fires early in every long backtest (2008 to 2018) and ends the run. Its design (cooling-off or re-arm rule) needs a decision before it is used for a 10k account.
- `vol_scaled=false` is a sleeve option, not part of the strategy YAML. Using any of these candidates in the paper fund needs the sleeve config set accordingly.
- Start 2007-01-03 was used (data starts 2005); the history gate measures the span of the strategy calendar after warm-up, so a later start is not needed for gate 1 (21y). A start 10y after the data start would shrink the in-sample period to the walk-forward minimum.
