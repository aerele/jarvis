# Plan: Renewing a lapsed plan arms auto-renewal in the same payment
STATUS: APPROVED
Date: 2026-08-08
Owner: Fable (team leader)

## Goal

A customer who pays to come back is subscribed — not asked for a second payment to
stay that way. One payment restores access AND arms auto-renewal, wherever the
gateway and billing cycle allow it.

## Context

**Reproduced by the owner, 2026-08-08 12:56, on `jarvis.local`.** Paid ₹100 to renew an
Expired Starter plan → "Payment confirmed" → Active → and the settings pane immediately said
*"Auto-renewal is off. Set it up before 2026-09-08 to stay subscribed"*, needing a SECOND ₹5
mandate authorization. Ledger: `sdfkq0qt9r` (RENEW, PAID) then a separate REAUTH
(`pay_TNBx4j3n4RzuX2`). Two payments for one intention.

**Why.** `renew()` passes `force_order=True` and `shape=SHAPE_ORDER`, and its comment states
renew "must NEVER mint a mandate" because `create_payment_intent` would route a Monthly plan to
`_create_mandate` and overwrite a live `cashfree_subscription_id` + `autorenew`. That hazard is
real for a RUNNING subscription — and **cannot exist for the lapsed cohort**: `can_reactivate`
requires Expired/Cancelled, or Past Due with no days AND no live mandate, so `has_live_mandate`
is false there by construction. The guard is defending against a state this cohort cannot be in.

**The shape already exists.** Signup and mandate-migration upgrades both mint a mandate with the
amount due as an UPFRONT ADDON and defer the first full cycle via `start_at`. Reactivation is
the same shape: pay the plan price now, first automatic cycle at period end. Nothing new is
invented — `renew` for this cohort becomes the flow signup already is.

**Alternatives rejected.** (a) Auto-firing the existing REAUTH after a successful renew: two
gateway objects, two customer-visible charges, and a failure between them leaves exactly the
split state we are fixing. (b) Offering a "renew + autopay" checkbox that runs both: same two
payments, now with a choice the customer should not have to make. (c) Changing REAUTH's ₹5 to
₹0: does not remove the second step, and a zero-amount authorization is not universally
supported.

## Architecture / approach

**Cohort and shape.** For a reactivation (`can_reactivate(sub)` true) where the target plan is
`_is_autopay` (paid **Monthly**) AND the provider supports a mandate for this flow, `renew`
initiates `SHAPE_MANDATE` with the plan price as an upfront addon and `start_at` = the new
period end. Everything else — Annual, Cashfree, a running subscription — keeps today's
`SHAPE_ORDER` behaviour untouched.

**The matrix is the enforcement, not an `if`.** `SUPPORT_MATRIX[(FLOW_RENEW, "razorpay")]` gains
`SHAPE_MANDATE` (becoming `{ORDER, MANDATE}`, exactly as `FLOW_UPGRADE` already is);
`(FLOW_RENEW, "cashfree")` stays `{ORDER}`. `resolve_shape_or_reject` then refuses an
impossible combination on its own, and no caller can bypass it.

**Per-provider / per-cycle behaviour, stated so it cannot differ silently:**

| cycle | Razorpay | Cashfree |
|---|---|---|
| Monthly (autopay) | mandate + upfront addon → autopay ARMED in one payment | order; autopay stays off (no mid-life mandate re-arm exists) |
| Annual | order; no mandate exists for annual | order |

Where autopay cannot be armed, the UI must say so BEFORE payment rather than after — the
current defect is not the second step itself, it is being told about it only afterwards.

**Activation must still start a PAID period.** A mandate authorizes rather than pays, and
`_activation` treats mandate-auth differently (`period_override`, `trial_days`,
`last_paid_amount_inr` deliberately left to the cycle webhook). Reactivation carries
`trial_days = 0` by construction, so it must produce a full paid period starting now — never a
trial window — and `autorenew` must end 1.

**The reactivate_plan swap must keep working.** The frozen `intent.target_plan` is applied on
all three seams (browser confirm, `webhook._activate`, `_apply_billing_by_purpose`). A mandate
reactivation changes the SHAPE, not that contract: paying for Growth must still land on Growth,
now through the mandate-auth branch as well.

