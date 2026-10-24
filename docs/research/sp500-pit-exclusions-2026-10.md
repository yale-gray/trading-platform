# S&P 500 PIT: exclusion list for the verdict run (ADR-0001 A33 M1/M2)

Fixed 2026-10-23, before any run of `tA_st_reversal_pit`. Scope: the 136 Alpaca-sourced daily series (cache `source_rev = alpaca_sip`) **after** cleaning (filler bars dropped, series cut after the first run of 5 or more filler bars; 17 series changed). yfinance-sourced series are not scanned or changed.

**Rule.** A symbol is listed (and excluded from the verdict run) if it has any one-day close move above 40% that either (a) falls within 5 sessions of a start or end of that symbol's own S&P 500 membership interval (the only corporate-action calendar available offline), or (b) is not reversed: 5 bars later the close is still more than 20% away from the close before the move. A sensitivity run including these symbols is reported alongside the verdict.

**Plain meaning.** Many of these are real events (a bank failure, a bankruptcy, a meme squeeze), not data errors; the rule cannot tell them apart, so they are all excluded. That removes some genuinely bad outcomes (FRC, SIVB, BBBY, CHK), which pushes results up. The sensitivity run with them included is therefore the honest upper-bias check.

**21 symbols, 40 moves:** ABMD, ADT, ARNC, BBBY, CHK, CNX, CTRA, DO, ENDP, EVHC, FRC, FTR, MNK, MRO, MUR, RHT, SIVB, TGNA, TNL, UA, WRK

| Symbol | Date | Move | Reason |
|---|---|---|---|
| ABMD | 2022-11-01 | +49.9% | not reversed |
| ADT | 2016-02-16 | +47.5% | not reversed |
| ARNC | 2020-04-01 | -89.2% | index change 2020-04-07; not reversed |
| BBBY | 2021-01-27 | +43.4% | not reversed |
| BBBY | 2021-06-02 | +62.1% | not reversed |
| BBBY | 2022-08-19 | -40.5% | not reversed |
| BBBY | 2023-01-11 | +68.6% | not reversed |
| BBBY | 2023-02-06 | +92.1% | not reversed |
| BBBY | 2023-02-07 | -48.6% | not reversed |
| BBBY | 2023-04-26 | -43.8% | not reversed |
| CHK | 2020-03-13 | +96.0% | not reversed |
| CHK | 2020-04-24 | +45.3% | not reversed |
| CHK | 2020-04-30 | -46.0% | not reversed |
| CHK | 2020-06-05 | +76.5% | not reversed |
| CHK | 2020-06-08 | +182.0% | not reversed |
| CHK | 2020-06-09 | -66.0% | not reversed |
| CNX | 2017-11-29 | -89.6% | not reversed |
| CTRA | 2019-11-14 | -48.6% | not reversed |
| CTRA | 2020-04-08 | +41.4% | not reversed |
| DO | 2020-03-13 | +164.8% | not reversed |
| ENDP | 2019-09-06 | +47.7% | not reversed |
| ENDP | 2022-05-18 | -46.5% | not reversed |
| ENDP | 2022-06-28 | +85.3% | not reversed |
| EVHC | 2016-12-02 | +209.6% | index change 2016-12-05; not reversed |
| FRC | 2023-03-13 | -61.8% | not reversed |
| FRC | 2023-03-20 | -47.1% | not reversed |
| FRC | 2023-04-25 | -49.4% | not reversed |
| FRC | 2023-04-28 | -43.3% | index change 2023-05-05; not reversed |
| FTR | 2020-03-20 | +278.9% | not reversed |
| FTR | 2020-03-23 | -56.7% | not reversed |
| MNK | 2019-09-10 | +84.8% | not reversed |
| MNK | 2020-03-16 | -40.8% | not reversed |
| MRO | 2020-03-09 | -40.3% | not reversed |
| MUR | 2020-03-09 | -42.5% | not reversed |
| RHT | 2018-10-29 | +45.4% | not reversed |
| SIVB | 2023-03-09 | -60.4% | index change 2023-03-16; not reversed |
| TGNA | 2017-06-01 | -78.4% | index change 2017-06-05; not reversed |
| TNL | 2018-06-01 | -55.1% | index change 2018-06-01; not reversed |
| UA | 2016-04-08 | -49.4% | index change 2016-04-11; not reversed |
| WRK | 2016-05-16 | -84.5% | not reversed |

Reproduce: `big_moves` in `src/tradebot/data/pit_prices.py`, run over each Alpaca-sourced cached series.
