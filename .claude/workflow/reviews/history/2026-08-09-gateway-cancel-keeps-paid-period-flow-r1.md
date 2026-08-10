# Flow review — 2026-08-09-gateway-cancel-keeps-paid-period — round 1
Reviewer: Opus (strict-reviewer)
Date: 2026-08-09
Scope: flows actually executed against a running system, not read.

**How it was driven.** The admin control plane was exercised as a running Frappe service on
`test_site` over real HTTP on `127.0.0.1:8002` (`Host: test_site`) — never
`jarvis_admin_v2.local`, never a pool/provision/fleet module. Webhooks were delivered as
genuine POSTs to `.../billing.webhook.razorpay_webhook` with a real HMAC-SHA256
`X-Razorpay-Signature` computed against a webhook secret set on the test site for the run and
cleared afterwards. Customer endpoints were called with real `api_key:api_secret` native auth
issued by `signup._provision_customer_credentials`, i.e. through `current_customer` and the
real permission chain. Eight fixture customers (Active autopay, bench-cancel, double-click,
one-shot Annual, Expired-with-dead-mandate, signup-stage, refund, lapse) were created,
exercised, and deleted; the webhook secret was restored and zero fixtures remain.

Bench UI: `npx vitest run` — 1018 tests / 64 files pass, including the new
`PlanBillingPane.spec.js` (cancel-button visibility on `can_cancel`, both confirm-dialog
copies, confirm/dismiss paths). The pane is driven entirely by the payload verified below and
re-reads via `loadAccount()` after every action, so its rendered state is a function of the
payloads captured here.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| S1. Mid-period autopay sub, signed `subscription.cancelled` over real HTTP | Active preserved, `autorenew=0`, customer Active, `User.enabled=1`, `can_reauthorize` true immediately | `{"ok":true}`; summary went `Active/autorenew 1/has_mandate T/can_cancel T/can_reauthorize F` → `Active/autorenew 0/has_mandate F/can_cancel F/can_reauthorize T/can_renew T/cancel_at_period_end 0`; customer `Active`, `User.enabled=1` | **PASS** — the owner's rule and the "Set up auto-renewal available immediately" requirement both hold |
| S2. Same event redelivered under a new event id | No second write, no status change | No change to any field | PASS |
| S3. Bench "Cancel auto-renewal" (mandate customer) over real HTTP | `Active`, `autorenew=0`, `cancelled_at` NULL, period end unmoved, `cancel_at_period_end:0`, `has_mandate:false`, `can_reauthorize` true | Exactly that (`cancelled_at:""`, `days_remaining:20`, `can_cancel:false`); follow-up summary `can_reauthorize: true` | PASS |
| S4. One-shot Annual "Cancel subscription" (regression pin) | Unchanged: `cancelled_at` stamped, `cancel_at_period_end:1`, Resume available | `cancel_at_period_end:1`, `cancelled_at:2026-08-09 10:54:01`, status `Active`, `can_reauthorize:false` | PASS |
| S5. Expired sub still carrying a dead mandate id → cancel | Hard refusal, not a silent OK | `{"ok":false,"error":{"code":"NotCancellable"}}` | PASS |
| S6. **Double-submit: two cancels fired in parallel** on one sub | One release, second a no-op, no `cancelled_at` stamped by the fall-through | Both returned `ok` with identical payloads, `cancelled_at:""`, `can_cancel:false`; sub ended `Active/autorenew 0/cancelled_at None` | PASS |
| S7. **Signup-stage sub (`Pending Payment` + mandate id + `autorenew=1`), signed `subscription.cancelled`** — what `expire_abandoned_checkouts` → `release_superseded_object` produces daily | Plan edge 11: `autorenew=0` **only**; row stays resumable and the stale-signup reaper keeps it | Status flipped to **`Past Due`**; `signup._signup_payment_state` then returns **`SIGNUP_TERMINAL`** and serves **no pay-page fields** — the customer can never resume and pay | **BREAK** (code-review BLOCKER 1) |
| S8. **Terminal path**: signed full `refund.processed` | Customer IS suspended | Sub `Cancelled`, customer `Suspended`, `User.enabled=0` | PASS — the terminal path still works |
| S9. Gateway cancel on a sub whose period had already ended | `autorenew=0` + Past Due | `Past Due`, `autorenew=0` | PASS |
| S10. Full fixture sweep after all events | Only the intended rows moved; no cross-contamination | autopay `Active/0`, bench `Active/0`, dbl `Active/0`, oneshot `Active/0/cancelled_at set`, expired `Expired`, refund `Cancelled/Suspended/disabled`, sweep `Past Due`, signup `Past Due` | PASS (except S7) |
| S11. Re-arm immediately after a gateway cancel (`reauthorize_autopay`) | Must NOT demand a Resume step first | Passed `_may_reauthorize` and reached the client-capability gate (`CLIENT_UPGRADE_REQUIRED`) — no `ResumeBeforeReauthorize`, no `NotReauthorizable` | PASS |
| S12. `resume_plan` on an autopay-cancelled sub | Nothing is scheduled, so refuse | `{"ok":false,"error":{"code":"NotResumable"}}` | PASS |
| S13. Wind the period past `current_period_end`, then past grace, run the real daily expiry sweep | Full `grace_period_days` (no zero-grace marker), then Expired, and the customer keeps their login so they can renew | `cancelled_at` NULL → grace 7 applied, deadline = period_end + 7; after the sweep: sub `Expired`, customer `Active`, `User.enabled=1` | PASS — plan Q3 holds |
| S14. Forged cancel: `subscription.cancelled` body with a bad signature | 400 `BadSignature` before dispatch, nothing written | `{"ok":false,"error":{"code":"BadSignature"}}`, HTTP 400, no state change | PASS |
| S15. Gateway release raising during a bench cancel (test_site has no Razorpay credentials, so `release_live_mandate` genuinely failed) | Endpoint still returns OK; the local record of switching AutoPay off survives | `ok:true` with the AutoPay payload, `autorenew=0` persisted. Note: the swallowed failure still leaked into `_server_messages` as a red `raise_exception` message ("Password not found … razorpay_key_secret"). The bench's `@/api` layer reads `message` only, so nothing is shown to the customer; pre-existing shape, unchanged by this diff | PASS (noted) |
| S16. Bench pane: cancel button visibility driven by `can_cancel`, both dialog copies, confirm and dismiss | Button hidden once autopay is off; AutoPay copy promises only autopay-off; one-shot copy unchanged; dismiss calls nothing | `PlanBillingPane.spec.js` — all cases pass inside the full 1018-test suite | PASS |
| S17. Bench pane against an admin payload with `can_cancel` absent | Defined behaviour | Not executed — no fixture and no spec case exists for the missing-field shape (code-review finding 8) | NOT RUN |

## Verdict rationale

S7 is a BREAK that reaches a real customer: an abandoned-then-returning autopay signup is
converted from a resumable checkout into `SIGNUP_TERMINAL`, and the trigger is a scheduled
sweep this system runs every day on purpose. Everything else the change set claims — the
owner's rule at both entry points, immediate re-arm, grace preserved, terminal paths still
revoking, idempotency under genuine parallel submission, the auth boundary — was executed
against the running service and survived.

VERDICT: RED
