# Process Log: Agent Hierarchy Experiment

One row per dispatched task. We use this to judge whether the Opus PM → Sonnet leads → Sonnet/Haiku workers cycle is worth it, and to tune the routing rule.

| Date | Spec#Task | Agent | Model | Outcome | Rework | Escalated? | Notes |
|---|---|---|---|---|---|---|---|
| 2026-10-20 | Phase 0 scaffold | PM | Opus | done | 0 | no | Written directly by the PM, since agents can't run before their files exist |

## Retrospective notes
<!-- After each phase: what worked, what didn't, routing-rule changes -->
