# Plan: Billing renew dead end — surface the server's recovery, stop the autorenew lie
STATUS: APPROVED
Date: 2026-08-07
Owner: Fable (team leader)

## Goal

A customer whose subscription is stuck (cancelled, or holding an unconfirmed payment) can
always see what is wrong and always has a next action. Renew never again spins and silently
does nothing, and the billing pane never claims "Auto-renew on" for a mandate that was
released.

## Context

Reported on production (`frappe-claw-test`), reproduced field-for-field on `jarvis.local`
(subscription `v4p648rp8g`).

**What happens today.** `renew()` passes every one of its own guards (verified on the repro
row: no open incident, mandate guard silent, price ₹100 > 0), reaches
`create_billing_checkout`, and `ledger.begin()` refuses to supersede an attempt that already
holds committed money (`CommittedMoneyError`). Admin answers, correctly:

    200 {contract_version: 2, code: "PAYMENT_CONFIRMATION_PENDING",
         recovery: "confirm_payment", payment_provider: "razorpay"}

`BillingPage.vue:settleWithRedirect` handles only `PAYMENT_PAGE_REDIRECT` and
`CLIENT_UPGRADE_REQUIRED`; every other code falls to a bare `await loadAccount()`
(`BillingPage.vue:632-634`). The instruction is discarded. The customer gets a spinner and
silence — permanently, because the stuck intent never clears itself.

**The server side needs no new semantics.** Both halves of the recovery already exist and are
whitelisted: `api/account.py:1032 get_billing_payment_state()` (passive; re-echoes a live
READY/OPENED attempt's pay-page token so the customer returns to the SAME pay page with no new
provider object) and `api/account.py:1051 check_billing_payment_status()` (active healer;
reconciles the frozen handle against provider truth, idempotent with the browser confirm and
the webhook). The repro's intent is `checkout_state=OPENED, money_state=AUTHORIZED`, i.e.
exactly the case the passive endpoint was built to resume. The tenant simply never wired
either one: `admin_client` has `check_signup_payment_status` only, and there is no billing
equivalent anywhere in the bench.

