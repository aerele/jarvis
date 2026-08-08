# Plan: A mandate payment can no longer go missing
STATUS: APPROVED
Date: 2026-08-08
Owner: Fable (team leader)

## Goal

A mandate payment that succeeded at the gateway reaches Jarvis — by the return path when
the browser survives, by the customer's own status check when it does not, and by a sweep
when the customer never comes back. And a lost confirm can never be charged for twice.
Today the only thing that rescues any of this is a webhook.

## Context

**The incident, 2026-08-08 on `jarvis.local`.** Intent `bcvho5i3b9` (RENEW, mandate,
₹100, sub `v4p648rp8g`) minted `sub_TNDDkv0gNwpVwF`. Razorpay: subscription
`authenticated`, invoice `inv_TNDDlgYUvc2gMw` **paid**, payment `pay_TNDE643QZc76yl`
**captured ₹100** (plus `pay_TND32nW9k2pIfP` ₹5 card-validation, auto-refunded — not a
second charge). Jarvis: the intent sat at `checkout_state OPENED / money_state UNKNOWN /
apply_state PENDING`, revision 4, untouched after creation. No Error Log. **The customer
paid, the mandate armed, and Jarvis knew nothing.** Restored by hand.

**Why the confirm was lost.** A card e-mandate authorizes at the bank, so Razorpay
navigates the top window. `checkout/opener.js` attaches a `handler` only and never a
`callback_url` ("frozen topology; the two are never attached together"). Once the page is
replaced, that closure is gone and there is no server-side callback either. The designed
backstop is the `subscription.authenticated` webhook — unreachable on localhost, and in
production the *single* point of recovery for this class. The server-side facts are all
consistent with this; the browser sequence itself is not provable from our data.

**What makes the fix possible.** `providers/razorpay.py` declines to implement
`fetch_mandate_authorization`, stating: *"Razorpay's Subscription entity carries no route
back to the authorization PAYMENT id"*. **That is false for any mandate that charges
something upfront.** `client.invoice.all({"subscription_id": ...})` returns the add-on
invoice with `status: paid` and a captured `payment_id` — the call that found the lost
₹100. The comment was written when mandates only ever took the auto-refunded ₹5
validation, which raises no invoice.

**Measured, not assumed — which mandates this changes:**

| flow | upfront charge | invoices at Razorpay | effect |
|---|---|---|---|
| RENEW reactivation | plan price (add-on) | 1 paid, names the payment | **newly provable** |
| UPGRADE (Monthly) | prorated (add-on) | expected paid, same shape | **newly provable** |
| SIGNUP, `signup_fee_inr > 0` | fee (add-on) | paid (`inv_TMrSo7lHWNtkQy` observed) | **newly provable** |
| SIGNUP, no fee, no trial | first cycle (no `start_at`) | expected paid | **newly provable** |
| REAUTH | none | **`[]` — verified on `sub_TNBwkbJlhmZ3Ni`** | unchanged |
| DOWNGRADE_MANDATE | none | none expected | unchanged |
| SIGNUP trial, no fee | none (₹5 refunded) | none | unchanged |

So the proof fires **only where money genuinely moved**. That is the whole safety argument
for the change, and T1's tests must pin it rather than trust it.

**The load-bearing consequence.** `_apply_billing_by_purpose` documents the assumption this
breaks: *"Mandate re-arm flows (REAUTH / DOWNGRADE_MANDATE / monthly UPGRADE) never reach
here from `_heal_paid` — their money is a mandate AUTHORIZATION with no keyable payment
id."* Turn the proof on and a **mandate UPGRADE** starts arriving there, falls past the two
`if`s, and hits the bare `activate_and_assign` tail: period extended, plan never switched,
new mandate never adopted, old one never released. **Implementing T1 without T2 converts one
recovery into a new money bug.** That ordering is not negotiable.

**Alternatives rejected.** (a) *Attach a `callback_url` to Razorpay Checkout* — the topology
is frozen precisely because handler + callback together produce two confirm paths for one
payment; it is a bigger change than the recovery it buys, and it does not help the
already-stranded. Kept as an open question, not folded in. (b) *Rely on the webhook* — it is
what we rely on today, it cannot reach a dev bench, and one delivery failure in production
leaves the same silent hole. (c) *Have the healer activate on an authorized mandate with no
payment id* — that is exactly the double-activation C03-5 forbids; the id is the idempotency
key every other seam converges on.

