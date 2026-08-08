# Plan: Reactivate from expired — unwedge the ledger, then let the customer renew, upgrade or downgrade
STATUS: APPROVED
Date: 2026-08-07
Owner: Fable (team leader)
Supersedes: 2026-08-07-billing-renew-dead-end (RED at review round 1)

## Goal

A customer whose plan has lapsed can get back to paying, on ANY plan: renew the same one,
move up, or move down — from the expired state itself, without support. Today none of the
three reaches a payment gateway.

## Context

**The root cause, proven.** Applying a payment stamps `apply_state=APPLIED` and `applied_at`
(`intent_ledger.py:763`) but never touches `checkout_state`. The enum has no completed state:
`CREATING / READY / OPENED / ABANDONED / EXPIRED / SUPERSEDED`, with
`LIVE_CHECKOUT_STATES = (CREATING, READY, OPENED)`. Only two production writers leave the live
set, and neither can fire on a paid attempt — `:773` SUPERSEDED is refused while money is
committed, and `:1226` EXPIRED filters `money_state not in COMMITTED_MONEY_STATES` (twice).

So a finished signup mandate stays `OPENED` + `AUTHORIZED` forever. `current_intent()` keeps
returning it, `ledger.begin()` sees a live attempt holding committed money, and every later
renew / upgrade / reauthorize dies on `CommittedMoneyError`. No order, no `pay_page_token`, no
redirect. **3 of 3 local subscriptions are wedged this way — including two healthy Active
ones** — so this is not about being cancelled; the cancelled state merely made the Renew button
visible enough to notice.

**Why this changes the previous plan's verdict.** Review round 1's BLOCKER was that the new
recovery endpoints refuse a `purpose=SIGNUP` intent. Once the wedge is fixed that intent is no
longer *current*, so `current_intent()` returns the genuine billing intent and those endpoints
become correct as written. The BLOCKER dissolves rather than needing the healer widened — which
is why it is fixed here rather than worked around.

**The second half: plan changes from a lapsed state.** `may_upgrade()` ends in
`sub.status == "Active"`; `_validate_upgrade` additionally refuses `_days_remaining(sub) <= 0`
with `NeedsRenew`; `_validate_downgrade` refuses any `status != "Active"` with "renew before
changing plans". Those guards are right for what they guard: proration credits unused days
(there are none), and a Monthly downgrade schedules at the next cycle boundary (there isn't
one). The mistake is treating "cannot prorate" as "cannot change plan".

**The approach.** When the period has lapsed there is nothing to prorate and nothing to
schedule, so a plan change collapses into one operation: **reactivate onto the chosen plan at
that plan's full price**. Renew already prices off `scheduled_plan or sub.plan`
(`api/tenant.py:3500`), so the seam exists. Upgrade and downgrade stop being separate verbs in
this state — they are the same payment, differing only in which plan the customer picked.

**Alternatives rejected.** (a) Widening `may_upgrade` to non-Active and reusing the prorated
`start_upgrade`: `_proration` would credit a period that has already run out, and the
mandate-migration path assumes a live mandate to migrate. (b) Making the customer renew first
and change plan second: two payments and a wrong intermediate charge to move up. (c) Excluding
`apply_state == APPLIED` inside `current_intent()` instead of a real state: leaves the row
lying about itself, and every other reader keeps seeing a live checkout.

## Architecture / approach

**1. Settled money stops blocking the next attempt.** AMENDED 2026-08-08, owner-approved, after
the first design was built and failed.

*Rejected (built, then reverted):* adding a terminal `CHECKOUT_COMPLETED` state set by
`mark_applied`. It broke confirm REPLAY — `test_capture_after_callback_race_converges_once` and
`test_replayed_return_is_idempotent` — because three money-path guards
(`_load_current_intent`, `_revalidate_current`, and the `/return` twin) each require the bound
intent to still be current AND `READY`/`OPENED`. A replaying customer got "under review"
instead of "confirmed", a worse failure than the wedge. That answers the plan's own open
question 2: yes, something depends on an applied intent staying current.

*Adopted:* narrow the supersede guard in `begin()` instead. Committed money blocks a supersede
only UNTIL it becomes entitlement:

    _blocks_supersede(i) := i.money_state in COMMITTED_MONEY_STATES and i.apply_state != APPLIED

