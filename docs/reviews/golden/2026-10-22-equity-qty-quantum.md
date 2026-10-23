# Golden re-pin G1-G4 (2026-10-22): equity_qty_quantum
Cause: new `EngineConfig.equity_qty_quantum` field (owner decision 2026-10-22: stocks and ETFs trade in fractional shares; default 1e-6). It is now part of the canonical config payload hashed by `config_hash`, so the hash changes for every golden run.
Reason: new EngineConfig.equity_qty_quantum field; goldens pinned to whole-share mode (`equity_qty_quantum=Decimal("1")` in the golden harness); metrics unchanged.
Intended behavior change: none. Verified by key-by-key comparison of the old and regenerated JSON: only `config_hash` differs (all metrics, fills, first_fills, ledger sha256, final equity and benchmark blocks are identical).
Regenerated with `uv run pytest tests/golden --update-golden --golden-note=docs/reviews/golden/2026-10-22-equity-qty-quantum.md -k test_golden_matches`.

| id | old config_hash | new config_hash |
|---|---|---|
| G1 | 8bd5b358ec6d29ebc85b9a6461ec7f0960e9266d3bfe8e114a1a5582109fdd1f | 6e559e25d9e0895fcaefa9c6e56d32285d3847a1024dda972dffa99d83918e4a |
| G2 | dbc451f709864c0eae527dbe4efa5bc59a060135a1b865dd962c1e0cbcbd39af | ea2911766113dfb1f5451cd43495ba852a8bb34e2c2ab86ba50c232060556529 |
| G3 | 3bef3187af02cb1d3dd1167e6d6e0e24e8708d129b51db1a84b6376ba37490ea | 5e1aba8a94467fa3af3fcd351600a5a0960efe79eecaba294663fb2c947882eb |
| G4 | 68ae993e278882009520929fe061a44e2896925a312fb583e5525a45d07c6c4a | af91224ac171d1edb4ee9bf73f7b4853f76f71231e3ce9ce3f47ff768ede8936 |

golden-sha256: G1=9975e36d000a937fccbdfdb40547364ce5eb835a6b359c015de59d7f886b0ae7 G2=b2c6f0b61d7b545d53f82003b5e196294680ebffdd5828dbb8427d3ca3bc1dba G3=7b2ab49c7bb833ecf8accb20349821c37b1f94f771a3e012c7850c72c995d6c9 G4=4894882902fc0f90f2e69eb65384a9a5cb02c499e80400f5d6ecfd78279e2ff1
Approved-by: PM 2026-10-22
