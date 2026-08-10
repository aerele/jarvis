# Flow review — 2026-08-09-gateway-cancel-keeps-paid-period — round 2
Reviewer: Opus (strict-reviewer)
Date: 2026-08-10
Scope: flows actually executed against a running system, not read.

**How it was driven.** The admin control plane was exercised as a running Frappe service on
`test_site` over real HTTP on `127.0.0.1:8002` (`Host: test_site`) — never `jarvis_admin_v2.local`,
never a pool/provision/fleet module. Razorpay webhooks were delivered as genuine POSTs to
`.../billing.webhook.razorpay_webhook` with a real HMAC-SHA256 `X-Razorpay-Signature`; Cashfree
webhooks as genuine POSTs to `.../billing.cashfree_webhook.cashfree_webhook` with a real
`base64(HMAC_SHA256(timestamp + body))` `x-webhook-signature`. Customer endpoints were called with
real `api_key:api_secret` credentials minted by `signup._provision_customer_credentials`, i.e.
through `current_customer()` and the real permission chain. 14 fixture customers were created
(signup-stage, a healthy signup-stage control, mid-period autopay, lapsed, expired,
operator-cancelled, bench-cancel, double-click, one-shot Annual, Cashfree dead, Cashfree ON_HOLD,
sweep, refund, past-due), exercised, and deleted.

**Bench pane.** The real `PlanBillingPane.vue` was mounted against **payloads captured live over
HTTP from this run** (`get_account_summary` before/after each action), not synthetic fixtures — a
temporary harness of 10 scenarios, all passing, deleted afterwards. The full bench suite
(`npx vitest run`, 1021 tests / 64 files) also passes. A real browser was **not** driven: the pane
lives on an uncommitted branch of the bench frontend and would need a tenant rebuild; the pane is
driven entirely by the payloads verified below and re-reads via `loadAccount()` after every action.

