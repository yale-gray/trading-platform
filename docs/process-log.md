# Process Log: Agent Hierarchy Experiment

One row per dispatched task. We use this to judge whether the Opus PM → Sonnet leads → Sonnet/Haiku workers cycle is worth it, and to tune the routing rule.

| Date | Spec#Task | Agent | Model | Outcome | Rework | Escalated? | Notes |
|---|---|---|---|---|---|---|---|
| 2026-10-20 | Phase 0 scaffold | PM | Opus | done | 0 | no | Written directly by the PM, since agents can't run before their files exist |
| 2026-10-20 | SPEC-001 | data-lead | Sonnet | done | 0 | no | 371 lines, ~3 min. Strong research and citations; caught the SOL <5y history conflict with the gate |
| 2026-10-20 | SPEC-002 | strategy-lead | Sonnet | done | 0 | no | 453 lines, ~5 min. Proposed an ExecutionPort that clashed with 003 (expected when parallel); citations from memory, flagged as such |
| 2026-10-20 | SPEC-003 | execution-lead | Sonnet | done | 0 | no | 353 lines, ~4 min. Most rigorous: exact rounding table and 17 invariants. Its interface was adopted as canonical |
| 2026-10-20 | SPEC-004 | platform-lead | Sonnet | done | 0 | no | 447 lines, ~2 min. Good idempotency design; integer money columns overridden by ADR |
| 2026-10-20 | SPEC-005 | quality-lead | Sonnet | done | 0 | no | 413 lines, ~4 min. Hand-worked numbers and leak canaries; proposed committing real data (overridden: public repo) |
| 2026-10-20 | ADR-0001 | PM | Opus | done | 0 | no | Reconciled 12 seam conflicts; defined dispatch waves |
| 2026-10-20 | Spec amendments 001–005 | 5 leads | Sonnet | done | 0 | no | 2–9 min each, in parallel. 002 caught that TargetBook must be persisted (became ADR A1) |
| 2026-10-20 | 004 T1 deps/CI | junior-engineer | Haiku | done | 0 | no | Added ruff `docs` exclude (accepted) |
| 2026-10-20 | 005 T1 clock | junior-engineer | Haiku | done | 0 | no | 13 tests |
| 2026-10-20 | 005 T2 conftest/guards | junior-engineer | Haiku | done | 1 | no | Review follow-up: gethostbyname* + netguard module (Haiku) |
| 2026-10-20 | 005 T3 fixtures | engineer | Sonnet | done | 0 | no | Byte-identical, all anchors met; audit PASS-WITH-NOTES |
| 2026-10-20 | 005 T4 manifest test | test-engineer | Haiku | done | 0 | no | 84 checks |
| 2026-10-20 | 001 T1 data types | junior-engineer | Haiku | done | 1 | no | PM fix: one AssetClass (StrEnum), money re-exports it |
| 2026-10-20 | 001 T3 calendar | junior-engineer | Haiku | done | 1 | no | Review found calendar window depended on today's date; fixed with 2000–2035 bounds (Haiku) |
| 2026-10-20 | 003 T1 money | engineer | Sonnet | done | 1 | no | Audit F1 (numpy scalars rejected) was a real bug; fixed with F2–F5 (Sonnet), re-audited |
| 2026-10-20 | 003 T11a money tests | test-engineer | Haiku | done | 1 | no | 95 → 486 tests after audit gaps |
| 2026-10-20 | 002 T2 indicators | junior-engineer | Haiku | done | 0 | no | All 8 reference vectors on first try |
| 2026-10-20 | 002 T7a indicator tests | test-engineer | Haiku | done | 0 | no | 48 tests incl. truncation invariance |
| 2026-10-20 | Wave 1 review | reviewer | Sonnet | done | n/a | n/a | 9/9 approve, 0 blocking; non-blocking notes were worth acting on |
| 2026-10-20 | Wave 1 audits | quality-lead | Sonnet | done | n/a | n/a | Money + fixtures PASS-WITH-NOTES |
| 2026-10-20 | Wave 2: 26 tasks | 8 engineer / 18 junior+test | Sonnet / Haiku | done | 0 | no | Dispatched in dependency stages A–E; 0 Haiku escalations |
| 2026-10-20 | Wave 2 review | reviewer | Sonnet | done | n/a | n/a | 8/8 approve, 0 blocking; good design questions (pos in entry, cross-asset refs) |
| 2026-10-20 | Wave 2 audits | quality-lead ×2 | Sonnet | done | n/a | n/a | Execution: 5 majors (stale stop overlay, ambient context, fill without fee, float coercion, zero ledger tests). Data: 4 majors (non-atomic append, 1-bar overlap, uncalibrated tol / wedge, untested cutoff) |
| 2026-10-20 | Wave 2 fixes | engineer ×4, junior ×1 | Sonnet / Haiku | done | 1 | no | Split by file so 3 fixers ran in parallel; PM relayed API changes between two data fixers mid-flight |
| 2026-10-21 | SPEC-006 dev console | platform-lead | Sonnet | done | 0 | no | Spec in ~3 min; 14 tasks with a clean dependency graph |
| 2026-10-21 | 006 T1–T14 | engineer ×4 / junior+test ×12 | Sonnet / Haiku | done | 1 | no | 0 Haiku escalations; workers caught an interface mismatch (period vs n) and a stale roadmap |
| 2026-10-21 | 006 review | reviewer | Sonnet | done | 1 | n/a | REQUEST-CHANGES: chart RSI/ATR/EMA off by up to ~5 points (short warmup) and a wall-clock read; fixed and re-reviewed |

