# Flow review — 2026-08-08-mandate-authorization-proof — round 2
Reviewer: Opus (strict-reviewer)
Date: 2026-08-08
Scope: executed, not read. Chrome + real HTTP + a live Razorpay TEST-mode mandate.

**Method.** Round 1's browser half was NOT RUN; this round it was. Two drivers were used:

- **Claude in Chrome (method B)** against the running bench (`frappe serve --port 8002`,
  workers + scheduler up), on the LOCAL control plane `jarvis_admin_v2.local`
  (`jarvis_pay_origin = http://jarvis_admin_v2.local:8002`, `razorpay_key_id = rzp_test_TD5…`,
  confirmed TEST mode before anything was created) and the local tenant `jarvis.local`
  (frontend built 15:16 today; the built `BillingPage-CESlXGDE.js` carries the change).
- **Direct HTTP (method C)** with `curl` for the cross-site `/return` battery, and
  `test_site` harnesses (`/tmp/rev2_a.py`, `/tmp/rev2_b.py`, `/tmp/rev2_b2.py`, `/tmp/rev2_c.py`)
  for the state machines a browser cannot pose.

**A real payment was made.** A synthetic customer (`revflow-r2@example.com`, its own
`Jarvis Customer`/`Jarvis Subscription`, never a real one) was renewed through the REAL
`renew()` seam, which minted the real Razorpay test-mode mandate `sub_TNFz0BQXBMMTpP` with a
₹100 upfront add-on. The card e-mandate was authorized at the bank in Chrome; Razorpay
captured `pay_TNG0BIS6DOajDX` (₹100, invoice `inv_TNFz0tU1dlJHwV` paid) and POSTed the
callback. **The callback was deliberately abandoned at Chrome's mixed-content interstitial**,
which reproduced the 2026-08-08 incident exactly — money at the gateway, `money_state UNKNOWN`
locally — and gave the recovery layers a real subject.

**Cleanup.** `sub_TNFz0BQXBMMTpP` cancelled at Razorpay; the synthetic customer, subscription,
intents and user deleted. Verified afterwards: the real subscription `v4p648rp8g` is
byte-identical to before, and the tenant list is unchanged (no container created, none
destroyed). No repo file was modified.

