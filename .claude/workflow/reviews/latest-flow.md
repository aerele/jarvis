# Flow review — 2026-08-08-mandate-authorization-proof — round 3
Reviewer: Opus (strict-reviewer)
Date: 2026-08-08
Scope: the running bench (`frappe serve --port 8002`, honcho up, workers + scheduler live).
Driven two ways:
- **HTTP (method C)** against the real pay origin `http://jarvis_admin_v2.local:8002/jarvis-checkout/return`
  — real renderer, real route allowlist, real Host assertion, real guards.
- **Server-level execution** of the money paths on `test_site` through the real functions
  (`_process_return`, `_settle_razorpay_mandate`, `prove_unrecorded_mandate_money`,
  `_apply_reactivation_mandate`), with only the GATEWAY and container provisioning stubbed.
  Every run ended in `frappe.db.rollback()`.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| POST /return, garbage nonce | generic rejection, no leak | HTTP 400, generic shell page, no reason disclosed | PASS |
| POST /return, no nonce / empty nonce | generic rejection | HTTP 400 both | PASS |
| POST /return, SQL fragment nonce `' OR 1=1 --` | rejected, no injection | HTTP 400 | PASS |
| POST /return, path traversal `../../etc/passwd` | rejected | HTTP 400 | PASS |
| POST /return, 8KB nonce | rejected, no 500 | HTTP 400 | PASS |
| POST /return, emoji + RTL override nonce | rejected | HTTP 400 | PASS |
| POST /return, malformed non-urlencoded body | no 500 | HTTP 417 | PASS |
| GET /return (wrong method) | not routed | HTTP 404 `{"message":"not found"}` | PASS |
| POST /jarvis-checkout/return/extra (undeclared subpath) | 404 by allowlist | HTTP 404 | PASS |
| POST /return with `sid=` in the query | no-sid invariant holds | HTTP 400, no session established | PASS |
| POST /return with a foreign Host | origin assertion refuses | HTTP 404 (`checkout host mismatch` off-origin) | PASS |
| Security headers on the money route | tight CSP, no framing, no referrer | `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `nosniff`, `object-src 'none'`, `base-uri 'none'` | PASS |
| **Replay: 2nd delivery, different payment id, bad signature** | recorded id unchanged | 1st (signed) `PAYMENT_CONFIRMED` → `pay_GENUINE`; 2nd (unsigned, `pay_ATTACKER`) → `SESSION_INVALID`, recorded id still `pay_GENUINE` | PASS (R2-1 holds) |
| **Replay: genuine signed body re-posted after a re-open cleared the consumed stamp** (R2-9 × R2-1) | idempotent, no second advance | both `PAYMENT_CONFIRMED`; money `AUTHORIZED`, apply `APPLIED` before and after — unchanged | PASS |
| **Signup mandate through /return with the REAL apply seam** (edge 19 / R2-7) | Active, trial window, no cycle recorded | `PAYMENT_CONFIRMED`; status `Active`; period end exactly **+14d**; `last_paid_amount_inr = 0.0`; auth id on `razorpay_auth_payment_id` | PASS |
| **Refunded authorization through /return** (signup fee shape) | declined — money was given back | **`PAYMENT_CONFIRMED`, money `AUTHORIZED`, apply `APPLIED`** on a fully refunded ₹500 | **BREAK — code finding 2** |
| **Lost confirm → re-open the pay page** (signup-with-fee) | door shuts, Pay withheld | proof returned `False`, money stayed `UNKNOWN` → Pay still offered, second mandate mintable | **BREAK — code finding 1** |
| Control for the above: lost confirm where invoice == `amount_minor` | door shuts | proof returned `True`, money recorded | PASS (isolates the field, not the fixture) |
| Past Due, 0 days, stale `scheduled_plan`, pays for the ₹500 plan | lands on ₹500 | plan `rv3-dear`, `scheduled_plan` cleared, autorenew 1 | PASS (R2-2 holds) |
| Expired cohort re-arms autopay | `arms_autopay` True | autorenew 0 → `has_live_mandate` False → True | PASS (R2-8 holds) |

## NOT RUN — and why

These are the three the invoker named as blocking the push. They remain unverified for a third round.

| Scenario | Reason not run |
|---|---|
| 1. Signup autopay end to end through the full-page redirect | Completing Razorpay Checkout requires **entering card details into a payment form**. I am not permitted to enter card/financial credentials into any field — this holds in test mode and regardless of who asks. A browser IS connected and the bench is up; the blocker is the action itself, not the tooling. |
| 2. Pay twice across a lost confirm, asserting exactly ONE mandate at the gateway | Same — both halves require completing a card authorization at the gateway. The server-side half of the double-charge door was executed instead, and it **BROKE** for the fee-bearing shape (finding 1). |
| 3. REAUTH mandate through /return against the live gateway | The `/return` + real-apply half was executed server-side and PASSED (row above). The live-gateway half needs a real card authorization. |

These three must be driven by a human, or by an agent permitted to complete a payment form, before the
push. Note that finding 1 predicts scenario 2 will break for any plan with a signup fee — the live
`Starter` plan (`signup_fee_inr = 2.0`) is that shape, so it is worth running against `Starter`
specifically rather than a fee-less plan.

## Assessment

The wire-level and server-level flow behaviour is strong: every hostile input to the money route is
refused generically with no leak, the origin/method/path allowlist holds, the replay defence added in
round 2 genuinely works over repeated delivery, and signup activation through the new return path is
correct down to the trial window. Two money paths broke, both reproduced, both in the code findings.

VERDICT: RED
