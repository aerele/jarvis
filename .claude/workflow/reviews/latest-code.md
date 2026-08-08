# Code review — 2026-08-08-mandate-authorization-proof — round 3
Reviewer: Opus (strict-reviewer)
Date: 2026-08-08
Scope: two repos, uncommitted, re-reviewed cold.
- `apps/jarvis_admin_v2` @ `fix/terminal-cancel-autorenew` — `billing/checkout/{billing,confirm,endpoints,shell,workspace,opener.js}`,
  `billing/providers/razorpay.py`, `billing/{intent_ledger,intent_resolution,signup,webhook,expiry}.py`,
  `api/{_billing_actions,account,tenant}.py`, `patches.txt`, `patches/v1_28_clear_autorenew_on_expired_subs.py`,
  changed/new test modules.
- `apps/jarvis` @ `fix/billing-renew-recovery` — `frontend/src/pages/billing/BillingPage.vue`,
  `frontend/src/onboarding/paymentCodes.js`, `BillingPage.spec.js`.
Plans: `2026-08-08-mandate-authorization-proof` (STATUS: APPROVED), on top of
`2026-08-08-reactivation-arms-autopay` and `2026-08-07-expired-plan-reactivation`. All three APPROVED — no unplanned work found.

Every round-2 finding was re-verified INDEPENDENTLY by execution on `test_site`, not by reading the fix.
Both new findings below were REPRODUCED. Nothing was fixed by me.

## Round-2 fix verification (all 12 checked independently)

| R2 | Claim | Verified |
|---|---|---|
| 1 BLOCKER | `_razorpay_return_signature_ok` runs on every request | **YES — reproduced.** Genuine signed return recorded `pay_GENUINE`; an unsigned replay carrying `pay_ATTACKER` answered `SESSION_INVALID` and the recorded id did not move. The check sits at `confirm.py:1072-1080`, above `already = ...` at `:1082` |
| 2 BLOCKER | `from_grid` captured before the mandate write | **YES — reproduced.** `_billing_actions.py:246` reads `can_reactivate(sub)` before the `set_value` at `:262`. Drove a Past-Due/0-days sub with a stale `scheduled_plan`: paid for the ₹500 plan, landed on the ₹500 plan, `scheduled_plan` cleared |
| 2b | webhook backstop inherits the capture | **YES — checked, not assumed.** `webhook.py:268` gates on `can_reactivate` before calling, and `_apply_reactivation_mandate` re-captures at its own line 246 before any write |
| 3 MAJOR | cycle admitted only when `trial_days == 0` | **YES** (`signup.py:2405-2411`). Trial+fee still proves (set is `(fee,)`); trial+no-fee yields `()` and the provider **fails closed** at `razorpay.py:314-322` (`if not expected: return None`) — genuinely closed, not skipped |
| 4 MAJOR | commit before `_lock_chain` on both sites | **YES** — `endpoints.py:546-553` and `billing.py:243-246` |
| 5 MAJOR | fallback prices off `target_plan` | **PARTIAL** — `_billing_actions.py:283-287` now uses `target_plan or sub.plan`, so the wrong-plan case is gone. The webhook still passes no `paid_amount_inr` though it holds the payment entity → finding 3 |
| 6 MAJOR | `/return` requires capture, rejects refunds | **NO — the predicate keys on the wrong field → finding 2 (reproduced)** |
| 7 MAJOR | edge case 19 covered | **YES — I executed it.** Drove a SIGNUP mandate through `_process_return` with the REAL `_confirm_payment`: status `Active`, period end exactly +14d (trial window), nominal token recorded `0.0`, auth id on its own column |
| 8 MINOR | `_expire` clears autorenew + v1_28 | **YES — reproduced.** Expired/autorenew=0 → `has_live_mandate` False → `arms_autopay` True. No code keys off Expired-with-autorenew (grepped) |
| 9 MINOR | consumed stamp reset on re-issue | **YES** (`confirm.py:911-917`). Tested the R2-9 × R2-1 interaction the invoker flagged: a signed replay after a re-open re-enters the full apply path and converges idempotently (money `AUTHORIZED`, apply `APPLIED`, unchanged) — no double advance |
| 10 MINOR | dead URL rewrite + its spec deleted | **YES** — no `replaceState`/`searchParams` rewrite remains; healer still gated on `SETTLING_OUTCOMES = {done, pending}` (`BillingPage.vue:457-467`) |
| 11 MINOR | `reconcile_signup` gets `_authorization_subject` | **YES** (`signup.py:3192`) |
| 12 MINOR | DECLINED→pending when money committed | **YES** (`confirm.py:1163-1170` + `_money_is_committed`) |

