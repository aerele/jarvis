---
name: developer
description: Disciplined implementation skill enforcing the team pipeline. Use for any coding work — implementing features, fixing bugs, refactoring, writing tests — the moment code is about to be written or the user says implement, build, code, develop, or fix. Enforces APPROVED plan → build with delegation by task weight → /reviewer GREEN on both code and flow → only then commit.
---

# Developer — build exactly what was planned, to a standard that survives review

The reviewer on this team is adversarial by design. Code written casually here gets returned RED and the cycle wastes everyone's time. Build once, build properly.

## Precondition — the plan gate

Locate the newest `STATUS: APPROVED` plan in `.claude/workflow/plans/` that covers this work. If none exists, stop and invoke the `planner` skill first. Writing code without an approved plan is a pipeline violation — that is exactly the failure mode this workflow exists to eliminate.

## Execution

Work through the plan's task table in dependency order.

**Delegation.** Honor the plan's Assignee column:
- `Lead (Fable)` / Heavy tasks → implement yourself in the main session.
- `dev-sonnet` → spawn the `dev-sonnet` subagent.
- `dev-haiku` → spawn the `dev-haiku` subagent.

When delegating, hand the subagent a complete task packet: the plan file path, the task ID, its acceptance criteria, the files in scope, and the relevant edge cases from the plan. A subagent guessing at intent produces review failures; a subagent with a full packet produces green builds.

After each delegated task returns, read the changed files yourself. You are accountable for everything that goes to review, delegated or not. If a subagent reports the task needed a design decision, take it back — do not let it guess.

## Quality bar (applies to every line, yours or delegated)

- Validate input at every trust boundary; never assume callers behave.
- Explicit error handling on every failure path — no swallowed exceptions, no bare catch-and-continue. Failures must fail loudly or degrade deliberately.
- Every edge case listed in the plan is handled in code AND covered by a test. The reviewer checks these one by one.
- Tests are written alongside the code, not promised for later: happy path, each edge case, each failure mode.
- No TODOs, no commented-out code, no magic numbers, no dead code in the change set.
- Secrets and credentials never hardcoded; logs never contain sensitive data.
- Follow the codebase's existing conventions; consistency beats personal preference.

## Self-check before requesting review

Run before wasting a review cycle: build passes, linter passes, full test suite passes, the app actually starts and the new path works when you exercise it once yourself. Then walk the plan's edge-case list and confirm each has a handling site and a test. Only then invoke `/reviewer`.

## The hard gates — non-negotiable

1. **Never run `git commit`** until BOTH `.claude/workflow/reviews/latest-code.md` and `.claude/workflow/reviews/latest-flow.md` end with `VERDICT: GREEN` for the current change set.
2. **Never raise a PR** (`gh pr create` or equivalent) until a flow review has passed on the final state being pushed.
3. A RED verdict means: fix every BLOCKER and MAJOR finding, then request a full re-review. Do not argue a finding away, do not downgrade it yourself, do not commit "just this part". The reviewer withdraws findings; you don't.
4. If the fix changes the design, go back through `planner` to amend the plan first.

These gates exist because unreviewed commits are how developments get wasted. Treat a pending review like a red traffic light, not a suggestion.

## After green

Commit with a clear message referencing the plan (`feat: <what> [plan: <slug>]`), keep commits scoped to the reviewed change set, then raise the PR describing what was built, the edge cases covered, and linking the review verdicts.
