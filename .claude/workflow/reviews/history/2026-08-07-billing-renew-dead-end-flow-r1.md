# Flow review — 2026-08-07-billing-renew-dead-end — round 1
Reviewer: Opus (strict-reviewer)
Date: 2026-08-07
Scope: executed against the running bench (`webserver_port 8002`, dev server live).
- Tenant SPA: `http://jarvis.local:8002/jarvis/billing`, driven in Chrome (Claude in Chrome).
- Server seam: `jarvis.account.get_billing_payment_state` / `.check_billing_payment_status` driven
  directly on `jarvis.local` for the auth boundary and error shapes.
- Control plane: `jarvis_admin_v2.local`, subscription `v4p648rp8g` (Cancelled, autorenew 1,
  period_end 2026-09-07), its one live `Jarvis Payment Intent` `v4pbs2dce3`
  (`purpose=SIGNUP`, `checkout_state=OPENED`, `money_state=AUTHORIZED`, `apply_state=APPLIED`).

Build freshness: the deployed chunk `sites/assets/jarvis/frontend/assets/BillingPage-BuStXTIl.js.map`
carries `sourcesContent` byte-identical to the working-tree `BillingPage.vue`, so the flow ran against
the final state. Scenarios marked *(stub)* replaced only the admin HTTP response for
`jarvis.onboarding.renew` / `check_billing_payment_status` / `get_billing_payment_state`; the SPA under
test was the real build in every case.

