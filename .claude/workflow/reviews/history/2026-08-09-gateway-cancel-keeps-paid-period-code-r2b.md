
---

# Code review — 2026-08-09-gateway-cancel-keeps-paid-period — round 2, second independent pass (B)
Reviewer: Opus (strict-reviewer), separate context
Date: 2026-08-10

**Why there are two passes.** Two reviewer instances were dispatched against this change set
concurrently and neither saw the other's work until write time. Pass A (above) was already on disk;
this pass is appended rather than overwriting it, because the findings do not fully overlap and
throwing away either set would lose real defects. Both passes independently return RED. Pass A's
findings stand as written — I re-derived and confirm A1 (`_sub_for_mandate` reads only the flat
`data["subscription_id"]` at `cashfree_webhook.py:259`, while its sibling
`_activate_authorized_mandate` reads `details.get("subscription_id") or data.get("subscription_id")`
— the tolerant form, three functions away, with a docstring calling the nested shape the verified
real one) and A4 (`still_entitled` has no remaining production caller anywhere in the app; the only
`grep` hits are an unrelated test name and a doc paragraph describing the deleted guard).

**Scope note.** The invoker described this round as "fixes for round-1 findings 5 and 6 only, in
`gateway_cancel_defect_report.py`". That is not what is on disk: mtimes show `webhook.py` (11:03),
`cashfree_webhook.py` / `cancellation.py` / `api/account.py` (11:04), `PlanBillingPane.vue` and the
plan itself (11:06) were all edited after round 1's verdict (11:00), and `test_cashfree_subscription.py`
was touched last (11:31). The whole change set is in scope, and round 1's flow review is stale for all
of it. The branch carries no commits — `git log origin/develop..HEAD` is empty; everything is working tree.

Evidence executed in this pass, on `test_site` only (never `jarvis_admin_v2.local`, no
pool/provision/fleet module), each module run cold:
`test_gateway_cancel_defect_report` 14 OK · `test_autopay_cancel` 31 OK ·
`test_terminal_cancel_autorenew` 9 OK · `test_trial_autopay` 44 OK · `test_account_endpoints` 61 OK ·
`test_authority_repair_hardening` 6 OK · `test_cashfree_subscription` 39 OK · `test_webhook` 16 OK ·
`test_expiry` 10 OK. `ruff 0.14.10 check` and `ruff format --check` clean. `npx vitest run` 1021 OK / 64 files.
Live driving is logged in the flow file (scenarios B1–B20).

*Test-evidence caveat, not a finding against this diff:* on this bench any two admin modules run
back-to-back produce `QueryDeadlockError` storms in the second (`test_account_endpoints` lost
11/28/33 tests across three attempts). Reproduced with modules this diff does not touch
(`test_webhook` → `test_account_endpoints`, `test_expiry` → `test_account_endpoints`), so it is
environmental: live bench workers share Redis with `test_site` and execute `enqueue_after_commit`
jobs against it mid-run (`logs/worker.log`, `intent_ledger.release_superseded_object`,
site=`test_site`, a pattern present since 2026-08-08). Every module is green run cold. Pass A saw the
same class of noise in `test_signup`.

## Findings (pass B)