The SPA also already owns the vocabulary: `CODES.PAYMENT_CONFIRMATION_PENDING` exists,
`ADMIN_CODES` includes it, and `paymentCodes.js:177` carries fitting copy with
`actions: [ACTIONS.CHECK, ACTIONS.INITIATE]` ("check the status before starting another
payment"). Onboarding routes this code properly. Only BillingPage does not.

**Second defect, independent of the first.** The three terminal-cancel paths —
`webhook.py:482 _subscription_cancelled`, `console.py:707 cancel_subscription`,
`incidents.py:680` — set `status="Cancelled"` and release the mandate remotely but never clear
`autorenew`. Since `has_live_mandate = any_mandate_id AND autorenew`, the row keeps claiming a
live mandate. This is not cosmetic: `_activation.py:188` reactivates with
`"autorenew": 1 if has_live_mandate(sub) else 0`, and the comment directly above it documents
the exact resulting lockout — autopay claimed on, nothing able to charge, `_may_renew`
refusing a manual renew, "locking the customer out until they had expired a SECOND time".
That guard only works if `autorenew` is honestly cleared when the mandate is released.
`subscription.halted` (`webhook.py:431`) already does this correctly; the terminal paths are
the outliers.

**Third defect.** `entitlement.py:112-115`: for `phase == "expired"` the notice hardcodes
`left = 0` and dates itself from `expired_at or period_end`. A mid-period terminal cancel has
no `expired_at`, so it renders "(access ended 07-09-2026)" — past tense about a date 31 days
away — while `get_account_summary` reports `days_remaining: 31`. Two surfaces, two answers.

**Alternatives rejected.** (a) Making `renew()` supersede the stuck intent: it would abandon
authorized money and risk a double charge — the ledger's refusal is correct. (b) New tenant
copy for this code: `paymentCodes.js` already has it; a second vocabulary would drift.
(c) Fixing only the status on the production row: leaves the renew dead end live for every
future customer, and the two defects are independent — either alone still strands someone.

## Architecture / approach

**Tenant (`jarvis`) — wire the existing recovery, end to end.**
Mirror the `check_signup_payment_status` chain exactly, one layer at a time:
`admin_client` method → whitelisted `onboarding.py` endpoint (same auth guard and `_surface`
error mapping as its signup sibling) → `api.js` function → BillingPage action. Two endpoints:
passive `billing_payment_state` and active `check_billing_payment`.

**BillingPage routing — fail loud by default.** `settleWithRedirect`'s final branch becomes
the *unknown-code* branch instead of the silent one: any code without a dedicated branch is
rendered through `copyFor(code)` (headline + body + its declared actions), so a code the bench
has never seen still produces an honest message and a button. `PAYMENT_CONFIRMATION_PENDING`
then routes by its existing table entry: **Check status** (calls the active healer) and
**Start a new payment** (re-initiates), in that order — the table's order is deliberate and
stays. Where `billing_payment_state` returns a live pay-page token, reuse the existing
`payPageUrl` + attestation gate and top-level navigate, identical to the
`PAYMENT_PAGE_REDIRECT` branch; a token this bench cannot navigate with still fails closed on
`BENCH_PAY_ORIGIN_UNCONFIGURED`. No new pay path, no gateway SDK.

**Admin (`jarvis_admin_v2`) — restore the invariant.** Clear `autorenew` in the same write
that sets `status="Cancelled"` in all three terminal paths (one `db_set`, so a partial failure
cannot leave the row claiming a live mandate). Add a patch repairing existing rows.

**Patch scope, deliberately narrow:** it clears `autorenew` on rows that are already
`status="Cancelled"` with `autorenew=1`. It does **not** change any `status` — resurrecting a
cancelled subscription is a business decision, never a migration's. Period-end-cancel rows
(`status="Active"` + `cancelled_at` set) are untouched by construction.

**Entitlement copy.** When a revoked subscription's clock has not run out, stop asserting it
has. Prefer `expired_at`; fall back to `period_end` only when that is in the past; otherwise
say access ends on that date, and report the real days remaining rather than a hardcoded 0.

Two repos, two branches off `develop`, two PRs: `fix/billing-renew-recovery` (jarvis) and
`fix/terminal-cancel-autorenew` (jarvis_admin_v2).

## Task breakdown

| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T1 | Tenant server seam: `admin_client.billing_payment_state()` + `check_billing_payment()`, and the two whitelisted `onboarding.py` endpoints wrapping them, with the same auth guard and `_surface` error mapping as the signup pair | Heavy | Lead (Fable) | — | Both endpoints reachable by a jarvis-admin user and refused for a member; admin 4xx/5xx/timeout surface as coded errors, never a bare success; python tests cover auth refusal + each failure shape |
| T2 | SPA: `api.js` functions + `BillingPage.vue` routing — unknown-code branch renders `copyFor(code)` with its declared actions, `PAYMENT_CONFIRMATION_PENDING` wires Check-status then Start-new-payment, live token reuses `payPageUrl` + attestation gate | Medium | dev-sonnet | T1 | Renew on the repro row shows "We have not confirmed this payment." + a working Check status; an unmapped code still renders honest copy and a button; no code path reaches a bare `loadAccount()` without rendering something; `*.spec.js` covers each |
| T3 | Admin: clear `autorenew` in the same write as `status="Cancelled"` in `webhook.py:_subscription_cancelled`, `console.py:cancel_subscription`, `incidents.py` revoke; add patch `v1_27_clear_autorenew_on_cancelled_subs` | Heavy | Lead (Fable) | — | All three paths leave `has_live_mandate` False; patch is idempotent, touches only `Cancelled`+`autorenew=1` rows, changes no `status`; tests per path + patch rerun test |
| T4 | Admin: `entitlement.suspended_reason` / `billing_notice` stop claiming access ended when `period_end` is in the future; report real days remaining | Light | dev-haiku | — | Future `period_end` → no "access ended" wording and `days_remaining` matches `get_account_summary`; past `period_end` and `expired_at` cases keep today's wording; tests in `tests/billing/test_expiry_banners.py` |

## Edge cases and failure modes (reviewer will verify each one)

1. **Double-submit Check / Renew.** Clicking Check or Pay twice quickly must not create a
   second gateway object or a second charge. Required: the ledger's reuse/idempotency path is
   exercised, and the button is disabled while in flight.
2. **Admin unreachable, slow, or 5xx during Check.** Must render an honest coded message and
   leave the row untouched — never a silent no-op (the bug being fixed), never fail open into
   "everything is fine".
3. **Unknown / future code from admin.** A code with no branch and no copy-table entry must
   still produce a generic honest headline plus at least one action. Forward compatibility is
   the whole point of the unknown-code branch.
4. **Auth boundary.** A workspace member (non jarvis-admin) calling either new tenant endpoint
   is refused; the billing pane's actions stay hidden for them. Billing actions are
   admin-only.
5. **Live token but pay origin unconfigured.** `billing_payment_state` returns a token the
   bench cannot navigate with → fail closed on `BENCH_PAY_ORIGIN_UNCONFIGURED`, never open a
   gateway sheet on this origin.
6. **Intent resolves between Check and re-read (race).** The healer applies the payment and
   the page re-reads: the customer must land on Active with no second payment offered.
7. **Concurrent terminal cancels.** A webhook echo and an operator console cancel landing on
   the same subscription must be idempotent — no lost update, `autorenew` ends at 0 either
   way.
8. **Partial-failure integrity.** `status` and `autorenew` are written together; a failure
   must not leave `Cancelled` + `autorenew=1`.
9. **Patch data shapes.** Runs against `autorenew` NULL / 0 / 1, rows with no mandate id,
   period-end-cancel rows (`Active` + `cancelled_at`), and already-repaired rows. Idempotent
   on rerun; never alters `status`.
10. **Entitlement date boundaries.** `period_end` in the past, in the future, exactly now, and
    `expired_at` set — the past cases must keep today's wording verbatim (no regression for
    genuinely expired customers).