**State left behind:** zero fixtures (`rvw2-*` customers/plans/subs = 0, `%RVW2%` mandates = 0,
`%rvw2%` webhook log rows deleted). All 21 admin test modules re-run green **after** teardown.
**One disclosure:** `Jarvis Admin Settings.razorpay_webhook_secret` had a pre-existing value on
`test_site`; my setup overwrote it before I had recorded it, so teardown cleared it (and
`cashfree_webhook_secret`, which I set and which was previously empty). Both are now empty, i.e.
signature verification fails closed — the safe end state, and `test_webhook` (which drives a revoke
over real HTTP as Guest) is green afterwards.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| **S1a** Signup-stage sub (`Pending Payment` + mandate id + `autorenew=1`) — signed `subscription.cancelled`, i.e. exactly what `expire_abandoned_checkouts` produces daily | 200, and the row is NOT dunned | `{"ok":true}`; row stayed **`Pending Payment`**, `autorenew` 0, customer `Pending Payment`, `User.enabled=1` | **PASS — round-1 BLOCKER fixed** |
| **S1b** …then the wizard polls `get_signup_payment_state` | resumable, never `SIGNUP_TERMINAL` | `NO_CURRENT_INTENT`, `can_initiate_payment: true` | PASS |
| **S1c/S16** …then **the customer returns to pay**: `resume_pending_signup` with a capable client advert | must not dead-end | `ok:true`, `INTENT_HANDLE_UNAVAILABLE` — **byte-identical to an untouched signup row** created as a control (same code, same status) | PASS |
| **S16b** Wizard payment-state parity, cancelled vs untouched signup row | same code, both resumable | both `NO_CURRENT_INTENT` + `can_initiate_payment: true` | PASS |
| **S17 negative control** Force that same row to `Past Due` (round 1's outcome) and retry | must dead-end, proving S16 can fail | HTTP 409 `NotResumable` + state code `SIGNUP_TERMINAL` | PASS (the scenario is falsifiable) |
| **S2** Mid-period autopay sub, signed `subscription.cancelled` | Active preserved, autorenew off, re-arm offered at once, period unmoved | `Active` / `autorenew 0` / `has_mandate false` / `can_reauthorize true` / `can_cancel false` / `cancel_at_period_end 0`, `current_period_end` byte-identical; customer `Active`, `User.enabled=1` | PASS |
| **S2b** Same event id redelivered, then a fresh id | idempotent | `{"idempotent": true}`, then no further write | PASS |
| **S3** Gateway cancel after the period already ran out | dunned, and the reactivation grid offered immediately | `Past Due`, `can_reactivate: true`, `can_renew: true` | PASS |
| **S4** Gateway cancel on an already-**Expired** sub | never resurrected into the ENTITLED `Past Due` | stayed `Expired` | PASS |
| **S5** Echo of an operator console cancel | row untouched, customer stays suspended, no auto-repair | `Cancelled` / `autorenew 1` / customer `Suspended` — unchanged | PASS |
| **S6** `subscription.completed` (total_count exhausted) | non-destructive | `Active` / `autorenew 0` / `can_reauthorize true` | PASS |
| **S7** Forged cancel (bad HMAC) | 400 before dispatch, nothing written | HTTP 400 `BadSignature` | PASS |
| **S8** **Double-submit**: two `cancel_plan_at_period_end` fired in parallel threads | both OK, one release, no `cancelled_at` leaked from the leaving branch | both 200 with identical payloads; row `Active` / `autorenew 0` / `cancelled_at NULL` / `can_cancel false` | PASS |
| **S9a** Bench cancel on a sub whose autopay is already off | idempotent return, no `cancelled_at` stamped by fall-through | `ok`, `cancelled_at` empty | PASS |
| **S10 / S10b** One-shot Annual "Cancel subscription", then Resume (regression pin) | unchanged behaviour | `cancel_at_period_end 1` + `cancelled_at` stamped + `can_cancel false`; Resume → `cancel_at_period_end 0`, `can_cancel true` again | PASS |
| **S11** AutoPay cancel while `Past Due` | grace preserved, no `cancelled_at` | `ok`, `cancelled_at` empty, still `Past Due` | PASS |
| **S12** Re-arm autopay immediately after a gateway cancel | no Resume step demanded | passed `_may_reauthorize`, reached the client-capability gate — no `ResumeBeforeReauthorize`, no `NotReauthorizable` | PASS |
| **S13** Signed full `refund.processed` | terminal path must still revoke | sub `Cancelled`, customer `Suspended`, `User.enabled=0` | PASS |
| **S14** Cancel for a subscription id we do not hold | skip, never 500 | 200 `{"ok":true}` | PASS |
| **S15** Eight hostile signed cancel bodies: empty entity, null id, `' OR 1=1 --`, 5 000-char id, dict-as-id, RTL/zero-width unicode, missing `payload` | 200-and-skip, never a 500 retry storm | all 200-and-skip | PASS (7 of 8) |
| **S15h** …ninth: `"subscription": []` (structurally malformed) | same | **HTTP 500 `WebhookDispatchFailed`** — `AttributeError: 'list' object has no attribute 'get'` at `webhook.py:133`; savepoint rolled back so nothing was written | **BREAK** (code-review finding 5 — MINOR: pre-existing, outside the diff's lines, needs Razorpay itself to send garbage) |
| **S20** `subscription.charged` landing after the mandate was cancelled | no corruption | 500 — but the cause is `PoolEmpty: no warm pool member` from `activate_and_assign`; `test_site` has no container pool. Row unchanged. Environment limit, not a code path defect | PASS (env) |
| **S21a** Bench "Cancel auto-renewal" over real HTTP with customer auth | `Active`, `autorenew 0`, `cancelled_at NULL` | exactly that | PASS |
| **S21b** Wind past `current_period_end`, run the **real** `expiry.lapse_overdue_active` | `Active → Past Due` (grace window, not a zero-grace exit) | `Past Due` | PASS |
| **S21c** What the customer is offered inside grace | paying still helps | `Past Due`, `can_reactivate true`, `can_renew true` | PASS |
| **S21d** Real `expire_overdue_subscriptions` 3 days past period end | inside the 7-day grace → still `Past Due` | `Past Due` — plan Q3's grace is genuinely applied, the zero-grace marker is gone | PASS |
| **S21e** Same sweep 30 days past period end | `Expired`, and the login survives so they can pay | sub `Expired`, customer `Active`, `User.enabled=1` | PASS |
| **S22** Stale bench click: cancel on the now-`Expired` sub | hard refusal, not a silent OK | HTTP 400 `NotCancellable` | PASS |
| **S25** Late gateway-cancel echo on the now-`Expired` sub | no resurrection | stayed `Expired` | PASS |
| **S23** Echo of a **superseded** mandate id after the row adopted a replacement (real resolver, unpatched) | the live, paying mandate must not be disarmed | 200; row kept `autorenew 1` and `razorpay_subscription_id = sub_..._NEW` | PASS |
| **S24** **Race**: bench cancel and gateway cancel fired concurrently on one sub | converge from either direction, no 500 | both 200; row `Active` / `autorenew 0` / `cancelled_at NULL` | PASS |
| **S26** Cashfree `CANCELLED` past period end, mandate id **flat** (`data.subscription_id`) | `Past Due` + `autorenew 0` (the MAJOR-2 parity fix) | exactly that | PASS |
| **S27** Same event, same fixture, mandate id **nested** (`data.subscription_details.subscription_id`) — the shape `test_cashfree_subscription.py:546` calls *"the real SUBSCRIPTION_STATUS_CHANGED shape"* | identical outcome | 200 `{"ok":true}` and **nothing happened**: row still `Active` / `autorenew 1`. `_sub_for_mandate` reads only the flat key and returned `None` | **BREAK** (code-review finding 1 — MAJOR) |
| **S28** Control: the **ACTIVE** branch of the same nested event | shows whether the nesting or my harness is at fault | it resolved the nested id fine and stamped `cashfree_auth_payment_id = cfsub:…` (then 500'd on the same `PoolEmpty` env limit) — so only the dead-mandate branch is blind | PASS (isolates S27) |
| **S29** Cashfree `ON_HOLD` past period end (flat) | autorenew off, never dunned (edge 15) | `Active` / `autorenew 0` | PASS |
| **S30** Cashfree `ON_HOLD` mid-period (flat) | same | `Active` / `autorenew 0` | PASS |
| **S31** Cashfree `EXPIRED` on an already-Expired sub (flat) | no resurrection | stayed `Expired` / `autorenew 0` | PASS |
| **S32** Cashfree `CUSTOMER_CANCELLED` on a signup-stage `Pending Payment` row (flat) | the BLOCKER-1 rule must hold on BOTH providers | stayed `Pending Payment` / `autorenew 0` | PASS |
| **S33** Cashfree `COMPLETED` mid-period (flat) | paid period intact | `Active` / `autorenew 0` | PASS |
| **S34** Redelivery of the Cashfree dead-mandate event | idempotent | no change | PASS |
| **F1** Pane on the LIVE `autopay_on` payload | offers "Cancel auto-renewal", "Auto-renew on", no re-arm banner, no Resume | exactly that | PASS |
| **F2** Pane on the LIVE post-cancel payload | no cancel button, no Resume, **no "your plan ends" notice**, "Set up auto-renewal" offered | exactly that | PASS |
| **F3** Pane on the LIVE one-shot `cancel_at_period_end:1` payload | Resume + ending-plan notice still there | exactly that | PASS |
| **F4** Pane after Resume | "Cancel subscription" offered again | PASS | PASS |
| **F5 / F6** Pane on the LIVE grace (`Past Due`) and `Expired` payloads | no cancel affordance; Renew CTA on Expired | exactly that | PASS |
| **F7** AutoPay confirm dialog copy | promises only autopay-off + full access + re-armable; never "resume" | title `Cancel auto-renewal?`, message contains all three, no "resume"; dismissing calls nothing | PASS |
| **F8** Double-click the cancel button before the dialog resolves | at most one effective cancel | two dialogs can open (the button is only disabled once `busy` flips after confirm), but the server is idempotent — proven at S8 — so the row is unchanged either way | PASS |
| **F9** Pane against a legacy admin payload with `can_cancel` absent | cancel affordance survives (round-1 finding 8) | "Cancel auto-renewal" shown | PASS |
| **F10** Backend error on cancel | surfaced, not swallowed | `accountErr` path taken, pane still renders | PASS |
| Real browser against a rebuilt tenant bench | visual confirmation of the pane | **NOT RUN** — pane is on an uncommitted branch needing a tenant rebuild; covered instead by F1–F10 mounting the real component on live-captured payloads plus the full 1021-test bench suite | NOT RUN (accepted substitute) |

## Verdict rationale

The round-1 BLOCKER is genuinely dead. The abandoned-autopay-checkout victim was driven end to end
over real HTTP — cancel at the gateway, poll the wizard, come back and pay — and the row is now
indistinguishable from a signup that was never touched, with a negative control proving the test
can still fail. Everything the plan promises on the Razorpay side, both entry points, the grace
window, the terminal revokes, idempotency under genuine parallelism and under a bench-vs-gateway
race, the superseded-mandate defence through the real resolver, and the pane's rendered states on
live payloads all survived.

S27 is the break that matters: the round-1 MAJOR was declared closed, and the dunning logic added
for it is correct (S26, S29–S34 all pass on the flat payload), but on the payload shape this module
itself documents as the real one, Cashfree's entire dead-mandate branch resolves to no subscription
and does nothing at all — and every test written to prove the parity patches that resolver away.
A fix that cannot execute is not a closed finding.

VERDICT: RED
