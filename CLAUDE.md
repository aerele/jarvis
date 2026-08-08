## Development pipeline (non-negotiable)

This repository runs a strict team workflow. The order is fixed:

PLAN → BUILD → CODE REVIEW → FLOW REVIEW → GREEN → COMMIT → PR

1. **No code without an approved plan.** Every task starts with the `planner`
   skill (team leader, Fable). Plans live in `.claude/workflow/plans/` and must
   read `STATUS: APPROVED` before implementation begins.
2. **Implementation follows the `developer` skill.** Tasks are delegated by the
   plan's weight column: Heavy → lead, Medium → dev-sonnet, Light → dev-haiku.
3. **No `git commit` until the `/reviewer` skill (Opus, adversarial) has written
   `VERDICT: GREEN`** in BOTH `.claude/workflow/reviews/latest-code.md` and
   `.claude/workflow/reviews/latest-flow.md` for the current change set.
4. **No PR until a flow review (Playwright or Claude in Chrome, actually
   executed against the running app) passed on the final state being pushed.**
   Any change after the last flow review makes it stale — re-run it.
5. **RED verdict → fix every BLOCKER and MAJOR → full re-review.** Findings are
   never argued away or self-downgraded; only the reviewer withdraws a finding.
6. Scope changes mid-build go back through `planner` for re-approval first.

The goal: strict reviews, high-grade code quality, an architecture that survives
adversarial testing. When in doubt, the stricter interpretation wins.
