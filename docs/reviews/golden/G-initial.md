# Golden initial set G1-G4 (2026-10-21)
Cause: SPEC-005 T17 first generation (Wave 4), run with
`uv run pytest tests/golden --update-golden --golden-note=docs/reviews/golden/G-initial.md -k test_golden_matches`.
Intended behavior change: none. These are the first pinned values for the four golden backtests on the
committed synthetic fixtures (`tests/fixtures/market`, 2018-01-02..2024-12-31).

Deviation from SPEC-005 section 5: runs start 2019-01-02, not 2018-01-02. SPEC-002 section 6 requires
`warmup_bars` of history before `start` (now enforced by `run_backtest`), and the fixtures begin 2018-01-02,
so the first year serves as indicator warm-up. End 2024-12-31, cash 100000, costs per `default_config()`.
G1-G3 run one sleeve at weight 1 with the asset and crypto caps and the kill switch relaxed (the
benchmark policy); G4 is the full 50/20/20/10 fund with default risk limits.

| id | final equity | fills | total return | Sharpe | benchmark total return |
|---|---|---|---|---|---|
| G1 | 98906.51 | 305 | -0.0109 | -0.001 | SPY 0.4292 |
| G2 | 97817.24 | 204 | -0.0218 | 0.030 | SPY 0.4292 |
| G3 | 200680.99 | 1305 | 1.0068 | 0.541 | BTC/USD 2.2676 |
| G4 | 122919.05 | 1893 | 0.2292 | 0.397 | BTC/USD 2.2676, SPY 0.4292, blend 0.9836 |

Independent check: `tests/integration/test_backtest_e2e.py` recomputes, without the portfolio engine, the
first G1 fills (price = bar open x (1 +/- 5 bps), amount = qty x price, signal bar before fill bar) and the SPY
buy-and-hold return from the raw bars. Status: first-run values, awaiting the quality-lead hand audit of at
least three fills and the benchmark return (SPEC-005 T17); the PM pre-approved generating the initial set.

golden-sha256: G1=ca3f6b437f432ce7307838dbe5027a395557f13067d36a3de8196f4f09e096bc G2=d49d75301c427d79d3ddb825b73c489c1956d9d1aa16e6fc4fc19d6097c736dd G3=1d3daaef77663af2ac51f7dc77b2d6bb873498d2466ae37e87747b3518f53b52 G4=2da71011956a02aaf4a876b180cf9343084af3d7384dae31f3c90048b7eb125a
Approved-by: PM 2026-10-21