Suite re-run by me: `test_checkout_confirm` 66 tests OK. The two findings below are not caught by it —
they are exactly the shapes it does not construct.

## Findings

| # | Severity | Location | What breaks | Required fix |
|---|----------|----------|-------------|--------------|
| 1 | BLOCKER | `billing/checkout/billing.py:364` (`prove_unrecorded_mandate_money`, `"expected_amounts_minor": (int(intent.amount_minor or 0),)`) | **T3's double-charge door does not shut for a signup-with-a-fee mandate — an acceptance criterion of the plan.** The shim freezes ONLY `amount_minor` (the plan price), but a fee-bearing signup's authorization invoice is the FEE (`expected_auth_minor`). The provider's `amount not in expected` then refuses, the proof answers `None`, the money axis stays `UNKNOWN`, and both guards that depend on it (`handle_open`'s "never re-open a paid attempt" and `ledger.begin`'s committed-money refusal) read an unpaid attempt — so Pay is offered again and a second click mints a second mandate and a second fee charge. **Reproduced on `test_site`:** gateway holding a paid ₹500 fee invoice (`pay_FEE`, captured) → `prove_unrecorded_mandate_money` returned `False`, `money_state` stayed `UNKNOWN`. **Control run in the same script**: an invoice equal to `amount_minor` → `True`, so this is the field, not the fixture. **Reachable on current config** — the live control plane's `Starter` plan is `signup_fee_inr = 2.0, trial_days = 1`, precisely this shape. The plan's own table promises "SIGNUP, `signup_fee_inr > 0` → newly provable", and T3's criterion is "Re-opening the pay page after a lost confirm shows the settled result and no Pay affordance". Neither holds. Note the codebase now carries THREE different expectation sets for the same question — `_authorization_subject` (fee, +cycle when no trial), this shim (`amount_minor` only), and `_amount_is_shape_appropriate` (both + nominal). | Build the shim's expectation set the same way `signup._expected_authorization_amounts` does — admit `expected_auth_minor` as well as `amount_minor` (dropping zeros), so the fee-bearing shape is provable. Better: extract ONE helper both callers use, so the three sets cannot drift again. Add a `prove_unrecorded_mandate_money` test for a fee-bearing signup intent. |
| 2 | MAJOR | `billing/checkout/confirm.py:652-654` (`if refunded > 0 and amount == int(intent.amount_minor or 0)`) | **The refund guard keys on the wrong field, so refunded money still buys a period.** The carve-out is meant to separate "the nominal validation token, refunded by design" from "real money that was given back", but it tests equality against `intent.amount_minor` — and for every signup-with-a-fee shape the authorization amount is `expected_auth_minor`, not `amount_minor`. `_amount_is_shape_appropriate` admits it, so the refund is invisible. **Reproduced on `test_site`:** intent `amount_minor=300000 / expected_auth_minor=50000`, payment `captured, amount=50000, amount_refunded=50000` (fully refunded) → `/return` answered **PAYMENT_CONFIRMED**, `money_state=AUTHORIZED`, `apply_state=APPLIED`, `provider_payment_id=pay_FEE_REFUNDED`. The customer is fully activated on money they got back. The same holds for fee+no-trial, where the authorization is `fee+price` and `amount_minor` is `price`. The invoice proof at `razorpay.py:337-338` rejects **any** refund — so the two authorities on the same question still disagree, which is the exact asymmetry R2-6 was raised for. `TestReturnMoneyDiscipline` misses it because its fixture leaves `expected_auth_minor` unset, so its refund case is the one shape the predicate does catch. | Key the carve-out on what it actually means: `if refunded > 0 and amount > ledger.nominal_auth_minor(intent.get("provider")): return _declined()`. That keeps the nominal token refundable and disqualifies every real-money shape. Add a refund test with `expected_auth_minor != amount_minor`. |
| 3 | MINOR | `billing/webhook.py:286-288` | R2-5 residual. The backstop holds the payment entity (it reads `...payment.entity.id` at `:282`) but passes no `paid_amount_inr`, so `_apply_reactivation_mandate` falls back to `target_plan`'s CURRENT list price. The serious half (recording a different plan's price) is fixed; what remains is a price edited between checkout and webhook being recorded as what the customer paid. | Pass `paid_amount_inr` from the payment entity's `amount`, or `intent.amount_minor / 100`. |
| 4 | MINOR | `billing/signup.py:2409` (`frappe.db.get_value(PLAN_DT, sub.plan, "trial_days")`) | The expectation set is derived from the plan row's LIVE `trial_days`, although the intent froze `trial_days` at claim time (`ClaimContext(trial_days=...)`). An operator editing the plan between checkout and reconcile changes which amounts count as the authorization — editing it to 0 admits a post-trial CYCLE charge as the authorization, which is the very hole R2-3 closed. Same "read live where a frozen value exists" class as R2-5. | Read `intent.trial_days`, consistent with every other frozen term. |