## Architecture / approach

**One new gateway read, one new provider answer.**
`razorpay_client.fetch_subscription_invoices(subscription_id)` wraps
`invoice.all({"subscription_id": ...})` with the module's existing failure posture.
`RazorpayProvider.fetch_mandate_authorization(sub)` returns a `ConfirmOutcome` **only** when
all of these hold, else `None`:

1. the subscription's **earliest** invoice by issue time is `status == "paid"`,
2. it names a `payment_id`,
3. its `amount_paid` equals its `amount` (no partial),
4. the payment itself fetches as `captured` for that same amount.

Earliest-only is the rule that keeps a later CYCLE charge from being credited as the
authorization. More than one invoice at authorization time is not a shape we create; if the
earliest is not paid, the answer is `None` — never "search on for a paid one".

`_reconcile_mandate` already consumes this (*"If a future SDK/route ever names the
authorization payment, this is where it lands"*), so `check_billing_payment_status` →
`_heal_paid` → `_apply_billing_by_purpose` lights up with no change to the healer itself.

**Amount discipline lives in the provider.** `_heal_paid` calls
`resolve_money(amount_minor=int(intent.amount_minor or 0))` — the intent's OWN frozen
amount, not the observed one — so `_amount_is_shape_appropriate` there compares a number to
itself and proves nothing. The provider must therefore verify the gateway amount against the
intent before answering. This is the single most important line in the plan.

**The dispatcher learns every mandate purpose it can now receive.**
`_apply_billing_by_purpose` gains an explicit branch per purpose and, crucially, a
**default that refuses** rather than falls through to `activate_and_assign`. A purpose with
no mandate seam (REAUTH, DOWNGRADE_MANDATE — unprovable today, but the dispatcher must not
depend on that staying true) returns the coded pending-confirm answer instead of activating.

**The double-charge door.** After a lost confirm the intent is still `OPENED` with
`money_state UNKNOWN`, so the pay page re-renders with **Pay enabled** and a second click
mints a second mandate and a second charge. `begin()`'s committed-money guard reads the
LOCAL money axis, so it cannot see gateway money we never recorded. Before offering Pay for
a `mandate`-shaped attempt, the shell must ask once whether this attempt already holds
proven money, and render the settled result instead. In production the webhook usually
closes this window in seconds; on a dev bench it is open forever, and one missed webhook
reopens it in production too.

**Silence is part of the defect.** Today this failed with no Error Log and no alert — the
customer was the monitoring. When the proof finds money the ledger had as `UNKNOWN`, that is
a confirm that went missing: log it and alert once (throttled per subscription, like
`_alert_converge_wedged`).

**The return path (owner-approved, open question 1).** A mandate whose bank step replaces the
page has no way back into our JS, so `handler` is the wrong primitive for it. Razorpay
Checkout offers `callback_url`: the gateway POSTs
`razorpay_payment_id | razorpay_subscription_id | razorpay_signature` to a server route.

The frozen-topology rule — *"handler and callback_url are never attached together"* — is
**kept, and made per-shape rather than global**: a `mandate` attempt gets `callback_url` and
NO handler; an `order` attempt keeps the handler and no callback. One confirm path per
attempt, exactly as the invariant intends, chosen by the shape that actually needs it.

The route already exists: Cashfree mandates return through `/jarvis-checkout/return` with a
single-use nonce (`mint_return_nonce` / `verify_return_signature` / `_process_return`).
Razorpay reuses it — `callback_url = {origin}/jarvis-checkout/return?nonce=<raw>` — with a
Razorpay branch that verifies the signature (authentication IS the HMAC over
`payment_id|subscription_id`), binds it to the intent's FROZEN subscription id, and settles
through `_settle_razorpay_mandate`. The nonce's existing replay semantics carry over
unchanged: a second delivery refetches server truth and returns the same safe answer. The
result page then exits through `workspace_return_url`, which since T5 sends a billing flow to
`/jarvis/billing`.

**This changes SIGNUP too**, because a signup autopay checkout is mandate-shaped. That is
deliberate: leaving signup on the lossy handler while billing gets the reliable path is the
drift this codebase keeps warning about. Signup is the money-critical path, so the flow
review covers it explicitly rather than assuming the shared seam carries it.

**The sweep (owner-approved, open question 2).** A customer who never comes back is still
owed their subscription. The `*/5` reconcile cron gains a bounded pass over mandate-shaped
intents that are LIVE, `money_state UNKNOWN`, older than a settle grace window, and free of
an open incident. It reconciles through `reconcile_signup` — the SAME provider seam, so it is
gateway-agnostic and Cashfree rides along — and applies through `_heal_paid`, so it inherits
every idempotency guarantee the other three paths have. Bounded per run so a backlog cannot
stampede the gateway, and every heal it performs raises T5's alert: a payment that needed the
sweep is a confirm that went missing, and that must be visible.

**Four layers, in the order they fire.** Return path (T6) catches it when the browser
survives; the customer's own status check (T1) when it does not; the sweep (T7) when they
never return; the webhook remains the async backstop it always was. T3 sits across all of
them as the guard that no single one of them can be tricked into a second charge.

**Branch.** Its own branch, cut from the current work (NOT from `develop`): T2 edits
`_apply_billing_by_purpose`, which `2026-08-08-reactivation-arms-autopay` T2 modified today
and which is still uncommitted. It does **not** block the pending round-5 review — the
reactivation feature is correct without it in production, where the webhook backstops.
T3 is the only item worth pulling forward if the owner wants the double-charge door shut in
the same release.

## Task breakdown

| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T2 | `_apply_billing_by_purpose` dispatches **every** mandate purpose explicitly (UPGRADE+MANDATE → `_apply_mandate_upgrade`) and REFUSES an unknown one instead of falling through to `activate_and_assign`; stale docstring corrected | Heavy | Lead (Fable) | — | A mandate UPGRADE arriving at the healer switches the plan, adopts the new mandate and releases the old; REAUTH/DOWNGRADE_MANDATE return pending-confirm and mutate nothing; no purpose reaches the bare activate tail |
| T1 | `fetch_subscription_invoices` + `RazorpayProvider.fetch_mandate_authorization`, amount verified in the provider; the false comment replaced with what is actually true | Heavy | Lead (Fable) | T2 | An add-on mandate answers a verified `ConfirmOutcome` naming the captured payment; a no-add-on mandate still answers `None`; a gateway error answers `None`, never a decline; the ₹5 validation payment can never be the answer |
| T3 | Shut the double-charge door: a `mandate` attempt whose gateway money is already provable renders as settled instead of offering Pay | Heavy | Lead (Fable) | T1 | Re-opening the pay page after a lost confirm shows the settled result and no Pay affordance; the check runs once per open, off-lock, and a gateway failure degrades to today's behaviour rather than blocking payment |
| T6 | The return path: a `mandate` attempt gets `callback_url` (and no handler) through the existing nonce'd `/jarvis-checkout/return`, with a Razorpay branch | Heavy | Lead (Fable) | T1 | A mandate whose bank step replaces the page still confirms; exactly one confirm path is attached per attempt; a replayed return is idempotent; signup autopay still completes end to end |
| T7 | Bounded sweep on the `*/5` cron over stale mandate-shaped LIVE intents with UNKNOWN money, applied through `_heal_paid` | Heavy | Lead (Fable) | T1, T2 | A customer who never returns is activated without touching the app; bounded per run; skips incident-bound subscriptions; provider-agnostic |
| T4 | Signup path: `_kept_authorization_amount` records the right figure for a no-fee, no-trial mandate whose FIRST CYCLE was charged (today it answers 0.0 and would under-record a real payment) | Medium | dev-sonnet | T1 | A charged first cycle records its real amount; a refunded ₹5 validation still records 0.0; trial + fee cases unchanged |
| T5 | Alert once when the proof finds money the ledger had as UNKNOWN (throttled per subscription) | Light | dev-haiku | T1 | Ops sees a lost confirm without a human reading the ledger; repeats inside the window do not flood |

## Edge cases and failure modes (reviewer will verify each one)

1. **A no-add-on mandate stays unprovable.** REAUTH returns `[]` invoices (verified). The
   answer must be `None`, so REAUTH's behaviour is byte-identical to today.
2. **The ₹5 validation payment is never the authorization.** It carries `invoice_id: null`,
   so an invoice-driven lookup cannot reach it. If a gateway change ever attached one, the
   amount check must reject it against a ₹100 intent.
3. **A later CYCLE invoice is never credited as the authorization.** Earliest-by-issue only;
   if the earliest is not paid, answer `None`.
4. **Partial payment.** `amount_paid < amount` is not proof — `None`.
5. **Gateway down / slow / 5xx on `invoice.all`.** `None` (a PENDING, never a decline);
   nothing mutated, webhook stays authoritative. Same posture as `_rzp_fetch_payment`.
6. **The healer races the browser confirm and the webhook.** All three converge on
   `_apply_reactivation_mandate`, whose key is the ADOPTED MANDATE read `for_update`.
   Verify the same holds for `_apply_mandate_upgrade` (its key is the payment id) under a
   healer that can now fire — this is the one seam whose idempotency was never exercised
   from this direction.
7. **Double-submit at the pay page** (T3's case): after a lost confirm, a second Pay must
   not mint a second mandate. With T3, refused before any gateway object.
8. **An amount that agrees with neither the intent nor the plan** → `None` + the T5 alert;
   never activated, never quarantined silently.
9. **Auth boundary** unchanged: the healer is customer-authenticated and subscription-scoped;
   the proof adds no new surface. Stated explicitly because it is the category most often
   assumed.
10. **A mandate proven AFTER the subscription was already restored** (the manual-recovery
    case): adoption check short-circuits, no second period.
11. **Cashfree is untouched.** It already answers `fetch_mandate_authorization`; nothing in
    this plan reads or changes that path.
12. **Currency.** An invoice in a currency other than the intent's is not proof.
13. **Exactly one confirm path per attempt (T6).** A mandate must carry `callback_url` and no
    handler; an order the handler and no callback. Both attached is the failure the frozen
    topology exists to prevent — assert it at the point the options are built, not in prose.
14. **A forged or replayed return POST (T6).** The signature is the authentication; a bad one
    changes nothing and consumes nothing. A replayed nonce refetches server truth and returns
    the same answer (existing `_process_return` semantics) — never a second activation.
15. **A return POST for a superseded attempt (T6)** takes the existing drifted-money path
    (`CONFIRM_MONEY_ON_RETIRED_ATTEMPT`), never a blind apply.
16. **The customer closes the tab at the bank (T6's own miss case).** Nothing posts back;
    T1 lets them self-heal and T7 catches them if they never return. The layers must be
    verified as layers — each one exercised with the ones before it disabled.
17. **Sweep bounds (T7).** A backlog must not stampede the gateway: bounded per run, a grace
    window before an intent is eligible (a checkout in progress is not a lost one), and
    subscriptions with an open incident skipped entirely.
18. **The sweep races the customer (T7).** The cron and a browser confirm can fire at the
    same moment; both go through `_heal_paid` and converge on the adopted-mandate key.
19. **Signup autopay is not collateral damage (T6).** A signup mandate takes the new return
    path too; its activation, trial window and coupon count must be unchanged.

## Test plan

- **Unit (admin, `test_site` only):** one test per edge case above. Specifically: the
  add-on/no-add-on split (1), earliest-invoice rule (3), partial (4), gateway failure (5),
  amount mismatch (8), currency (12); the dispatcher's per-purpose routing and its refusing
  default (T2); `_apply_mandate_upgrade` idempotency driven from `_heal_paid` (6);
  `_kept_authorization_amount` for the four signup shapes (T4).
- **Regression:** the full mandate/reconcile/confirm set —
  `test_mandate_reconciliation`, `test_checkout_confirm`, `test_billing_checkout`,
  `test_trial_autopay`, `test_signup`, `test_reactivation_apply`, `test_webhook`,
  `test_intent_ledger`, plus `api.test_tenant_endpoints` and `api.test_account_endpoints`.
- **Unit (T6/T7):** exactly-one-confirm-path assertion at option-build time; the Razorpay
  return branch (good signature, forged signature, replayed nonce, superseded attempt);
  sweep eligibility (grace window, incident skip, bound per run) and its convergence with a
  concurrent confirm.
- **Flow review (executed, not read), each layer with the ones before it disabled:**
  1. **Return path (T6):** on a lapsed Monthly Razorpay fixture, pay the mandate and let the
     bank step replace the page — access is restored with `autorenew = 1` and no clicks.
  2. **Self-heal (T1/T3):** repeat with the return suppressed — re-opening the pay page shows
     settled rather than Pay, and the billing page's status check restores access.
  3. **Sweep (T7):** repeat, never return to the app, and assert the cron activates them.
  4. **Signup autopay (T6 regression):** a full signup through the new return path.
  5. **Break attempt:** click Pay twice across a lost confirm and assert exactly ONE mandate
     exists at the gateway and one period was granted.

## Build log — all seven tasks BUILT 2026-08-08

- **T2** dispatcher: exhaustive `(purpose, shape)` table; `UnappliableFlow` instead of a
  fall-through to the activation tail. Two hazards found while building: `_heal_paid` marked
  the intent APPLIED *unconditionally* (entitlement nobody granted), and
  `_apply_mandate_upgrade` had no self-guard — the healer is a third caller that cannot check
  adoption first, and a re-run nulls the period anchors then returns early from
  `activate_and_assign`, leaving an Active subscription with no period. Both fixed.
- **T1** `fetch_subscription_invoices` + `RazorpayProvider.fetch_mandate_authorization`.
  **Verified against the live incident**: `sub_TNDDkv0gNwpVwF` → verified,
  `pay_TNDE643QZc76yl`, ₹100; the no-add-on REAUTH mandate → `None`; a wrong expected amount
  → `None`. The amount check had nothing to compare against, so `_reconcile_shim` now carries
  `expected_amount_minor` + `currency` from the intent.
- **T3** door: `prove_unrecorded_mandate_money` moves the money axis so the EXISTING guards
  (`handle_open`'s F6, the ledger's committed-money supersede refusal) fire by themselves. No
  third guard added.
- **T6** return path: mandate → `callback_url` + `redirect`, no handler; order → handler, no
  callback. "Never both" preserved as a per-shape choice. Razorpay's return nonce ROTATES per
  open (Cashfree's is baked into the gateway object and still mints once). Razorpay's absent
  signature fails CLOSED — the opposite of Cashfree, whose API signs nothing by contract.
- **T7** sweep on the `*/5` cron. A test caught a real bug: frappe's `["<", x]` filter matches
  NULL, so the cutoff alone dragged in every never-opened checkout and would have spent the
  bounded budget on attempts that never reached a gateway. Fixed with `["opened_at", "is",
  "set"]`.
- **T4** `_kept_authorization_amount` third shape (no fee, no trial → the first cycle IS
  charged), gated on the amount covering the plan price so a nominal charge can never be
  recorded as a cycle.
- **T5** throttled ops alert: proving a payment is a recovery, not an all-clear.

Admin suites green (24 modules). Frontend: vitest 920, node 615 + opener 14. ruff clean.
NOT reviewed, NOT committed. Flow review NOT run — it is the only way the T6 topology change
and the signup redirect get exercised for real.

## Decisions taken (owner, 2026-08-08)

1. **The topology changes.** Approved — T6. `handler`-only was the wrong primitive for a
   redirect-capable mandate. The frozen "never both together" invariant is preserved as a
   per-shape choice rather than abandoned. T3 is therefore **not** a stopgap that T6 retires:
   it stays as the guard that no layer can be tricked into a second charge.
2. **The proof runs on a schedule.** Approved — T7, on the existing `*/5` reconcile cron.
3. **T4 proceeds on the reasoned premise** (no fixture available): `providers/razorpay.py`
   omits `start_at` for a no-trial plan so Razorpay charges the first cycle at authorization.
   **Stated as an assumption, not an observation** — T4's test asserts our behaviour given
   that invoice shape, and the flow review is where the premise itself gets confirmed. If it
   turns out Razorpay raises no such invoice, T4 is a no-op rather than a wrong figure.

## Definition of done

- All tasks meet acceptance criteria
- Code review VERDICT: GREEN
- Flow review VERDICT: GREEN
- Committed only after both greens; PR raised only after flow review passed on the final state
