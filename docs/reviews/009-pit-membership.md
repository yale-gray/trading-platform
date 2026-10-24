# Audit 009: point-in-time S&P 500 membership (ADR-0001 A32)

Auditor: quality-lead. Date: 2026-10-23. Scope: uncommitted A32 changes. Verdict: **PASS-WITH-NOTES for the membership logic; FAIL for pre-registration until B1 and M1 are decided.**
Checks run: 284 related tests pass, ruff clean. No backtest of tA_st_reversal_pit was run. I ran only synthetic 140-bar checks (scratchpad) and read-only scans of the cache (no strategy P&L).

## Blockers
**B1. No delisting rule: a held symbol whose bars end is never closed (src/tradebot/strategy/engine.py `_rules` ~L560-600; sim/broker.py L268-275).**
- A synthetic run confirmed it. The exit signal is true (bars_held is counted on the union calendar), so a flat intent is issued, but the SELL has no bar to fill on. It expires after `max_pending_bars=5`, and the flat intent is re-issued at the next decision.
- The position stays open to the end of the run, marked at the last close forever. It ties up about 1/max_pos of equity as dead cash, inflates exposure, and does not count toward `kept`, so new entries keep coming.
- `FLATTEN_NO_BAR` exists only for the intraday flatten path.
- Daily equity has no equivalent. A bankruptcy or seizure is credited at the last close instead of about 0.
- See section 3 for the count and the recommendation.

## Majors
**M1. The Alpaca daily series contain filler bars and spliced entities, and the strategy would trade them.**
- Alpaca returns flat, zero-volume carry-forward bars after delisting: O=H=L=C, volume 0.
- It then splices a *different* company that later reused the ticker. Example: MON is 127.95 until 2021-03-15, then 9.80 (a SPAC). The same happens for PX, SE, EMC, CA, XL, LLL, HCP, ADT, DNB, CTRA, MNK, DO and CHK (reorganisation).
- 30 universe series have flat dead bars. 22 of them resume as a different price level.
- **Effect on the trial:** the membership gate hides most of this. Delisting-while-held is partly masked because the dead bars fill at the deal price.
- **Spurious rank inputs on member days (no outcome data used):**
  - ARNC 2020-04-01, -89%, 4 member days.
  - WRK 2016-05-16, 199 to 30.7, 5 days.
  - TGNA 2017-06-01, 58 to 12, 2 days.
  - EVHC 2016-12-02, +232%.
  - Each makes the name the "worst 5-day loser" or distorts it on a corporate-action artifact, so it occupies a slot. Adjustment=all did not adjust these events.
- The doc claim "returns agree" was tested only on 40 large survivors (A32 doc, "Adjustment consistency"). It does not cover removed names.
- **Fix:** before the run, drop or quarantine dead bars (volume 0 and flat O=H=L=C). Add a one-day |ret| > 40% quarantine for Alpaca-sourced names, or at least list these artifacts in the pre-registration. Pre-registrable minimum: require volume > 0 in `entry`, or treat dead bars as missing and apply the B1 rule.

**M2. `pit_prices` `junk()` / validate_bars passes zero-volume filler (data/quality.py L186-194 is warning-only).** This is the root cause of M1. Add a trailing-flat-bar strip in `fetch_alpaca_daily`.

## Minors
- m1. `sp500_aliases.csv` IR->TT `until 2020-03-02` combined with the TT row starting 2020-03-03 makes TT a non-member on trading day 2020-03-03 only (checked: TT flags 1,0,1 on 03-02, 03-03, 03-04). Fix: the alias `until` should be 2020-03-03 for a gap-free handover, or the TT row should start 2020-03-02. The IR mapping is otherwise correct (S&P press release 2020-02-27: Gardner Denver took the IR ticker effective 2020-03-03).
- m2. `sp500_member_flags` returns 0.0 before the history start (2014-06-02 and earlier) while `sp500_members` raises DataError. The two are inconsistent. Harmless for 2016+, but a 2014/2015 warm-up would silently be non-member (universe.py L180-210).
- m3. 3,108 of 1,013,850 member-days (0.31%) in 2016-2023 have no bar (about 3 symbols per day). NaN means entry false, which is correct and causal. SNDK, STI and TE are absent from the yaml (matches the coverage doc).
- m4. PARA (yfinance) has 214 member days of flat zero-volume bars. Ranks 0%, harmless.
- m5. The coverage doc says it "did not check" how the simulator handles series ends (point 3). This audit answers it: B1.