## Edge-case verification

| Plan edge case | Handling site | Test | Verified |
|---|---|---|---|
| 1 no-add-on mandate unprovable | `razorpay.py:296` (`if not invoices`) | `test_a_mandate_that_charged_nothing_upfront_stays_unproven` | YES |
| 2 ₹5 validation never the authorization | invoice-driven lookup | same suite | YES |
| 3 later CYCLE never credited | `razorpay_client.py:264` sort + `razorpay.py:297`; `signup.py:2405` trial gate | `TestATrialSignupCycleIsNotAnAuthorization` | YES — R2-3 closed; empty set fails closed at `razorpay.py:314` |
| 4 partial payment | `razorpay.py:302` | `test_a_part_paid_invoice_is_not_proof` | YES |
| 5 gateway down → PENDING | `confirm.py:610-615` | `test_an_unreadable_gateway_is_a_pending_not_a_failure` | YES |
| 6 healer races confirm + webhook | `_billing_actions.py:398-404` `for_update` | `TestMandateUpgradeIdempotency` | YES |
| 7 double-submit at the pay page | `endpoints.py:546`; `billing.py:243` | `TestSecondAttemptAfterALostConfirm` | **NO for signup-with-fee — finding 1** (reproduced); YES for the equal-amount shape (control run) |
| 8 amount agreeing with neither | `razorpay.py:323`; `confirm.py:657` | amount-mismatch tests | YES for the proof; `/return` still admits a REFUNDED in-shape amount — finding 2 |
| 9 auth boundary unchanged | `account.py:1093` rate limit | endpoint tests | YES |
| 10 proven AFTER already restored | `_billing_actions.py:250-256` adoption short-circuit | `test_the_same_mandate_applied_twice_does_not_extend_the_period` | YES — and re-confirmed via the R2-9 × R2-1 replay probe |
| 11 Cashfree untouched | `confirm.py:1172` default | `test_cashfree_still_mints_once_...` | YES |
| 12 currency | `razorpay.py:306`, `:341`; `confirm.py:655` | `test_a_different_currency_is_not_proof` | YES |
| 13 exactly one confirm path | `opener.js`; `endpoints.py:489-514` | `test_opener.mjs` (14) | YES (code + node suite); browser half NOT RUN |
| 14 forged / replayed return POST | `confirm.py:1072-1080` (now unconditional) | `TestReplayCannotRewriteTheRecordedPayment` | **YES — reproduced by me** |
| 15 return for a superseded attempt | `confirm.py:1098-1110` | `TestConfirmRaceWindows` | YES |
| 16 tab closed at the bank; layers as layers | T1 proof, T3 door, T7 sweep | server-level probes | PARTIAL — T3 layer broken for the fee shape (finding 1); sweep→activation NOT RUN live |
| 17 sweep bounds | `billing.py:776-802` | 4 tests in `TestUnconfirmedMandateSweep` | YES |
| 18 sweep races the customer | adopted-mandate key | `test_a_mandate_already_adopted_...` | YES |
| 19 signup autopay not collateral damage | `confirm.py:1140-1170` | `test_a_signup_mandate_confirms_through_the_return` | **YES — I executed it with the REAL apply seam**: Active, +14d trial window, `0.0` recorded for the nominal token |

## Things that held up

- R2-1's repair is the right shape. Moving the signature above the `already` branch makes authentication
  unconditional for the provider whose branch reads the body, and the Cashfree branch keeps its
  refetch-by-frozen-id posture. Reproduced as sound.
- R2-2's capture-before-write is correct at BOTH sites, and I confirmed the webhook backstop inherits it
  rather than assuming it. The Past-Due/0-days cohort now lands on the plan it paid for.
- The provider proof fails CLOSED on an absent expectation set and refuses ANY refund. That is the
  standard the `/return` path should have been held to (finding 2).
- R2-8's `_expire` + v1_28 is narrow, idempotent, reports its rowcount before the commit, and nothing
  in the tree keys off Expired-with-autorenew.
- The new `BILLING_COPY_OVERRIDES` is scoped to one code on one surface and merges over the base, so it
  cannot mask another code's copy or actions; `CONTINUE` now has a real destination.

VERDICT: RED