## Retrospective notes
### Lead research round (2026-10-20)
- **Worked:** five specs in about 5 minutes of wall time. Each lead stayed in scope and listed its interface needs and assumptions, which made the reconciliation mechanical.
- **Didn't work:** running the leads in parallel guaranteed interface clashes (002 vs 003 execution API, fixtures, money storage). Next time, give leads a shared draft interface sheet first, or run strategy and execution leads in sequence.
- **Lesson:** "Assumptions about other areas" sections are the most valuable part of a parallel spec round. Keep requiring them.

### Wave 1 (2026-10-20)
- **Haiku held up well** on well-specified tasks. 8 Haiku tasks, 0 escalations, and the indicator library matched every hand-computed vector first try. Precise specs with reference numbers are what make Haiku viable.
- **Parallel workers in one checkout worked** because file lists were disjoint and workers were told to lint/test only their own files. No worktrees needed.
- **The review layers paid off.** The reviewer found a date-dependent calendar, and quality-lead found numpy values being rejected by the money module. Neither was caught by the workers' own tests, because each test only checked what its spec listed.
- **Cross-task issues still land on the PM**: a duplicate AssetClass and a shared library warning. Next time, specs should name a single owner for shared types up front.

### Wave 2 (2026-10-20)
- **Staged dispatch worked well**: 26 tasks with a 6-deep dependency chain in data finished cleanly by unlocking each task the moment its inputs landed.
- **Audits found what tests didn't, again.** Every major came from quality-lead, not the reviewer or workers' own tests. The worst ones were the stale stop level on re-entry, the fee-less fill, and the one-bar "restatement" that would have permanently corrupted prices. Mutation probing ("does any test fail if I flip this sign?") was the single most useful audit technique; worth making it standard in the audit template.
- **Spec gaps cluster at seams**: the 6-letter fixture names vs the 5-letter ticker rule, the basis label for crypto, where STRATEGIES lives. Workers flag these well when asked to report decisions.
- **Haiku continues to hold up** on precise tasks (0 escalations in 18 Haiku tasks). It did once state a wrong fact (claimed yfinance was not installed) — PM verification of surprising claims matters.

### Mini-wave 2.5: dev console (2026-10-21)
- **Reviewer earned its keep on a "non-critical" UI**: the indicator charts would have shown RSI values up to ~5 points different from what the strategy computes — the kind of silent mismatch that erodes trust in a dashboard. Measured, not eyeballed.
- **Looking at the running app early helped**: the PM preview caught the late-starting SMA line before review did.
- **Seam bugs again** (period vs n between two helper modules written in parallel). Exact signatures in the spec prevent most, but cross-module data shapes need the same precision.

<!-- After each phase: what worked, what didn't, routing-rule changes -->