**Deliberately NOT RUN, and why.** The final APPLY of a live return
(`_apply_reactivation_mandate` → `activate_and_assign`) was not executed. The local control
plane has no warm pool tenant and the `laptop` host has headroom (2/10), so the cold-provision
fallback would have created a REAL container for a synthetic customer. Creating and then
tearing down fleet objects on the shared control plane is the one hazard a reviewer must not
introduce. The apply seam was instead driven to completion on `test_site` and, for the live
return, short-circuited through `_confirm_payment`'s own mandate idempotency guard so the full
route ran with the activation as a no-op.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| F1 Lapsed Monthly Razorpay customer renews onto a plan (real `renew()`, real gateway) | a MANDATE-shaped attempt with a ₹100 upfront add-on | `shape=mandate`, `amount_minor=10000`, `provider_subscription_id=sub_TNFz0BQXBMMTpP`, Razorpay price summary "₹100 now, then ₹100 every month" | PASS |
| F2 Real pay page loads in Chrome | summary + one Pay button | "Jarvis (Aerele) / Subscription for Rev Flow Co / Starter ₹100.00 / Pay securely" | PASS |
| F3 **Exactly one confirm path per attempt (edge case 13)** — instrument `window.Razorpay`, click Pay | `callback_url` + `redirect`, NO handler, NO modal | opts = `{key, name, subscription_id, callback_url: ".../jarvis-checkout/return?nonce=rtn_70e8…", redirect: true}`; `handlerAttached=false`, `modalAttached=false`; Pay button left DISABLED | PASS |
| F4 Card e-mandate authorizes at the bank; the page is replaced and the browser closure is gone | the result comes back as a server POST, not a JS callback | Razorpay redirected to `POST /jarvis-checkout/return?nonce=…`; Chrome raised "Form is not secure" (HTTP bench only — production's pay origin is HTTPS) | PASS |
| F5 **The confirm is LOST** (interstitial abandoned) — does T1 find the money? | the proof names the captured payment and moves the money axis | gateway: `status=authenticated`, `inv_TNFz0tU1dlJHwV` paid ₹100 → `pay_TNG0BIS6DOajDX` captured, `amount_refunded=0`. `prove_unrecorded_mandate_money` → `True`, `money_state UNKNOWN→PAID`, `provider_payment_id=pay_TNG0BIS6DOajDX` | PASS |
| F6 **The double-charge door** — re-open the pay page in Chrome after that lost confirm | settled result, no Pay affordance | "Confirming your payment … You can safely return to your workspace" + "Back to your workspace". No Pay button, ever | PASS |
| F7 **Pay twice across a lost confirm** — call `renew()` again for the same subscription | exactly ONE mandate at the gateway | second `renew()` REUSED the live attempt (frozen tuple match); gateway mandates minted for the subscription = `['sub_TNFz0BQXBMMTpP']` — one | PASS |
| F8 Result-page exit routes by flow (T5) | a BILLING flow returns to `/jarvis/billing`, not the signup wizard | "Back to your workspace" navigated to `http://jarvis.local:8002/jarvis/billing` | PASS |
| R1 `/return` with a valid nonce and NO signature | fail closed, nonce NOT burnt | 400, "This payment link is no longer valid", `return_nonce_consumed_at` still NULL, money untouched, `return_url = http://jarvis.local:8002/jarvis/billing?pay=failed` | PASS |
| R2 `/return` with a forged signature | same | 400, same, nonce intact | PASS |
| R3 `/return` aimed at another subscription id | `sub_id_mismatch`, nothing consumed | 400, nonce intact | PASS |
| R4 `/return` with an unknown nonce | generic invalid, no disclosure | 400, generic body, `return_url = ""` (no intent exists to resolve — inherent, and no longer reachable by rotation) | PASS |
| R5 `/return` with a duplicated form key | rejected as ambiguous | 400, generic | PASS |
| R6 `/return` posted to a non-canonical Host | refused | 404 | PASS |
| R7 **GENUINE signed return** (real HMAC over `pay_TNG0BIS6DOajDX\|sub_TNFz0BQXBMMTpP`, real `verify_subscription_payment_signature`) | confirmed, workspace exit | 200, "Payment confirmed", `return_url = http://jarvis.local:8002/jarvis/billing?pay=done` | PASS |
| R8 **REPLAY of that return with a DIFFERENT payment id and a junk signature** | the body is ignored; server truth decides (the Cashfree property) | 402 "Payment not completed" — and the money row's `provider_payment_id` was **rewritten** from `pay_TNG0BIS6DOajDX` to `pay_TND32nW9k2pIfP` (a fully-refunded ₹5 payment from another subscription) by an UNSIGNED POST | **BREAK — BLOCKER (code #1, and #6 on the missing refund/capture check)** |
| R9 (`test_site`) Same replay against an intent whose first return ended in a pending | no money advance from an unsigned body | `money_state UNKNOWN→AUTHORIZED` with the forged id, apply PENDING → committed money that `_blocks_supersede` refuses to supersede: the customer can no longer pay | **BREAK — BLOCKER (code #1)** |
| A1 (`test_site`) Expired customer picks their CURRENT plan on the reactivation grid with a stale downgrade scheduled | the stale schedule is cleared (round-3 behaviour) | `landed_on = the plan paid for`, `scheduled_plan = None` | PASS |
| A2 (`test_site`) **Past Due, 0 days left, no live mandate** — same scenario, the other half of the cohort `can_reactivate` admits | same | grid_shown=True, paid for the ₹500 plan, **landed on the ₹100 plan**; `_apply_scheduled_plan_change` applied the stale downgrade and would resize the container down | **BREAK — BLOCKER (code #2)** |
| C1 (`test_site`) Free-trial autopay signup, confirm lost, customer returns AFTER the trial ended and the first cycle was charged | a recurring cycle charge is never the authorization (edge case 3) | `ConfirmOutcome(verified=True, payment_id='pay_CYCLE1', paid_amount_inr=3000.0)`; `_kept_authorization_amount` records **0.0**; a fresh free trial window would be granted | **BREAK — MAJOR (code #3)** |
| B1 (`test_site`) REAUTH mandate through `/return` (round-1 S3b reproduction) | confirms; autopay armed | `PAYMENT_CONFIRMED`, `money_state=AUTHORIZED`, nonce consumed | PASS (round-1 BLOCKER 1 fixed) |
| B2 (`test_site`) Trial SIGNUP mandate through `/return` (round-1 S8b reproduction) | confirms | `PAYMENT_CONFIRMED` | PASS (round-1 BLOCKER 2 fixed) — but the apply seam is stubbed, see code #7 |
| B3 (`test_site`) Gateway unreachable during `/return` | honest pending, never a decline | `PAYMENT_CONFIRMATION_PENDING`, HTTP 202 | PASS (round-1 BLOCKER 3 fixed) |
| T1 Tenant `/jarvis/billing?pay=done` auto-heal (browser) | the healer runs exactly once | `jarvis.account.check_billing_payment_status` fired once per load, followed by `get_billing_payment_state` + `get_account` | PASS |
| T2 Tenant `/jarvis/billing?pay=failed` (browser) | no healer — nothing to converge on | no `check_billing_payment_status` request | PASS |
| T3 Tenant `/jarvis/billing?pay=done&tab=usage&keepme=1#anchor-x` — do other params survive? (MINOR 14) | only `pay` removed | arrived as `/jarvis/billing`: query AND hash gone. The same happens with NO `pay` param at all, so the router discards them and the fix is inert in the app | MINOR (code #10) — not a regression |
| T4 Lapsed cohort's reactivation grid renders on the real tenant | full-price cards, no proration copy | "Renew on this plan / Full price for this plan… Nothing is prorated, and nothing is scheduled" | PASS |
| Live return driven to APPLY (`activate_and_assign`) | period granted, autopay armed, container assigned | — | **NOT RUN** — would cold-provision a real container on the shared local control plane (no warm pool, host headroom 2/10). Covered on `test_site` instead |
| Sweep (T7) driven to activation against a live gateway | customer who never returns is activated | — | **NOT RUN**, same reason; round-1 S6b covered it on `test_site` (`{'checked':1,'proved':1,'healed':1}`) |
| Full SIGNUP through the new redirect in a browser | signup completes, trial window + coupon unchanged | — | **NOT RUN** — no signup fixture can be driven on this control plane without a pool assignment; and no test covers it either (code #7) |

## Verdict rationale

Four flows broke. Two are BLOCKERs: an unsigned cross-site POST rewrote the payment id on a
settled money row (R8, reproduced against a live Razorpay mandate), and a customer paying for
the plan they chose on the reactivation grid landed on a different, cheaper one (A2). A third
credits a recurring cycle charge as a mandate authorization and records ₹0 for it (C1).
Three planned scenarios remain NOT RUN, one of which (signup autopay end to end) is also
untested in code. Either condition alone forces RED.

The round-1 breaks are genuinely repaired: S3b, S8b and the gateway-outage case all pass now,
the stranded-customer return URL is fixed, the double-charge door held against a REAL lost
confirm, and the T6 topology was confirmed in a real browser with `handler` provably absent.

VERDICT: RED
