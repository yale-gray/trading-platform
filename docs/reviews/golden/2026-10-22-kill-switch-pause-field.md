# Golden re-pin G1-G4 (2026-10-22)
Cause: ADR-0001 A18 added `RiskConfig.kill_switch_pause_bars` (default 20). The field is part of the
canonical config, so `config_hash` changes for every golden run.
Intended behavior change: none. Metrics, fills, ledger sha256 and benchmark blocks are unchanged
(verified by the quality-lead by key-by-key comparison of the old and regenerated JSON; only `config_hash` differs).
Regenerated with `uv run pytest tests/golden --update-golden --golden-note=docs/reviews/golden/2026-10-22-kill-switch-pause-field.md -k test_golden_matches`.

| id | old config_hash | new config_hash |
|---|---|---|
| G1 | a67141f77cc12dda... | 8bd5b358ec6d29eb... |
| G2 | (see git history) | dbc451f709864c0e... |
| G3 | (see git history) | 3bef3187af02cb1d... |
| G4 | (see git history) | 68ae993e278882 |

Unchanged: ledger_sha256 G1=0813ba16... G2=b640bb21... G3=e10e46b9... G4=2151a81d... (identical old vs new), all metrics, fills_sha256, benchmark blocks.

golden-sha256: G1=b16d82e36a426780d70f2ea1e8f9dc5802a7e2714aa3b186be0207484d3f4b8e G2=fb1b87988bd840ca393ec0e8c3264e80459767338dfe7e8234edd306513046ea G3=8819f531dd2ab326b59a2e2864a5dfaec7ea73a4b7868ae75333028a43ac46c9 G4=c076cfd2eca16c93eb595fb1c4bb9d5ca79db8b4010fcb40d7d939458c408161
Approved-by: PM 2026-10-22
