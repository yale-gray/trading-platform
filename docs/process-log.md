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

## Retrospective notes
### Lead research round (2026-10-20)
- **Worked:** five specs in about 5 minutes of wall time. Each lead stayed in scope and listed its interface needs and assumptions, which made the reconciliation mechanical.
- **Didn't work:** running the leads in parallel guaranteed interface clashes (002 vs 003 execution API, fixtures, money storage). Next time, give leads a shared draft interface sheet first, or run strategy and execution leads in sequence.
- **Lesson:** "Assumptions about other areas" sections are the most valuable part of a parallel spec round. Keep requiring them.

<!-- After each phase: what worked, what didn't, routing-rule changes -->