| # | Severity | Location | What breaks | Required fix |
|---|----------|----------|-------------|--------------|
| B1 | **BLOCKER** | `billing/gateway_cancel_defect_report.py:244` and `:246`; same unguarded shape assumption at `:248` (refund) and `:257` (dispute) | **One malformed log row kills the entire report — no partial output, no victims, just a traceback.** Reproduced by running `report_defect_victims()` on `test_site`: `TypeError: cannot use 'dict' as a dict key (unhashable type: 'dict')` at `:246`, from a verified row whose body was `{"event":"subscription.cancelled","payload":{"subscription":{"entity":{"id":{"a":1}}}}}`; a second row, `{"payload":{"subscription":[]}}`, raises `AttributeError: 'list' object has no attribute 'get'` at `:244`. `_print_report` is never reached. Provenance matters and cuts the wrong way for this change: those rows were not planted by me and not written by a unit test — they are the residue of *pass A's own live break-attempts*, i.e. validly-signed malformed bodies POSTed to the real endpoint, which `razorpay_webhook` **persists as `verified=1`** even though dispatch then 500s (pass A's finding 5). So the two defects compound: one shape-variant delivery from Razorpay both 500s the webhook and permanently poisons the log, after which the operator's only tool for finding the customers this whole plan exists to protect (Q2, T4, edge 16) refuses to produce a single line. It also breaks two written promises — the module's own comment at `:241-242` ("unparseable payload can't corroborate anything; leaves any candidate it would have matched as UNCERTAIN rather than crashing" — `_parse_payload` guards `json.loads` only, never the shape) and T4's acceptance criterion ("Prints customer, sub, plan, `current_period_end`, container … Safe to run on the live control plane"). `:257` has the same exposure through `razorpay_dispute_event`, whose `int(amount)` raises on a non-numeric amount. No test covers any of it: `_parse_payload`'s `continue` is never exercised by the 14 tests. | Make a bad row skippable, not fatal: wrap the per-row extraction in `try/except (AttributeError, TypeError, ValueError)` → log and `continue`, and dig through the payload with a helper that `isinstance(..., dict)`-checks each hop and coerces `sub_id` with `str()`. Apply to the cancel, refund and dispute branches alike. Add tests for a non-dict `payload` / `subscription` / `entity`, a non-string `id`, a non-numeric refund `amount`, and a dispute row that raises — each asserting the report still prints and still classifies every other candidate. |
| B2 | **MAJOR** | `api/account.py` `_may_cancel` — `return bool(sub.autorenew) or not any_mandate_id(sub)` — plus `cancel_plan_at_period_end`'s `if not _may_cancel(sub): return _ok(_cancellation_payload(sub))` | **Every customer who has ever armed a mandate permanently loses the ability to cancel, and the endpoint answers a silent success when they try.** `has_live_mandate`'s own docstring states the mandate id is deliberately never cleared on release ("it stays our handle on a mandate we only BELIEVE is dead"), and nothing outside `cancel_scheduled_downgrade` ever clears it — so `any_mandate_id(sub)` stays true forever, `_may_cancel` stays false forever once `autorenew` is 0, and it survives any number of manual renewals onto fresh, fully-paid periods. Proven live over HTTP on two Active, entitled, `autorenew=0` subscriptions differing only by a dead id string: the never-autopay one returns `can_cancel: true`, stamps `cancelled_at`, and resumes cleanly (flow B14); the ex-autopay one returns `can_cancel: false`, the pane renders no cancel control at all, and `POST cancel_plan_at_period_end` returns `{"ok":true,…}` while changing nothing (flow B12). That silent OK is precisely the anti-pattern the endpoint's own new comment says the guard reordering exists to prevent — "reads as 'already settled' and gets a silent OK instead of being told its plan has ended". Not covered by any of the plan's 24 edge cases (edge 18 assumes "no mandate", which a released-mandate row is not) and by no test. | Choose and encode one: (a) key the term on `has_live_mandate(sub)` rather than `any_mandate_id(sub)`, so a released mandate hands the customer back to the one-shot branch; or (b) keep the narrowing but make the endpoint **refuse** (`NotCancellable` / `NothingToCancel`) instead of returning a silent OK, and take the narrowing through `planner` as a new edge case. Either way, add a test pinning the behaviour for an Active, entitled, `autorenew=0` sub that still carries a mandate id. |
| B3 | **MAJOR** | Flow review, bench-UI half (both passes) | Plan flow scenarios **3** ("Cancel auto-renewal" in Settings → Plan & billing: confirm copy, no ending-plan banner, no Resume, "Set up auto-renewal" offered instead) and **4** (one-shot "Cancel subscription": ending-plan banner, `Ends D MMM` pill, Resume) were **NOT RUN in a browser by either pass**. `PlanBillingPane.vue` was edited after round 1's flow review, so no browser evidence exists for the shipped pane; pass A states plainly that it did not drive one either. Three routes were attempted here and all are blocked on this host: no Chrome extension is connected (`list_connected_browsers` → `[]`); the repo's Playwright harness cannot start — the vite dev server answers **HTTP 500** (its frappe-ui plugin shells out to `bench list-app-sites`, a command this frappe build does not have) and binds IPv6-only, so chromium cannot reach the configured `localhost:8080`; and the bench-served bundle at `jarvis.local:8002/jarvis` needs Administrator credentials I do not have and will not guess. The pre-existing `tests/e2e/mobile.spec.js` fails identically, so this is an environment break, **not** a regression from this diff — but per the flow-review contract, unexecuted scenarios covering planned flow steps force RED regardless of cause. Pass A's payload-driven pane harness and the 12 component specs raise confidence; they do not open Settings in a browser. | Run scenarios 3 and 4 against the built bundle in a real browser (Chrome extension, or repair the e2e harness: bind the dev server on IPv4 or pin `baseURL` to `http://[::1]:8080`, and stub the frappe-ui plugin's site lookup). Note the bench bundle had to be rebuilt during this review for the pane change to be present at all (`npm run build`) — whoever re-runs this must build first, or they will test the old pane. |
| B4 | MINOR | `billing/gateway_cancel_defect_report.py:108-109` | `window_days` is validated on the lower bound only. Probed live against the control plane: `10**9` → uncaught `OverflowError: days=-1000000000; must have magnitude <= 999999999`; `"365"` → `TypeError: '<=' not supported between instances of 'str' and 'int'`, and a string is exactly what `bench execute --kwargs "{'window_days': '365'}"` yields if the operator quotes the value, which the module's own invocation examples make easy to do; `True` is silently accepted as a one-day window. Read-only and loud, so nothing is corrupted — but a guard that advertises input validation should not be one-sided. | `window_days = int(window_days)` inside the check, and bound the upper end, reusing the existing `ValueError` message. |
| B5 | MINOR | `tests/billing/test_gateway_cancel_defect_report.py:466` | Comment reads "Outside the default 90-day window". The module default is **365** (`_DEFAULT_WINDOW_DAYS`); 90 is what this call passes explicitly. The next reader will believe the default is 90. | "outside the 90-day window this call asks for". |
| B6 | MINOR | `billing/gateway_cancel_defect_report.py:182-207` vs `fleet/_tenant_lookup.py::find_assigned_tenant` | `_bulk_find_assigned_tenants` re-implements the shared helper's rule in order to batch it, and it is correct today — I diffed the filter set and ordering (`assignment_state=Assigned`, `status in`, `is_move_target=0`, `quarantined=0`, `assigned_at desc`, top-1) and they match, as pass A also found. What is missing is anything pinning the equivalence: no test covers two live tenants for one customer (newest `assigned_at` must win), a `quarantined=1` row, or an `is_move_target=1` row. `find_assigned_tenant`'s docstring records that both filters were added to fix real incidents; the next change to it will silently drift this copy, and the container name this function returns is what an operator acts on when restoring a victim. | Add a test with two Assigned tenants for one customer (assert newest wins) plus one `quarantined=1` and one `is_move_target=1` row (assert both excluded). |

### Round-1 findings — pass B disposition

Agrees with pass A on all ten, with two qualifications:
- **r1-2 (Cashfree parity)** — I independently confirm pass A's A1: the dunning added is right and I
  verified it live on the flat payload (flow B17: `CANCELLED` on a lapsed sub → Past Due; `ON_HOLD` on
  the identical fixture → still Active), but `_sub_for_mandate`'s flat-only read means the branch is
  unreachable on the nested shape the module documents as real. **Not closed.**
- **r1-6 (unbounded log read + N+1)** — closed for the two things it named, but the module it hardened
  still dies on a malformed row (B1) and the batching is unpinned (B6).

### Edge cases where pass B's evidence differs from pass A

| Plan edge case | Pass B result |
|---|---|
| 10. Malformed payload / no crash | **PARTIAL.** The webhook's `_mark_past_due` path is fine, and pass A found the webhook itself 500s on a malformed body (A5). The **report** path additionally dies (B1). Neither is covered by a test. |
| 18. Bench "Cancel subscription" (one-shot) unchanged | **NOT unchanged** for a one-shot subscription carrying a dead mandate id — B2. It is unchanged, and verified live, for a subscription with no id at all. |
| 17/18 browser rendering | **NOT RUN** (B3). |

VERDICT: RED