Two call sites, initiation path only; every confirm/replay/return path is untouched, so those
tests pass unchanged. No doctype change and NO PATCH — wedged rows unwedge themselves the first
time a new attempt is initiated, and the settled attempt is superseded (money and apply axes
intact, still resolvable by provider id) rather than rewritten.

*Known trade-off:* the retired row keeps `checkout_state=OPENED` until superseded, so the audit
trail stays imprecise and `get_billing_payment_state` could still offer to resume a finished
checkout. The completion state remains the better end state and is deferred to a follow-up that
does the confirm-path work it requires.

**2. Reactivation as its own contract, not a widened upgrade.** `get_account_summary` gains
`reactivation_plans` (every active plan in the subscription's own billing cycle, cheaper AND
dearer, current plan included) and `can_reactivate`. `upgrade_plans` / `downgrade_plans` keep
their exact present meaning — the Active-only, prorated / next-cycle-scheduled flows — so the
delicate proration and mandate-migration code is not touched at all. Two different operations
stay two different fields; the SPA renders one grid or the other, never both.

`renew(target_plan=None)` accepts a plan: when the caller is in the reactivation cohort and
names a valid plan in the same billing cycle, the order is priced at the target's FULL price and
the target is recorded so `confirm_payment` activates on it (the existing
`_apply_scheduled_plan_change` seam). `target_plan` omitted keeps today's behaviour exactly.

**Reactivation cohort:** `status in ("Expired", "Cancelled")`, or `status == "Past Due"` with
`_days_remaining(sub) <= 0`. Active-with-time-left is deliberately excluded — that customer
gets the prorated path, unchanged.

**3. The recovery UI, reworked.** The round-1 finding list is carried in full (below). The
notice machinery survives; the parts the reviewer broke apart are rebuilt: resume gets its own
labelled action instead of firing off CHECK, transport failures render a bench code rather than
`Failed to fetch`, "settled" is distinguished from "unreadable", the rendered code is reconciled
against the freshly-read account, and INITIATE retries the action that produced it.

**Landing order.** PR A = the already-GREEN autorenew + entitlement work (previous plan's T3/T4,
admin branch `fix/terminal-cancel-autorenew`), reviewed and landed on its own — it is independent
and should not wait. PR B = T1 (ledger). PR C = T2+T3 (reactivation). PR D = T4+T5.

## Task breakdown

| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T1 | DONE 2026-08-08. `_blocks_supersede` in `intent_ledger.begin()`: committed money blocks a supersede only until `apply_state == APPLIED`. No doctype change, no patch (see amended architecture 1) | Heavy | Lead (Fable) | — | MET: an applied attempt no longer blocks and is superseded with money/apply intact and still resolvable by provider id; committed-but-unapplied money still blocks; confirm REPLAY untouched (`test_checkout_confirm` 50/50); ledger 52/52; 45 billing modules green; proven end to end on `v4p648rp8g` — renew minted gen-2 RENEW/READY and the browser reached `/jarvis-checkout` |
| T2 | DONE 2026-08-08 (server side). `can_reactivate` / `reactivation_plans` / `reactivation_target` in `get_account_summary`; `renew(target_plan=None)` staging the target on the `scheduled_plan` seam; typed refusals; tenant passthrough in `admin_client.renew` + `onboarding.renew` | Heavy | Lead (Fable) | T1 | MET: lapsed cohort offers both directions (cross-cycle + inactive + zero-price excluded); dearer and cheaper each charge full price and stage the swap; same-plan stages nothing; Active refused with `NotLapsed`; a staged target is not reported as a scheduled downgrade; `_validate_upgrade`/`_validate_downgrade` untouched. 59/59 account + 96/96 tenant endpoints. Live on `v4p648rp8g`: Cancelled → `can_reactivate: true`, offers Starter ₹100 and Growth ₹500 |
| T3 | SPA: reactivation plan grid + "Renew on this plan"; plus round-1 findings 2,3,4,5,6,11,12 on the notice machinery | Medium | dev-sonnet | T2 | Expired state shows selectable plans with the price each will charge; CHECK never navigates (resume is its own labelled action); transport failure renders a bench code; settled vs unreadable distinguished; notice reconciled against the re-read account; INITIATE retries its originating flow; stale notice/error cleared on a new action |
| T4 | `@rate_limit` on `api/account.check_billing_payment_status` mirroring the signup twin (240/hr), surface `PAYMENT_CHECK_RATE_LIMITED` on the bench; request-shape tests in `test_admin_client.py` (findings 7, 9) | Light | dev-haiku | T2 | Limiter present and asserted; a 429 renders as "wait and retry", never as a decline; both `_m(...)` paths asserted so a typo fails the suite |
| T5 | DONE + verified. Lockfiles reverted, `test:node` glob fixed (really runs 615 now), admin tests stop leaking rows, triage written | Light | dev-haiku | — | MET; finding 14 closed: the 10 failures were `test_jarvis` staleness, and after `bench --site test_jarvis migrate` the suite is 81/81 |