## 1. Lookahead: PASS
- `_member` (engine.py L437-449) uses only the dates of the symbol's own bar index, never prices. Membership for date d is read at `_prev_session(d)`, so it is causal by construction.
- Concrete boundaries, as run:
  - MON: end 2018-06-07 is exclusive. The flag is 1 on 06-07 and 0 on 06-08.
  - TWTR: start 2018-06-07. The flag is 0 on 06-07 and 1 on 06-08.
  - META after the FB alias: 1 throughout the 2022-06 rename.
  - Counts: 502 members on 2016-01-04 and 503 on 2023-12-29.
- Weekends and holidays use the same previous session.
- The existing universe-truncation tests pass. A dedicated truncation test for `{universe: sp500_member}` is worth adding (dropping future bars cannot change the flag), but nothing leaks.
- Source caveat: the dataset records dates when lists changed. Retroactive source corrections are not visible. That is acceptable.

## 2. Data spot-check
Dataset interval vs real event:

| Symbol | Dataset | Real event | Verdict |
|---|---|---|---|
| MON | ends 2018-06-07 | Bayer closed 2018-06-07 | OK |
| CELG | ends 2019-11-21 | BMS closed 11-20 | OK |
| TWTR | ends 2022-11-01 | Musk close 10-27 | OK (bars end 10-27, member lags) |
| ATVI | 2015-08-31 to 2023-10-18 | MSFT close 10-13 | OK |
| AGN | ends 2020-05-12 | AbbVie close 05-08 | OK |
| RTN | ends 2020-04-06 | merger 04-03 | OK |
| XLNX | ends 2022-02-15 | AMD close 02-14 | OK |
| SIVB / FRC / SBNY | removed 2023-03-15 / 05-04 / 03-15 | actual removals | OK |
| LNKD | absent | never in the S&P 500 | OK |
| IR / TT | alias until 2020-03-02 | see m1 | OK |

- Aliases spot-checked: FB->META, BBT->TFC, UTX->RTX, WLTW->WTW, HRS->LHX are correct.
- Wrong-entity risk for aliases: none seen.
- Strategy file vs tA_st_reversal: only id, name, description, comments, universe and entry differ (diff checked). Parameters are identical.
- Alpaca vs yfinance: see M1.

## 3. Delisting and attrition (cache scan, 2016-2023)
- 84 universe series end inside the window. 68 were still members within 5 sessions of the last bar. The rest ended after removal.
- Another 22 delistings are masked by filler bars (M1).
- Plausible hits for this strategy: about 68 events x roughly 2% base chance of being held, so about 1.4 events by chance. Distressed names are over-picked by a worst-loser rule.
- Named likely hits: FRC (a reversal buy on its late-April 2023 collapse, last bar 2023-04-28, held at the end, then stuck) and SIVB (a buy signal on its last bar 2023-03-09 that never fills, so an expired pending order only).
- Size of the effect: a stuck FRC-type position at 10% weight is credited about -50% instead of about -100%. That is about a 5% equity optimism for one event, plus dead-cash drag. Probably 0-3% on cumulative equity overall, one-directional.
- **Recommendation (causal, pre-registrable):** on the first calendar bar where a held symbol has no bar while other symbols do, force-sell in the same step.
  - Price: the last close, with 50 bp extra sell slippage (reuse `FLATTEN_NO_BAR_EXTRA_BPS`).
  - Write-off: if the symbol's trailing 5-bar return before its last bar was <= -50%, the sale price is 0 (conservative; matches FRC, SIVB, SBNY and bankruptcies).
  - Count these events in the report.
  - Execution-lead owns this, as a sim/driver change. The handling must be written into the pre-registration before any run.

## 4. Silent-error scan
- Ordering: the universe is sorted. Candidate ties break on (score, symbol), so results are deterministic.
- NaN warmup and no-bar names give an entry of False, so they are never candidates.
- The 0.0 before 2014-06-02 is irrelevant for the 2016-2023 window (m2).
- `run_backtest` validates and loads bars for all 669 symbols. Cache-only (refresh=False) is required, as documented.
- Residual survivorship bias is stated honestly in the coverage doc. This audit supports its estimate: 0.31% of member-days have no bar.