Data integrity across the whole session: admin intent count for `v4p648rp8g` was 1 before and 1 after;
site-wide `Jarvis Payment Intent` count 10 before and 10 after; subscription still
`{status: Cancelled, autorenew: 1}`. No gateway object created, no charge, nothing mutated.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| Load `/jarvis/billing` on the repro row | current plan + honest status | "Starter ₹100/mo · Cancelled · Renews 2026-09-07 · 31 days left · **Auto-renew on**" | PASS (the autorenew lie is the T3 patch's job and the patch has not been applied to this row) |
| Renew → confirm "Pay ₹100" (the plan's flow step 1) | pending-confirmation copy + a Check action, instead of today's silence | "We have not confirmed this payment." + "Check the status before doing anything else. If money already moved, please do not pay again, as that would authorize a second one." + **[Check payment status]**. INITIATE correctly absent. | PASS |
| **Check payment status** on the repro row (flow step 2) | healer runs, no new intent, customer converges | red `There's no payment in progress for this subscription.`; notice cleared; **no next action left**. Server seam reproduces it: both endpoints raise `ValidationError` with that message as Administrator. Cause: the blocking intent is `purpose=SIGNUP`, and both endpoints refuse `purpose not in BILLING_FLOWS` (`billing.py:342`, `:424`). | **BREAK — BLOCKER (finding 1)** |
| Double-click "Pay ₹100" (edge 1) | at most one renew, one gateway object | exactly one `POST /api/method/jarvis.onboarding.renew`; intent count unchanged | PASS |
| Double-click "Check payment status" (edge 1) | exactly one healer call | exactly one `POST /api/method/jarvis.account.check_billing_payment_status` (HTTP 417); `get_billing_payment_state` correctly not reached after the throw | PASS |
| Assert no `Jarvis Payment Intent` row created (flow step 2) | count unchanged | 1 for the sub / 10 site-wide, before and after the entire session | PASS |
| Member session — `jarvis-chat` (Jarvis User, no Jarvis Admin) calls both endpoints directly (edge 4, flow step 5) | refused before any admin round trip | `frappe.PermissionError` on both; `admin_client` mocks assert zero calls | PASS |
| Unknown future code from admin *(stub: `{code:"SOME_FUTURE_CODE_XYZ_2027"}`)* (edge 3) | generic honest headline + ≥1 action | "We could not determine the payment status." + "Nothing has been assumed about your payment. Check the status to see where it stands." + **[Check payment status]** | PASS |
| Non-string code *(stub: `{code: 42}`)* (edge 11) | no throw, renders something | generic row rendered; zero `error` / `unhandledrejection` events captured | PASS |
| Missing code *(stub: `{message: null}`)* (edge 11) | renders the generic branch | dialog closed, **nothing rendered** — no notice, no error, no console error. Identical to the pre-fix symptom for this payload shape. | **BREAK — MAJOR (finding 4)** |
| Live token, origin not attested *(stub: token + `pay_origin_attested:false`)* (edge 5) | fail closed, no navigation | "Secure payment isn't set up on this site yet. Nothing has been charged…"; `location` unchanged | PASS |
| Live token, origin attested *(stub: token + `pay_origin:"http://jarvis_admin_v2.local:8002"`, attested)* fired from **Check payment status** | Check is read-only; a resume should be an explicit, labelled, priced action | browser top-level-navigated off the workspace to `jarvis_admin_v2.local:8002/jarvis-checkout#t=…` — no amount shown, no confirm step, from a button labelled "Check payment status" | **BREAK — MAJOR (finding 2)** |
| Admin transport failure during Check *(stub: fetch rejects)* (edge 2) | honest coded message, row untouched | notice + Check button correctly survive; error text is the literal **"Failed to fetch"**. `BENCH_ADMIN_UNREACHABLE` copy exists for exactly this and is unused. | **BREAK — MAJOR (finding 3)** |
| Intent resolves between Check and re-read *(stub: state → `PAYMENT_ALREADY_ACTIVE`)* (edge 6) | land on Active, no second payment offered | "Payment is confirmed. Nothing more is owed. We are continuing your setup." rendered directly above a **Cancelled** badge and a live **Renew** button. No second payment offered (correct), but the page states two contradictory things. Reachable for real: `billing.py:348` returns this code whenever `apply_state == APPLIED`. | **BREAK — MAJOR (finding 5)** |
| Code declaring INITIATE *(stub: `{code:"PAYMENT_DECLINED"}`)* → click "Start a new payment" | recovery retries the action that failed | opens the **"Renew subscription / Pay ₹100"** dialog unconditionally (`runCodeAction` → `doRenew`). Right for renew; wrong for a code produced by an upgrade / downgrade / reauthorize answer. | **BREAK — MAJOR (finding 6)** |
| Stale error across actions | a new action starts clean | "There's no payment in progress for this subscription." remained on screen underneath a freshly opened Renew dialog (`openConfirm` never clears `actionErr`) | BREAK — MINOR (finding 11) |
| Error placement | the failure is visible next to the control that caused it | `actionErr` renders at the page foot, ~350px below the notice button, under the plan grid | BREAK — MINOR (finding 12) |
| Direct un-throttled hammering of `POST /api/method/jarvis.account.check_billing_payment_status` | bounded, like the signup twin | no rate limit anywhere on the chain; the SPA's `busy` ref is the only guard and a direct POST bypasses it. Each call makes a live provider `reconcile_signup`. **Not executed against the live gateway** — refused to hammer a real provider from a review. | NOT RUN (by design) — MAJOR (finding 7), evidenced from code: `billing/signup.py:2990` has `@rate_limit`, `api/account.py:1049` has none |
| Restore the row to Active and re-verify a normal renew (flow step 6) | normal path unbroken | NOT RUN — would mutate live control-plane billing state, and the change set is RED regardless. Must be exercised by the developer before the re-review. | NOT RUN |
| Back / refresh mid-flow, bfcache return | overlay cleared, state re-read | NOT RUN — the only navigation available is the stubbed redirect above; re-test once finding 2 is resolved | NOT RUN |

## Notes

- All stubbed scenarios were run against the real built SPA; only admin's HTTP answer was replaced, and
  only for the three endpoints under test. Every unstubbed call (`get_account`, `is_ready_for_chat`, …)
  hit the real server.
- The single genuinely un-stubbed end-to-end path — Renew, then Check, on the real repro row — is the
  one that dead-ends (row 3). That is the plan's headline scenario.

VERDICT: RED
