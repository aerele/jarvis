# Flow review — 2026-08-08-mandate-authorization-proof — round 1
Reviewer: Opus (strict-reviewer)
Date: 2026-08-08
Scope: executed, not read.

**Method.** Direct driving (flow-review.md method C) against the running bench
(`frappe serve --port 8002`, workers + scheduler up) on `test_site`, the sanctioned test site —
never the live control plane. Real `Jarvis Customer` / `Jarvis Subscription` / `Jarvis Plan` /
`Jarvis Payment Intent` rows, the real `initiate_billing_checkout` saga, the real
`_process_return` route seam, the real provider, the real ledger, the real apply seams and the
real `sweep_unconfirmed_mandates` cron entry point. The ONLY things faked are the network
boundary and the environment the bench cannot supply:

- `razorpay_client.fetch_subscription_invoices` / `fetch_payment` — the HTTP calls. Invoice
  shapes are the ones the plan itself measured live (`[]` for a no-add-on mandate; one paid
  ₹100 invoice naming a captured payment for the reactivation).
- `api.tenant.verify_payment_signature` and `confirm._provider_mode` — there are no Razorpay
  credentials on `test_site`.
- `_activation._assign_or_reuse` / `_assigned_tenant_any` / `_connection_for` — pool and fleet,
  deliberately not touched (a jarvis_admin fleet path on a live control plane destroys tenants).

Harness: `/tmp/flowrev_t6.py`, `/tmp/flowrev2.py`, `/tmp/flowrev_diag.py` (outside both repos).
All fixtures cleaned up after each scenario; no repo file and no live tenant was modified.

