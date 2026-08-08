# Flow review — 2026-08-07-expired-plan-reactivation — round 2
Reviewer: Opus (strict-reviewer)
Date: 2026-08-08
Scope: executed, not read.
- Browser (Claude in Chrome) against the running bench: `http://jarvis.local:8002/jarvis/billing`
  as the workspace admin, subscription `v4p648rp8g` on `jarvis_admin_v2.local` (Cancelled, Starter ₹100).
- Direct driving of the real endpoints from the page's own authenticated session
  (`fetch` → `jarvis.account.*`, `jarvis.onboarding.renew`).
- Direct driving of the real admin endpoints on `test_site` (`api.tenant.renew`,
  `api.account.get_account_summary`) with only `create_order` mocked, so no gateway object is minted.
- Forensics on the live plane: `Jarvis Payment Intent` rows, `Error Log`, subscription row.

No payment was completed. Live-plane state before and after this review is identical
(`Cancelled / Starter / scheduled_plan=None / autorenew=1`, 2 payment intents).

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| Load `/jarvis/billing` on a Cancelled sub | reactivation grid, full-price note, no prorated cards | Starter (Current plan, ₹100, "Renew") + Growth (₹500, "Renew on this plan") with "Full price… Nothing is prorated, and nothing is scheduled." | PASS |
| Forensics: what happened to the author's own end-to-end payment on this row | payment applied, sub Active | intent gen 2 RENEW: `money_state=PAID`, `pay_TN8EPrWkxjvBuP`, `apply_state=REVIEW_REQUIRED`, `applied_at=NULL`, `incident_open=1`, `incident_code=APPLY_FALLTHROUGH_ON_COMMITTED_MONEY`; Error Log `confirm_payment: order order_TN8BFky9eIyZCl matched no confirmable sub`; sub still **Cancelled** | **BREAK — code finding 1 (money captured, nothing delivered)** |
| Root-cause the confirm miss | the order resolves to its subscription | `Jarvis Subscription.razorpay_order_id` is NULL on the row and **no subscription anywhere carries that order id** — a WS8 renew keeps the handle on the ledger while `_confirm_payment` resolves by `SUB_DT.razorpay_order_id`. Cancelled is additionally excluded from that branch's status filter | **BREAK** |
| Is the customer able to retry after that? (plan edge 14 "re-runnable") | yes | no — `has_open_incident` refuses every initiation; Renew and both new endpoints all answer PAYMENT_UNDER_REVIEW | **BREAK** |
| Did ops actually get paged? | alert delivered | `Error Log: fleet: ops alert NOT DELIVERED (ops_alert_email is unset)` ×2 | **BREAK** (environment, but it is the only safety net behind finding 1) |
| Click "Renew" while ₹100 of the customer's money is parked | some acknowledgement that a payment is being held | dialog offers "Pay ₹100" with no hint anything is held | **BREAK — invites a second payment** |
| Confirm that dialog (safe: refused before any order) | coded `PAYMENT_UNDER_REVIEW` row with its declared action | bare red line "A payment on this account is under review; please contact support." — no code row, no action, no Check affordance | **BREAK — code finding 6** |
| Reactivate onto a DEARER plan, then click plain "Renew" (driven end to end on `test_site`) | ₹100 dialog → ₹100 order | stage Growth → `amount_inr 500`, `scheduled_plan=growth` persists; summary shows card price `100.0`, `scheduled_plan: ''`, `reactivation_target: growth` (never read by the SPA); plain `renew()` → **`amount_inr 500`** | **BREAK — code finding 2 (5× mischarge, wrong plan applied)** |
| Reactivate onto a CHEAPER plan | full price, no credit, nothing scheduled | `amount_inr` = the cheaper plan's full price, target staged, no proration | PASS |
| Past-Due-with-no-days-left cohort: offer vs accept | offer and accept agree | offer `can_reactivate: True` + full grid; **every** accept (with and without `target_plan`) → `AutoRenewActive` "no manual renewal needed", no order minted | **BREAK — code finding 5** |
| `target_plan` type confusion over the wire: `{}`, `[]`, `7` | refused before any admin round trip | 417 `FrappeTypeError` at the tenant's own signature boundary | PASS |
| `target_plan` hostile strings: `' OR 1=1 --`, `../../etc/passwd`, 5000×`A` | no injection, no 500, no traceback to the customer | all reached the admin and were stopped by the incident gate; parameterised docname lookup, no SQL surface. The refusal message echoes the whole 5000-char string back (code finding 12) | PASS (with finding 12) |
| Active healer over the real wire (`jarvis.account.check_billing_payment_status`) | coded envelope the SPA can decode, never a transport error | HTTP 409 `{ok:false, error:{code:"PAYMENT_UNDER_REVIEW", recovery:"contact_support"}}` — the RAW-call + `paymentCodec.decode` integration fix works | PASS |
| Passive read over the real wire (`jarvis.account.get_billing_payment_state`) | coded answer or an honest error | 417 `ValidationError` (throws by design). In `doCheckStatus` that lands in the catch and renders "We could not reach the payment service" about a service that answered | **BREAK (narrow) — code finding 10** |
| Both new endpoints unauthenticated / as a non-admin | refused before any admin round trip | not re-driven in the browser this round (single admin session). Covered by `test_role_gates` 6/6 — both endpoints and `jarvis.onboarding.renew` are in `GATED_ENDPOINTS` — and by `test_gate_runs_before_any_admin_round_trip` asserting the admin mocks were never called | NOT RUN (browser); PASS (automated) |
| Double-click reactivate / direct repeat POST → at most one gateway object | one intent, one order | **NOT RUN** — the repro subscription is blocked by the open incident, so no initiation can be made on it, and no admin test fires two reactivations. The different-plan variant is unguarded by inspection (code finding 4) | NOT RUN |
| Check payment status must not navigate | Resume offered, no auto-navigate | **NOT RUN in the browser** — with the money parked there is no coded notice, so `doCheckStatus` is unreachable from the UI in this state. Covered by `BillingPage.spec.js` "F2" ×4 | NOT RUN (browser); PASS (automated) |
| Resume a settled/parked checkout from `get_billing_payment_state` | must not re-offer a paid pay page | **NOT RUN** — blocked by the incident. The plan itself records this as a known trade-off of Design B | NOT RUN |
| Restore to Active and re-check the prorated upgrade/downgrade paths | unchanged | **NOT RUN** — would require resolving a real parked-money incident on the live row; refused on principle. Covered by `test_account_endpoints` 60/60 and `BillingPage.spec.js` edge 12 | NOT RUN |
| Page state hygiene | error renders near the controls, above the plan grid | verified in the screenshot: the failure line sits between the plan summary and the "Plans" heading (round-1 finding 12 closed) | PASS |
| Copy sanity on the live pane | a Cancelled sub does not claim to be renewing | header reads "Renews 2026-09-07 · 31 days left · **Auto-renew on**" above a **Cancelled** badge. The autorenew half is what patch `v1_27` fixes and it has not been run on `jarvis_admin_v2.local`; the "Renews …" wording does not branch on status | BREAK (cosmetic) — code finding 15's neighbour, recorded as m9 in the report |

## Notes on method

Scenarios marked NOT RUN are blocked by the live plane's own state: the author's end-to-end payment
left a durable parked-money incident on `v4p648rp8g`, and `assert_no_open_incident` refuses every
initiation while it is open. I deliberately did not clear that incident — resolving real parked money
is not a reviewer's call, and the incident is itself the primary evidence for finding 1. Re-run this
flow review on a clean lapsed subscription once findings 1 and 2 are fixed; the double-submit,
resume-a-settled-checkout, and restore-to-Active scenarios must all be executed before any PR.

VERDICT: RED