**Lead integration fix, 2026-08-08 (T3/T4 seam).** T4 changed the tenant healer to answer coded
refusals as a facade envelope under a deliberate 4xx (mirroring the signup twin) AFTER T3 was
briefed, and T3 discarded `checkBillingPayment()`'s result entirely. Net effect: a rate limit
either vanished or rendered "We could not reach the payment service" about a service that WAS
reached — leaving finding 7 unmet in the UI even though its server half shipped. Fixed by making
`api.checkBillingPayment` a RAW call and decoding it with `paymentCodec.decode` in
`doCheckStatus`, exactly as the signup pair does: a coded refusal renders its own code and skips
the passive re-read; only a genuinely unreadable/offline answer falls back to the transport row.
Three tests pin it. Neither subagent could have seen this — it existed only between them.

Round-1 finding 8 (T1 built in `account.py`, not `onboarding.py`) is hereby **approved as
built** — `account.py` is the documented home for `/jarvis/billing` wrappers and the auth gate
is correct there. This plan records it rather than moving the code.

## Edge cases and failure modes (reviewer will verify each one)

1. **Premature completion.** An intent whose money is committed but `apply_state` is not
   `APPLIED` (PENDING / REVIEW_REQUIRED / an open incident) must stay live. Only
   money-became-entitlement closes a checkout.
2. **Concurrent apply and supersede.** Apply landing while another initiation is superseding the
   same attempt must not lose either write or leave a row both COMPLETED and SUPERSEDED.
3. **Patch selectivity.** Runs against live+APPLIED, live+not-APPLIED, already-terminal,
   incident-open, and `apply_state` NULL rows. Closes only the first class; idempotent on rerun;
   never alters `money_state` or `apply_state`.
4. **Mandate afterlife.** After completion, a later `subscription.charged` for the same mandate
   must still resolve its subscription and renew the period. Nothing may key on the intent being
   live.
5. **Confirm replay.** Re-confirming a payment id whose intent is now COMPLETED stays idempotent:
   no new intent, no second activation, no error to the customer.
6. **Invalid target_plan.** Missing, empty, non-string (dict/list — the type-confusion vector
   `_require_plan_name` exists for), unknown, inactive, or a different billing cycle: typed
   refusal, no order minted.
7. **Zero-price target.** Reactivating onto a free/0 INR plan builds an order the gateway
   rejects — refuse with the existing `UpgradeRequired` code instead.
8. **Cheaper target charges full price.** Reactivating downward must charge the cheaper plan's
   full price with NO credit for the lapsed period, and must not schedule anything.