**NOT RUN, and why.** The browser half — the real Razorpay Checkout redirect, the bank page,
the actual cross-site POST, the pay-page shell's `redirecting: true` handling, and the tenant
`BillingPage.vue` `?pay=done` auto-heal — was not executed. `list_connected_browsers` reports one
Chrome, but the browser tool requires a user selection I cannot obtain from this dispatch. Two
scenarios in the plan's flow list are therefore NOT RUN in a browser: "let the bank step replace
the page" and "a full signup through the new return path". Their SERVER halves were driven and
both broke (S3, S8), so the browser half would only add the visual confirmation.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| S1 Lapsed Monthly Razorpay reactivation: pay the mandate, gateway POSTs `/return` with a signed tuple | access restored, `autorenew=1`, plan applied, sent to `/jarvis/billing?pay=done` | `PAYMENT_CONFIRMED`; status `Active`, `autorenew=1`, `razorpay_subscription_id=sub_FLOWREV`, period end +1 month, `return_url=https://a.example.com/jarvis/billing?pay=done` | PASS |
| S2 Deliver the SAME return a second time (replay) | same answer, no second period | `PAYMENT_CONFIRMED`; `current_period_end` byte-identical | PASS |
| S3a REAUTH (re-arm autopay) — does `/open` attach a callback and drop the handler? | one confirm path per attempt | `callback_url` attached ⇒ `opener.js` attaches NO handler; `/return` is the ONLY path | (setup) |
| S3b REAUTH mandate authorized at the bank, gateway POSTs `/return`; provider answers `[]` invoices (the plan's own live-verified shape) | mandate adopted, `autorenew=1` | **`SESSION_INVALID`**; `autorenew=0`, `razorpay_subscription_id=None`, **nonce burned**, money `UNKNOWN`, apply `PENDING`, customer sent to `/jarvis/billing?pay=failed` | **BREAK — BLOCKER (code #1)** |
| S4 Double Pay across a lost confirm: start a mandate, gateway holds captured money we never recorded, customer clicks Pay again for a different amount | refused; exactly ONE mandate ever minted | `PAYMENT_CONFIRMATION_PENDING` / `confirm_payment`; intents carry exactly `['sub_FLOWREV']` | PASS |
| S5 Re-open the pay page after that lost confirm | F6 refuses to re-offer the sheet | money axis `PAID`; `money_state in COMMITTED_MONEY_STATES` ⇒ F6 refuses | PASS |
| S6b Customer never returns; `*/5` sweep runs (grace exceeded, opened, no incident) | activated with no customer action | `{'checked': 1, 'proved': 1, 'healed': 1}`; status `Active`, `autorenew=1`, apply `APPLIED`, `last_paid_amount_inr=100.0`; ops paged once | PASS |
| S7 Renew an Active Annual sub, then schedule a downgrade AFTER the checkout was priced, then confirm | the scheduled downgrade survives | `scheduled_plan = None` — silently destroyed by `apply_reactivation_target(sub, target==sub.plan)` | **BREAK — MAJOR (code #7)** |
| S8a Is a trial autopay signup mandate-shaped? | yes ⇒ it takes the new topology | `_is_autopay` True ⇒ shape `mandate` ⇒ `callback_url`, no handler | (setup) |
| S8b SIGNUP autopay, trial plan, no fee: mandate authorized, gateway POSTs `/return`, no invoice raised | signup completes end to end (T6 acceptance) | **`SESSION_INVALID`**; subscription stays `Pending Payment`, nonce burned, apply `PENDING`, customer sent to `/jarvis/onboarding?pay=failed` | **BREAK — BLOCKER (code #2)** |
| S9 Mandate UPGRADE returns with an invoice for ₹999 when the intent froze ₹250 | never activated; quarantined | `PAYMENT_UNDER_REVIEW`, money recorded `PAID`, payment id retained, incident opened — caught by the ledger's `resolve_money`, NOT by the provider (the `/return` shim carries no expected amount) | PASS (with code #8 on the missing provider-side check) |
| S10 A captured payment that has been REFUNDED (`amount_refunded == amount`) offered as proof | not proof | accepted as `verified=True` — no `amount_refunded` check exists | **BREAK — MAJOR (code #5)** |
| S11 Two `/open`s (nonce rotates), then the FIRST tab's bank flow returns | recoverable miss with a way home | `SESSION_INVALID` **and `return_url` empty** — customer stranded on the checkout origin with no route back to their workspace | **BREAK — MAJOR (code #6)** |
| Gateway unreachable during a NEW initiation | never the reason a customer cannot pay | initiation still returns a live `pay_page_token` | PASS |
| Gateway unreachable during `/return` | pending, nonce intact, webhook authoritative | provider swallows the exception → `None` → `_invalid()`, nonce already burned | **BREAK — BLOCKER (code #3)** — established by code path (`razorpay.py:282` / `confirm.py:1161`), the same `None` S3b/S8b produced empirically |
| Real Razorpay Checkout redirect through a bank page (browser) | page replaced, server POST arrives, Pay never re-enabled | — | **NOT RUN** (no browser; see Method) |
| Tenant `BillingPage.vue` `?pay=done` auto-heal + param strip (browser) | healer runs once, param stripped | — | **NOT RUN** (no browser) |
| Full signup through the new return path (browser) | signup completes | — | **NOT RUN** (server half driven: S8b, BROKE) |

## Raw output (second batch, verbatim)

```
S6b sweep               -> {'checked': 1, 'proved': 1, 'healed': 1} | status= Active | autorenew= 1 | apply= APPLIED | last_paid= 100.0
S8a plan is autopay(mandate-shaped signup) = True
S8b signup-trial return -> SESSION_INVALID | sub status= Pending Payment | nonce_burned= True | apply= PENDING | customer lands on= https://a.example.com/jarvis/onboarding?pay=failed
S9 upgrade wrong-amount -> intent froze 25000 minor, gateway says 99900 -> PAYMENT_UNDER_REVIEW | money= PAID | recorded pid= pay_W
S10 refunded payment    -> accepted as proof: True | amount_refunded was 10000 of 10000
S11 stale nonce return  -> SESSION_INVALID | status= Expired | return_url=
```

## Verdict rationale

Four flows broke, three of them on the money path, two of them BLOCKERs that make a
customer's successful gateway authorization read as a failure. Three planned scenarios were
NOT RUN for want of a browser. Either condition alone forces RED.

VERDICT: RED
