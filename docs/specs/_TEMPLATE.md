# SPEC-NNN: <title>

- **Lead:** <agent name>
- **Status:** draft | approved | in-progress | done
- **Roadmap item:** <link/phase>
- **Related ADRs/specs:** <links>

## Goal
<1–3 sentences: what this delivers and why.>

## Non-goals
- <explicitly out of scope>

## Design
<Approach, data flow, key decisions with reasoning. Cite sources for external facts.>

### Interfaces
```python
# Exact signatures, pydantic models and types that workers must implement
```

### Invariants / rules
- <e.g., cash never negative; signal at t fills at t+1>

## Acceptance criteria
1. <testable statement>
2. ...

## Tasks
Tag each task `[sonnet]` (engineer) or `[haiku]` (junior-engineer / test-engineer) using the routing rule in CLAUDE.md.

### T1 [haiku|sonnet] <title>
- **Files:** `path/a.py`, `path/b.py`
- **Depends on:** none | T#
- **Do:** <precise instructions>
- **Done when:** <criteria refs, e.g. AC 1, 2>

### T2 [haiku] Tests for T1
- **Files:** `tests/...`
- **Depends on:** T1
- **Do:** cover AC 1–2 with fixture data
- **Done when:** tests pass offline

## Open questions
- <for the PM / an ADR>

## Risks
- <what could go wrong>