**Branch.** Its own branch off the current work, not folded into it:
`2026-08-07-expired-plan-reactivation` is RED at round 3 with MAJOR 7 partial, 7 MINORs open and
four flow scenarios NOT RUN. Landing a payment-shape change on top of an unreviewed RED branch
would make one review answer for two designs.

## Task breakdown

| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T1 | Matrix + shape selection: add `SHAPE_MANDATE` to `(FLOW_RENEW, razorpay)`; `renew()` picks mandate for a reactivation onto an `_is_autopay` plan and keeps `force_order` for every other case | Heavy | Lead (Fable) | — | Monthly Razorpay reactivation mints a mandate with the plan price as an upfront addon and `start_at` = period end; Annual, Cashfree, and any non-lapsed renew are byte-identical to today; `resolve_shape_or_reject` refuses an unsupported combination |
| T2 | Activation: a mandate-auth reactivation starts a full PAID period now, `autorenew = 1`, and still applies the frozen `intent.target_plan` on all three seams | Heavy | Lead (Fable) | T1 | Period starts now (never a trial), `autorenew` 1, plan == the intent's target on browser-confirm, webhook and healer seams |
| T3 | Disclosure: the confirm dialog and plan card say what the payment does — "restores access and turns auto-renewal back on" where it will, and states plainly that autopay cannot be armed where it cannot (Cashfree / Annual) | Medium | dev-sonnet | T1 | Copy differs by what will actually happen; the Cashfree/Annual customer is told BEFORE paying, not after; specs cover both |
| T4 | Remove the now-dead second step for the armed cohort: the "Auto-renewal is off" banner must not appear after a reactivation that armed it | Light | dev-haiku | T2 | Banner absent when `autorenew` is 1; still present for the cohorts that genuinely need it |
| T5 | Return path: a BILLING checkout exits to the customer's billing page, not the signup wizard | Heavy | Lead (Fable) | — | A renew/upgrade/reauth/dunning checkout returns to `/jarvis/billing`; a signup still returns to `/jarvis/onboarding`; an unknown/blank site URL still yields "" |

**Amendment 2026-08-08 (owner-requested, after hitting it live).** `checkout/workspace.py`
hardcodes `WORKSPACE_PATH = "/jarvis/onboarding"` for EVERY flow, so exiting the pay page on a
renewal drops the customer in the signup wizard. The wizard maps every lapsed status to
`SIGNUP_TERMINAL` — *"This signup cannot be continued… Renew from your account"*, with a
Contact-support button and no way to renew. A renewing customer is told to renew, on a page
that cannot renew, by the renewal itself. Pre-existing (the order-shaped renew had the same
exit); invisible until now because a COMPLETED renewal returns Active and the wizard bounces
to chat. Not folded into T1/T2: it moves where every checkout returns, signup included.

## Edge cases and failure modes (reviewer will verify each one)

1. **A live mandate must never be overwritten.** The whole reason `force_order` exists. A
   reactivation is only offered when `has_live_mandate` is false; assert it defensively at the
   seam too, and refuse rather than mint a second mandate.
2. **Annual reactivation** takes the order path unchanged — no mandate is minted, no autopay
   claimed.
3. **Cashfree reactivation** takes the order path; the matrix refuses `SHAPE_MANDATE`; the
   customer is told autopay cannot be armed BEFORE paying.
4. **Mandate authorized but never captured** — the customer abandons at the gateway: no period
   granted, no `autorenew`, and the attempt is superseded by the next initiation.
5. **Double-submit** two reactivations: at most one mandate object; the ledger's committed-money
   guard still holds (an AUTHORIZED mandate is committed money).
6. **Reactivating onto a DIFFERENT plan** (the round-3 blocker) still lands on the intent's
   frozen `target_plan` — now via the mandate branch, on all three apply seams.
7. **Trial fields**: a plan carrying `trial_days > 0` must NOT give a lapsed customer a fresh
   trial on reactivation — they are paying to come back.
8. **Webhook wins the race**: the mandate's first `subscription.charged` must not double-extend
   a period the authorization already granted.
9. **Zero/negative price** target: refused before any gateway object (existing
   `UpgradeRequired`).
10. **Auth boundary**: unchanged — reactivation is admin-gated like every billing action.
11. **Gateway down / slow / 5xx** during mandate creation: honest coded copy, nothing mutated
    locally, and no half-armed autopay.

## Test plan

