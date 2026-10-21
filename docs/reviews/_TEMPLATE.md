# Audit NNN: <spec / task / diff>

- **Auditor:** quality-lead
- **Date:** <YYYY-MM-DD>
- **Code:** <paths touched, with line counts>
- **Tests:** <test files, case counts, pass/fail, `ruff check` result, mypy result if run>
- **Against:** <SPEC-NNN sections and task blocks, ADR refs, CLAUDE.md hard rules>
- **Verdict: PASS | PASS-WITH-NOTES | FAIL**

<One to three sentences: what was audited, whether it is safe to merge, and the main reason for the verdict. FAIL means any blocker (wrong numbers, lookahead, missing costs, invariant violation, network in tests). The PM does not merge on FAIL.>

## Scope

<Commits, branch or paths in scope. State what was not in scope.>

## Evidence

<Commands run and their results, for example:>
- `uv run pytest <paths> -q`: <N passed, N failed>
- `uv run ruff check .`: <clean | N findings>
- <probes, golden recomputation, or other checks run by hand>

## Checklist (Y / N / n.a. with file:line)

| Item | Result | Evidence |
|---|---|---|
| Lookahead (indicators use <= t; truncation test present) | <Y / N / n.a.> | <file:line> |
| Survivorship | <Y / N / n.a.> | <file:line> |
| Costs both directions | <Y / N / n.a.> | <file:line> |
| Accounting invariants tested | <Y / N / n.a.> | <file:line> |
| Determinism / golden pinned | <Y / N / n.a.> | <file:line> |
| Decimal + explicit rounding | <Y / N / n.a.> | <file:line> |
| Benchmark reported | <Y / N / n.a.> | <file:line> |
| Offline tests | <Y / N / n.a.> | <file:line> |
| Restart/idempotency (if runner) | <Y / N / n.a.> | <file:line> |

## Findings (ranked)

Ranked by severity: blocker, major, minor, nit. Every finding cites file:line.

| # | Severity (blocker/major/minor/nit) | file:line | Issue | Required fix |
|---|---|---|---|---|
| F1 | <blocker / major / minor / nit> | `<path>:<line>` | <what is wrong and why it matters> | <the concrete change, or "none, info only"> |

## Mutation and test-strength review

Mutations applied mentally or with a real mutant run, and whether a test catches each one.

| Mutation | Caught by | Verdict |
|---|---|---|
| <for example: `ceil8` rounding CEILING changed to FLOOR> | `<test file:line>` | Caught / Survives / Equivalent (explain) |

Notes on surviving or equivalent mutants go here, with the test that should be added. If mutants were not run and the table is by reading only, say so.

## Test gaps

- **G1 (<severity>):** <missing test, what it should assert, and the file it belongs in>

## Re-audit needed? <yes/no, trigger>

<If yes, name the trigger: the finding IDs that must be fixed, the file changes that force a re-audit, or the golden or fixture change that does. If no, say why.>

## Re-audit (fix pass)

<Add this section only when a re-audit happens. Keep the original findings above and record the new verdict here.>

**Verdict: PASS | PASS-WITH-NOTES | FAIL.** <One-line summary of what changed and what is still open.>

| ID | Status | Evidence |
|---|---|---|
| F1 | <Fixed / Open / Partly fixed> | <file:line, test run result> |
