# Point-in-time S&P 500: membership data and price coverage (ADR-0001 A32)

Written 2026-10-23, before any backtest of `tA_st_reversal_pit` (trial 11 of 12). Window: 2015-06-01 to 2023-12-31. "Member-day" means one trading day on which a ticker is an S&P 500 member under the loader's rule (list in force at the previous trading day's close).

## Headline

- **97.97% of member-days have a price overall (1,090,258 member-days). In the 2016-2023 development window it is 99.57%.** The worst year is **2015 at 76.4%**, because Alpaca's SIP history starts on 2016-01-04 and Yahoo no longer serves delisted names. Years 2020-2023 are at 99.99%.
- 688 tickers were members at some point in the window: 503 are still members at the end of 2023, 185 left. 669 have at least one price bar in the window and are in the strategy universe. 19 have none (list below).
- Plain meaning: in 2016-2023 about 2.1 of the roughly 505 index members on an average day have no price series, almost all in 2016-2018. That is small, but not zero (see the bias estimate).

## Membership data

- **Source:** [fja05680/sp500](https://github.com/fja05680/sp500), file "S&P 500 Historical Components & Changes (Updated).csv" (2,720 dated lists, 1996-01-02 to 2026-08-18), retrieved 2026-10-23. The raw file was kept outside the repo; `src/tradebot/data/universes/sp500_history.csv` is the trimmed copy: one row per (ticker, membership interval) from 2014-06-01, with the source URL and retrieval date in its header.
- **Cross-check:** Wikipedia "List of S&P 500 companies", "Selected changes" table, December 2023 revision (193 changes dated 2015-06-01 to 2023-12-31, 364 add/remove events). **352 of 364 events match the dataset within 20 days (298 on the same day); median gap 0 days, range -6 to +5.** The 12 misses are mostly ticker spellings that differ between the sources (e.g. YHOO/AABA, TYC/JCI, ACE/CB, CSC/DXC, DPS/KDP, WYN/WYND) plus a few entries one source lacks (the Dec 2022 MBC add/remove pair, IR and BMS), not systematic gaps.
- **Timing differences:** the dataset records a removal when the company actually delisted, and an addition on its effective date, so a "replaces" pair can sit a few days apart. Where it differs from Wikipedia the dataset is usually the more realistic tradable list.
- **Convention:** a list dated d is first used for trading on the session after d (the previous-close rule), so a change is never known on its own day. Intervals have an exclusive end.
- **Tickers:** the source lists tickers as they were at the time (FB, not META). `src/tradebot/data/universes/sp500_aliases.csv` maps 24 renames onto the ticker that carries the price history (FB to META, ANTM to ELV, ABC to COR, and so on), and `IR` is mapped to `TT` only before 2020-03-02 (Ingersoll-Rand became Trane; the later IR is a different company). A rename is only listed where the target's cached series was checked to span the same company.

## Prices: what came from where

| Source | Symbols | Member-days priced |
|---|---|---|
| yfinance (existing cache; normal `MarketData` path) | 533 | 943,861 |
| Alpaca SIP daily (`adjustment=all`), written into the same cache | 136 | 124,313 |
| none | 19 | 0 |

- yfinance first. 105 more names came through the normal path this run; 13 live names that yfinance returns but the repo's quality check rejects (a bad OHLC row, e.g. HUBB 2021-05-05) and a few one-bar stubs were replaced by Alpaca series instead. Alpaca is only a fallback; no symbol mixes two sources.
- The Alpaca provider (`providers/alpaca_provider.py`) gained daily bars (`Timeframe.D1`, `1Day`, `adjustment=all`, data host only). `tradebot.data.pit_prices` (`uv run python -m tradebot.data.pit_prices`) fetches them and writes them into the normal cache under the `yfinance` source key; each row's `source_rev` column says `alpaca_sip`.
- **Adjustment consistency.** yfinance: `Adj Close/Close` factor (splits and dividends, total return). Alpaca: splits and dividends, total return. Measured on 40 large current members over 2016-2023 (same dates): median close-level ratio 1.000 (individual names 0.96 to 1.07, because Yahoo's series also includes dividends paid after 2023-12 while the Alpaca fetch stops there), daily-return correlation median 0.999 (minimum 0.986), at most 0.7% of days differ by more than 50 bp, largest single-day gap 9% (one name). Levels differ, returns agree. Rank-by-return rules are unaffected; anything using price levels across sources would not be.
- **Alpaca history starts 2016-01-04.** Names removed before about 2016 (ALTR, DTV, FDO, HCBK, HSP, JOY, KRFT, LO, MWV, PLL, QEP, SIAL, TEG, CMCSK) have no price anywhere free.
- **The cache must be read cache-only for this universe** (`refresh=False`, as `api/jobs.py` backtests do). `MarketData.get_bars(..., refresh=True)` on an Alpaca-sourced name tries to extend it from yfinance and fails (empty frame or quarantine). This was tested on a copy of the cache. A paper-trading `tick` run with refresh would hit the same wall, so the strategy is backtest-only. I did not wire Alpaca into the `MarketData` routing; it needs a design decision (Alpaca as a second daily provider with its own cache key).

## Coverage by year

| Year | Member-days | With a price | Share |
|---|---|---|---|
| 2015 (from 06-01) | 75,200 | 57,432 | 76.37% |
| 2016 | 127,101 | 125,149 | 98.46% |
| 2017 | 126,745 | 125,555 | 99.06% |
| 2018 | 126,736 | 125,974 | 99.40% |
| 2019 | 127,260 | 126,911 | 99.73% |
| 2020 | 127,767 | 127,749 | 99.99% |
| 2021 | 127,260 | 127,245 | 99.99% |
| 2022 | 126,439 | 126,421 | 99.99% |
| 2023 | 125,750 | 125,738 | 99.99% |
| **All** | 1,090,258 | 1,068,174 | **97.97%** |
| 2016-2023 only | 1,015,058 | 1,010,742 | 99.57% |

Of the 22,084 member-days without a price, 80% are in 2015. Survivors (503 names still in the index at end-2023) are missing 0.5% of their member-days overall (0.2% from 2016); names that were removed are missing 11.4% (1.7% from 2016).

## Members with no data (19; all member-days unpriced)

| Ticker | Member-days | Why |
|---|---|---|
| STI | 1,141 | SunTrust, 2015-2019. Ticker reused (Yahoo and Alpaca STI start 2022). |
| TE | 276 | TECO Energy, to 2016-07. Ticker reused. |
| SNDK | 241 | SanDisk, to 2016-05. Ticker reused by the 2025 spin-off. |
| ALTR | 147 | Altera, to 2015-12. Pre-2016. |
| HCBK, SIAL, JOY, HSP, PLL, TEG, DTV, KRFT, FDO, CMCSK, MWV, NE, QEP, LO, FCPT | 5 to 121 each | Removed or renamed before 2016 (Alpaca starts 2016-01-04), or reused ticker (NE, FCPT). |

## Members with partial data (112 below 98% coverage; worst 25)

| Ticker | Member-days | Share priced | Reason |
|---|---|---|---|
| AABA | 518 | 0.2% | Yahoo Inc. traded as YHOO until 2017-06; the source lists AABA throughout |
| PCP, BRCM | 170 each | 11% | Removed in Jan 2016 |
| PCL, GMCR, CAM, POM, ESV, CNX, ADT, TWC, ARG, CCE, CVC, BXLT, GAS, CPGX | 184-276 | 18-50% | Removed during 2016 (2015 part has no Alpaca data), or ticker reuse (CNX, ADT, ESV) |
| ANDV | 842 | 35% | Tesoro traded as TSO until 2017-08 |
| EMC, HOT, DO, LM, STJ | 322-405 | 53-63% | Removed 2016-2020; 2015 part missing |
| FOXA, FOX | 2,162, 2,083 | 56-58% | Yahoo's FOXA/FOX are the 2019 Fox Corp; the earlier 21st Century Fox series is not in the cache |
| DOW | 1,766 | 68% | Yahoo DOW is Dow Inc. from 2019; the old Dow Chemical (to 2017-09) has no feed |
| ARNC | 1,222 | 71% | Alcoa Inc. before 2016-11 traded as AA |

Others in the 98% to 70% range are removed names that lose only their 2015 months. Full per-name numbers are reproducible with `read_coverage` in `tradebot.data.pit_prices`.

## Residual survivorship bias (my honest estimate)

1. **Development window, 2016-2023.** 0.43% of member-days (about 2.1 of about 505 members on an average day) have no price, mostly 2016-2018: STI (SunTrust), FOX/FOXA (old 21st Century Fox), DOW (old Dow Chemical), ANDV (Tesoro), AABA (Yahoo), ARNC, TE, SNDK. Removal from the index is not random: bankrupt names fall in value, and acquired ones usually do not. A short-term reversal rule buys the worst 5-day losers, which over-picks distressed names, perhaps 2 to 3 times their share of days. So an upper bound on the share of daily top-10 slots that belonged to unpriced names in 2016-2018 is on the order of 1-3% per year (2019 onward below 1%, 2020-2023 essentially zero). The effect on the reported return is likely small but still one-directional: any losses that happened to those picks are missing.
2. **2015-06 to 2015-12** has 24% of member-days unpriced, almost all removed names. Results using 2015 are much more biased; the pre-registered development window (2016+) avoids this, and the first usable signals come in early January 2016.
3. **Ends of series.** A removed or delisted name's series stops at its last trade. Acquired names exit at the deal price (fine). Bankruptcies in the sample (for example CHK 2020, FRC 2023, SIVB 2023) end with the final listed price; the real owner kept trading on the over-the-counter market at far lower prices. A strategy holding such a name at delisting would be credited the last close. This can only overstate returns. Whether the simulator carries the last price or forces an exit at the end of a series is not something I checked; execution-lead should confirm before the run.
4. **Reused tickers.** FOXA, FOX and DOW hold the later company's prices. Because the membership flag is false (or the price is missing) when the old company was the member, no wrong-entity price is traded, but those names are partly lost.
5. **Membership accuracy.** About 3% of Wikipedia change events could not be matched mechanically (renames), and dates differ by up to 6 days. Effect on bias: negligible next to points 1 to 3.

Overall: this is much closer to a true point-in-time test than the current-constituents universe, but it is not survivorship-free. I would label the result "approximately point-in-time, residual bias small after 2018, mildly upward".

## Reproduce

```
uv run python -m tradebot.data.pit_prices --start 2015-06-01 --end 2023-12-31   # network; fills the cache
```
The helper only touches cache entries it made itself on the same day; pre-existing entries are never changed.

## Update 2026-10-23: after cleaning (ADR-0001 A33 M1/M2)

Alpaca-sourced series were cleaned in place (new cache generation; yfinance entries untouched): zero-volume flat (O=H=L=C) filler bars dropped, and everything after the first run of 5 or more filler bars cut (the ticker was reused). Of 136 Alpaca-sourced series, 17 changed (ADT, ATVI, CA, CHK, CTRA, DNB, DO, EMC, HCP, INFO, LLL, MNK, MON, PX, SBNY, SE, XL). The data quality report now counts filler bars (`QualityReport.filler_bars`) and `fetch_alpaca_daily` applies the same cleaning.

| Window | Member-days | With a price | Share |
|---|---|---|---|
| 2015-06 to 2023 | 1,090,258 | 1,067,594 | 97.92% |
| **2016-2023 (development window)** | 1,015,058 | 1,010,162 | **99.52%** |

By year: 2016 98.46%, 2017 99.06%, 2018 99.39%, 2019 99.72%, 2020 99.99%, 2021 99.94%, 2022 99.78%, 2023 99.79%. The slight drop from the earlier 99.57% is the filler bars that were counted as prices (the dead tail of delisted names); it is the more honest number. Members with no price at all in 2016-2023: 4.

Verdict-run universe: the 21 symbols in `sp500-pit-exclusions-2026-10.md` are excluded (15,689 member-days, 1.55% of 2016-2023 member-days). Coverage of the remaining member-days is 99.59%; counting the excluded days as unpriced it is 98.05%. A sensitivity run keeps them.

Delistings: with `EngineConfig.force_sell_missing=True` a held symbol that stops printing is sold on the first calendar bar without its bar at the last close less normal sell slippage and 50 bp (or written off at about 0 after a 50% fall over its last 5 bars), flagged `DELIST_NO_BAR`.