- **Unit (admin, `test_site` only):** shape selection per cycle/provider/cohort (T1); the matrix
  refusal; activation starting a paid period with `autorenew=1` and no trial (edges 2, 3, 7);
  target-plan applied on all three seams under the mandate branch (edge 6); live-mandate refusal
  (edge 1); double-submit (edge 5).
- **Unit (tenant + frontend):** confirm-dialog and card copy per cohort (T3); banner suppression
  (T4). Run the FULL vitest and node suites.
- **Flow review (executed, not read):** on a lapsed Monthly Razorpay fixture — pay once, assert
  access restored AND `autorenew=1` with NO second step offered; pay onto a DIFFERENT plan and
  assert the plan landed; a Cashfree/Annual fixture shows the honest "autopay cannot be armed"
  message before payment. Plus the round-3 scenarios still NOT RUN.

## Open questions

1. **Is the ₹5 authorization charge refunded or applied?** Today's REAUTH takes ₹5; if
   reactivation charges the plan price as an upfront addon instead, confirm with the owner that
   no separate authorization amount should appear at all.
2. **Cashfree lapsed customers** keep a two-step experience because no mid-life mandate re-arm
   exists there. Acceptable, or should Cashfree reactivation be refused/steered differently?
   Does not block T1/T2 for Razorpay.
3. **The Settings pane alignment** the owner reported: element not yet identified. Not a task
   here; needs the owner to point at it.

## Build log

**T1 + T2 built 2026-08-08 (Lead).** Admin only; nothing on the tenant side yet.

- `SUPPORT_MATRIX[(RENEW, razorpay)]` = `{order, mandate}`; Cashfree unchanged.
- `renew()` picks the shape from `arms_autopay` = razorpay AND `can_reactivate` AND
  `_is_autopay(pricing_plan)` AND `not has_live_mandate` (the last one re-asserted at the
  seam, not assumed from the cohort). Mandate branch mints a Razorpay subscription with the
  plan price as an upfront add-on and `start_at` = `_fresh_cycle_epoch(plan)`, derived from
  `_activation._period_end` so the first automatic charge lands exactly when the period this
  payment buys runs out — not a 30-day approximation.
- `razorpay_client.create_subscription` gained `upfront_label` (the invoice line said
  "Prorated upgrade" on a renewal) and now charges `to_paise(upfront_inr)` rather than
  truncating fractional rupees below the amount the intent froze.
- **T2 was NOT optional.** A mandate-shaped RENEW hit `is_mandate_auth and not
  _is_signup_stage(sub)` in `_confirm_payment` and returned SIGNUP_TERMINAL, which `_apply`
  turns into an INCIDENT: money taken, nothing granted. Three seams now apply it —
  `_confirm_payment` (exempted by the frozen `reactivate_plan`, which only the token-bound
  guest confirm passes, so replaying a mandate id at the session endpoint still buys
  nothing), `webhook._reactivation_mandate_backstop`, and `_apply_billing_by_purpose`.
- `_apply_reactivation_mandate` adopts the mandate, applies the frozen target, and activates
  a FULL PAID period (no `period_end` override → never a trial). Idempotency is the ADOPTED
  MANDATE read `for_update` — not the payment id, which the two seams cannot always share.

**T5 built 2026-08-08 (Lead).** `workspace.return_path_for(intent)` picks the route from the
intent's PURPOSE: `BILLING_FLOWS` → `/jarvis/billing`, everything else (signup, and any
purpose this module has not heard of) keeps `/jarvis/onboarding`. Failing towards the surface
that already existed means a future flow is never silently routed somewhere it was not
designed for. The billing page now also runs the healer ONCE when it is returned to with
`?pay=done|pending` — landing on a plain read right after paying can show the customer the
very state they paid to leave — and strips the parameter so a reload does not re-check a
payment that finished long ago. `?pay=failed` has nothing to converge on.

Open question 1 is answered by the build: the armed cohort has no separate ₹5 step at all,
because the authorization charge IS the plan price. `reauthorize_autopay` stays for the
Active-sub-whose-mandate-died case, where nothing is owed.

Still to do: T3 (disclosure copy, dev-sonnet), T4 (banner suppression, dev-haiku), then the
flow scenarios and review.

## Definition of done

- All tasks meet acceptance criteria
- Code review VERDICT: GREEN
- Flow review VERDICT: GREEN
- Committed only after both greens; PR raised only after flow review passed on the final state