11. **Oversized / malformed admin payload.** A response missing `code`, or with a non-string
    code, must not throw in the SPA; it renders the generic branch.

Not applicable: no new user-supplied free-text input is introduced (both new endpoints take no
arguments), so injection/oversize-input categories reduce to item 11's malformed-response case.

## Test plan

**Unit — admin (`jarvis_admin_v2`)**
- `tests/billing/`: each of the three terminal paths leaves `has_live_mandate` False (edge 7,
  8); patch idempotency and row-selectivity (edge 9).
- `tests/billing/test_expiry_banners.py`: future/past/now/`expired_at` wording and
  `days_remaining` agreement with `get_account_summary` (edge 10).
- Run on the admin **test site only** — never the fleet/pool suites against a live plane.

**Unit — tenant (`jarvis`)**
- `tests/test_admin_client.py`: both new methods' request shape and error mapping (edge 2).
- `tests/test_onboarding.py`: auth refusal for a member, coded surfacing per admin failure
  (edges 2, 4).
- `frontend`: `BillingPage.spec.js` — `PAYMENT_CONFIRMATION_PENDING` renders copy + Check;
  unknown code renders generic copy + action; malformed payload does not throw; live token
  navigates; unconfigured origin fails closed (edges 3, 5, 11). Run the **full** vitest and
  `node --test` suites, not the touched directory only.

**Flow review (executed against the running app, not read)**
Against the existing local repro — `jarvis.local`, subscription `v4p648rp8g`, whose intent is
genuinely `OPENED`/`AUTHORIZED`, so no state has to be faked:
1. Renew → expect the pending-confirmation copy and a Check status button (today: silence).
2. Check status → healer runs; assert **no new `Jarvis Payment Intent` row** is created.
3. Double-click Check, and double-click Pay → one gateway object, one charge at most (edge 1).
4. Point the bench at an unreachable admin → honest error, not silence (edge 2).
5. Member (non-admin) session → billing actions absent; direct endpoint call refused (edge 4).
6. Restore the row to Active and confirm the normal renew path is unbroken.

## Open questions

None that change this plan's design. Three that are genuinely separate:

1. **Policy, deliberately out of scope:** should a *mid-period* terminal cancel preserve chat
   entitlement until `period_end` instead of revoking immediately? Today `Cancelled` revokes at
   once (`SUSPENDED_STATUSES`), which is intentional for a genuine terminal cancel. Changing it
   is a billing-policy decision, needs its own plan, and does not affect T1–T4.
2. **What actually cancelled the production subscription** (10 hours after creation, mid-period,
   with autorenew still on) — needs the production `Jarvis Subscription` version history and the
   webhook log. All four tasks are correct regardless of the answer, but the answer may reveal a
   fourth defect.
3. **Does production's plan catalogue hold a plan dearer than Dev Payment Testing (₹10)?**
   Determines whether repairing that row's status restores the upgrade list. Local proof used a
   purpose-added `Growth` plan as a control.

Separately, repairing the production row itself is an operational decision for the owner, not
part of this plan: the patch clears `autorenew` only and will not resurrect a cancelled
subscription's status.

## Definition of done

- All tasks meet acceptance criteria
- Code review VERDICT: GREEN
- Flow review VERDICT: GREEN
- Committed only after both greens; PR raised only after flow review passed on the final state
