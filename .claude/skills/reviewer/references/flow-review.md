# Flow review procedure

A flow review verifies the system as a running system, not as text. It is invalid unless the application was actually executed. Its purpose is to find what code review structurally cannot: integration breaks, wiring mistakes, state bugs, and UX failure paths.

## Step 1 — Get the app running

Use the repo's own recipe: README, `package.json` scripts, Makefile, docker-compose, or an existing `/run` recipe skill. If the app cannot be started at all, that is itself a BLOCKER finding — record it and stop the flow review there.

## Step 2 — Pick the driving method (in this order)

**A. Playwright (preferred, repeatable)**
1. If the repo has e2e tests: run them first (`npx playwright test`). Any failure is a finding.
2. Write adversarial specs for the flows this change introduces or touches. Keep them in `tests/flow-review/` so they become permanent regression protection. Cover, per flow:
   - the happy path,
   - every relevant edge case from the plan,
   - break attempts from the attack playbook: double-click submits, back/refresh mid-flow, direct deep-links past guards, hostile input into every field, slow-network behavior (`page.route` with delays), and error responses (mock the backend failing).
3. Assertions must check real outcomes (data persisted, UI state, redirects, error surfaced to the user) — not merely "page loaded".

**B. Claude in Chrome (when the MCP browser connection is available)**
Drive the real browser through the same scenario list interactively. Useful when flows involve third-party pages, real auth, or visual states Playwright setup would be disproportionate for. Record each scenario and its observed outcome precisely.

**C. Direct driving (APIs, CLIs, services with no UI)**
Exercise the running service with curl/httpie or its CLI: happy path, each edge case, malformed payloads, wrong methods, missing auth, concurrent requests (two curls in parallel against the same resource). Same logging discipline.

## Step 3 — Log every attempt

Every scenario goes into the flow review file as:

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| Double-click "Pay" | one charge, second click ignored | two charge records created | BREAK |

Any BREAK is a finding (BLOCKER if it corrupts data, security, or money; MAJOR otherwise). Any scenario you could not execute is recorded as NOT RUN with the reason — unexecuted scenarios covering planned edge cases force RED.

## Step 4 — Before-PR rule

The flow review that gates a PR must have run against the final state being pushed. If the developer changed anything after the last flow review — even a "trivial" fix — the flow review is stale and must be re-run. Trivial fixes that broke the build are a classic; the rule has no exceptions.