9. **Live mandate present.** A reactivation while `has_live_mandate` is true must not stack a
   second mandate (the exact failure `paymentCodes.js` documents at jarvis#705).
10. **Double-submit.** Two fast reactivate clicks, and a direct repeat POST, produce at most one
    gateway object — the ledger's idempotency/reuse path, not the SPA's `busy` ref.
11. **Auth boundary.** A workspace member is refused before any admin round trip; reactivation
    controls absent for them.
12. **Active-with-time-left is untouched.** Still gets prorated upgrade / scheduled downgrade,
    empty `reactivation_plans`, and no "renew on this plan" affordance.
13. **Admin unreachable / slow / 5xx / rate-limited** during reactivate or check: honest coded
    copy, no silent no-op, nothing mutated locally.
14. **Partial failure.** Order minted but activation fails: the customer must not be left paying
    with no plan change; the intent-ledger saga owns the money, and the flow must be re-runnable.
15. **Contradictory rendering.** A success code must never render above a revoked subscription
    (round-1 finding 5).

Not applicable: no new free-text input is introduced — `target_plan` is a name validated against
the plan table, which item 6 covers.

## Test plan

**Unit — admin (`test_site` only, never the live plane, never fleet/pool suites)**
- Ledger: completion transition (1, 2), patch selectivity + idempotency (3), mandate afterlife
  (4), confirm replay (5). Existing `test_intent_ledger` / `test_billing_checkout` /
  `test_checkout_transport` suites must stay green — they encode the current state machine.
- Reactivation: cohort membership (12), each refusal in 6/7, full-price up and down (8), live
  mandate (9), double-submit (10).

**Unit — tenant (`test_jarvis`)**
- `test_admin_client.py`: request shapes for every new/changed call (finding 9).
- `test_account.py`: auth boundary (11), coded surfacing per admin failure (13).
- `frontend`: `BillingPage.spec.js` for the reactivation grid and findings 2,3,4,5,6,11,12.
  Run the FULL vitest and node suites — round 1 shipped a red CI because only one directory ran.

**Flow review (executed, not read)** on `jarvis.local` against subscription `v4p648rp8g`:
1. Before T1: reproduce the wedge (renew mints nothing). After T1: renew reaches a pay page.
2. Reactivate onto a DEARER plan → one order at full price, activates on it.
3. Reactivate onto a CHEAPER plan → same, no credit, no schedule.
4. Double-click reactivate, and a direct repeat POST → at most one gateway object; assert the
   `Jarvis Payment Intent` count.
5. Check payment status → must NOT navigate to a pay page (round-1 finding 2).
6. Admin unreachable → coded copy, not `Failed to fetch`.
7. Member session → controls absent, endpoint refused.
8. Restore to Active and confirm the prorated upgrade/downgrade paths are unchanged.

## Open questions

1. ~~Should an OPERATOR cancel be self-serve reactivatable?~~ **ANSWERED by the owner
   2026-08-08: yes.** `Cancelled` stays in the reactivation cohort regardless of who cancelled,
   so an operator-cancelled customer can buy a new period without support. No code change — this
   is what `_LAPSED_STATUSES` already does; recorded here so it reads as a decision rather than
   an accident. Consequence to keep in mind: the console's Cancel is documented "permanent and
   ends billing", and that copy is now wrong — it ends the CURRENT period, it does not bar a
   future purchase. A follow-up should reword it.
2. **Does anything depend on an applied intent remaining current?** I found no reader that does
   (`subscription.charged` and confirm REPLAY key on provider ids, not `checkout_state`), but a
   guard this central deserves the reviewer proving it rather than my absence of evidence.
3. **Production's ledger is unseen.** The wedge is proven on local (3/3). Confirm the same shape
   on `frappe-claw-test` before relying on it there.

4. **The offer and the acceptance resolve the subscription differently** (noticed 2026-08-08
   while building T2; NOT introduced by it). `get_account_summary` reads
   `_current_subscription`, which PREFERS a serviceable row (`Active / Past Due / Pending *`)
   and only falls back to most-recent-overall; `renew()` always takes most-recent-overall. For a
   customer with exactly one subscription — every case tested, and the production case — they
   agree. They can diverge for a customer holding several rows of mixed status (e.g. an older
   Active beside a newer Cancelled): the pane would offer no reactivation while a direct
   `renew(target_plan=...)` would act on the other row. The refusal inside `renew` is still
   self-consistent (it checks `can_reactivate` on the row it resolved), so nothing mis-charges,
   but this violates the "offer and accept must agree" contract `may_upgrade` sets. Left alone
   deliberately: changing subscription resolution is high blast radius and outside T2's approved
   scope. The reviewer should probe it and decide whether it needs its own plan.

## Definition of done

- All tasks meet acceptance criteria
- Code review VERDICT: GREEN
- Flow review VERDICT: GREEN
- Committed only after both greens; PR raised only after flow review passed on the final state
