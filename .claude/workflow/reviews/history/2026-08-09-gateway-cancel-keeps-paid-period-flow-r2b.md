
---

# Flow review — 2026-08-09-gateway-cancel-keeps-paid-period — round 2, second independent pass (B)
Reviewer: Opus (strict-reviewer), separate context
Date: 2026-08-10

**Why there are two passes.** Two reviewer instances ran concurrently and neither saw the other until
write time. Pass A (above) is preserved; this is appended, not substituted. Both return RED.

**How it was driven.** The admin control plane was exercised as a running Frappe service. Razorpay
webhooks were delivered as genuine POSTs to `.../billing.webhook.razorpay_webhook` on
`127.0.0.1:8002` with `Host: test_site` and a real HMAC-SHA256 `X-Razorpay-Signature`. Customer
endpoints were driven over real HTTP with a real login session as the customer's own
`cust-…@jarvis.invalid` user, so `current_customer()` and the full permission chain were in play.
Cashfree's status handler was driven in-process on committed rows (its signature scheme differs;
pass A drove it over signed HTTP and found more — see A1). Eight fixture customer/plan/subscription
sets were created and **all were deleted afterwards**; both working trees are byte-identical to their
pre-review state (`git status --short` unchanged in `apps/jarvis_admin_v2` and `apps/jarvis`).

**The report was additionally driven against the LIVE control plane** (`jarvis_admin_v2.local`),
which is the one thing its acceptance criterion is about. Read-only was not assumed: SHA-1 snapshots
of `name`+`modified` across `Jarvis Subscription`, `Jarvis Customer`, `User`, `Jarvis Tenant` and
`Jarvis Razorpay Webhook Log` were taken before and after — identical.

**Housekeeping disclosure.** Two malformed leftover rows in `test_site`'s webhook log (the residue of
pass A's break-attempts, `rvw2-…` event ids) were deleted after they were used as evidence for B1, so
the rest of the report flow could be exercised. Nothing else on any site was mutated by this pass
beyond the fixtures listed above, all since removed. The bench frontend was rebuilt (`npm run build`)
because the pane change was not present in the served bundle; the output directory is gitignored.

## Break attempts (pass B)

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| **B1** Mid-period autopay sub, real signed `subscription.cancelled` over HTTP | autopay off, nothing else | 200 `{"ok":true}`; `status` Active, `autorenew` 0, `cancelled_at` NULL, `current_period_end` unmoved, `Jarvis Customer.status` Active, `User.enabled` 1 | PASS |
| **B2** Exact redelivery (same `X-Razorpay-Event-Id`) | no second write | 200 `{"ok":true,"data":{"idempotent":true}}` | PASS |
| **B3** Fresh event id, same already-off row | no write at all | 200, state byte-identical | PASS |
| **B4** `subscription.completed` on the same row | non-destructive, never a revoke | 200, state unchanged | PASS |
| **B5** Forged signature on a cancel-shaped body | 400 before dispatch | 400 `BadSignature` | PASS |
| **B6** Subscription id no row carries | skip, never raise (no retry storm) | 200 `{"ok":true}` | PASS |
| **B7** Hostile ids: `' OR 1=1 --`, `../../etc/passwd` | no injection, no traversal, no match | 200 each, no row touched | PASS |
| **B8** **Empty** subscription id — could it match a one-shot row whose id column is `""`? | must not resolve | 200, nothing touched; defended by the `if rzp_sub_id` guard in `_sub_for_rzp_subscription` before the `db.exists` | PASS |
| **B9** Signup-stage row (`Pending Payment` + mandate id + `autorenew=1`) — the shape `expire_abandoned_checkouts` cancels daily | must NOT be dunned | stayed **`Pending Payment`**, `autorenew` 0; `_signup_payment_state` returned `NO_CURRENT_INTENT`, **not** `SIGNUP_TERMINAL` | **PASS — round-1 BLOCKER fixed** |
| **B10** `Expired` row, dead mandate echo | must never resurrect into an entitled status | stayed `Expired`, `autorenew` 0 | PASS |
| **B11** `Active` row past `current_period_end` | genuinely overdue → dun | `Past Due`, `autorenew` 0 | PASS |
| **B12** **`POST cancel_plan_at_period_end` as the customer, on an Active entitled sub with `autorenew=0` and a released (dead) mandate id** | either cancel, or refuse and say why | **`{"ok":true, …, "can_cancel":false}` and nothing changed** — a silent success on a live subscription the customer asked to cancel | **BREAK — finding B2 (MAJOR)** |
| **B13** Bench "Cancel auto-renewal": **two genuinely parallel** HTTP cancels on a fresh autopay sub | one release, one end state, no `cancelled_at` | both 200 with identical settled payloads; final row Active / `autorenew` 0 / `cancelled_at` NULL / `can_reauthorize` true / `cancel_at_period_end` 0. The gateway release itself threw (`razorpay_key_secret` unset on `test_site`) and was swallowed — edge 20 exercised for free, local write intact | PASS |
| **B14** True one-shot sub (no mandate id at all): cancel, then resume | unchanged from before this change | cancel → `cancel_at_period_end` 1 + `cancelled_at` stamped; resume → cleared, `can_cancel` back to true | PASS |
| **B15a** Autopay-cancelled sub 2 days past period end, run `expire_overdue_subscriptions` | grace applies, do NOT expire | stayed `Past Due` | PASS — plan Q3 confirmed live |
| **B15b** Same sub wound 30 days past end, full sweep chain | `lapse` → Past Due → `expire` → Expired, login preserved | `lapse=1`, `expire=1`, row `Expired`, `Jarvis Customer.status` Active, `User.enabled` 1 | PASS |
| **B16** Partial refund, then full refund, both signed over HTTP | partial no-op; full revokes | partial: no change. Full: sub `Cancelled`, customer `Suspended`, `User.enabled` 0 | PASS — terminal path intact |
| **B17** Cashfree `CANCELLED` vs `ON_HOLD` on identical lapsed fixtures | dead duns, pause does not | `CANCELLED` → `Past Due`; `ON_HOLD` → still Active, `autorenew` 0 | PASS on the flat payload — but see A1: unreachable on the nested payload |
| **B18** `report_defect_victims()` against the **live control plane** | prints, mutates nothing | printed the single Cancelled Razorpay-mandate row, classified `excluded_operator` with its reason; before/after table hashes identical (`MUTATED: False`) | PASS |
| **B19** `report_defect_victims()` on `test_site`, which holds validly-signed malformed log rows | skip the bad row, report the rest | **`TypeError: cannot use 'dict' as a dict key`** — the whole run died, zero victims printed | **BREAK — finding B1 (BLOCKER)** |
| **B20** Report classification end-to-end after removing the poisoned rows: Guest+evidence / non-Guest+evidence / non-Guest+no evidence / full refund | victim / uncertain / excluded_operator / excluded_refund | exactly that, across 7 candidates, each with its `modified_by` and evidence ids; the batched tenant lookup gave each customer its own container | PASS — round-1 finding 5 confirmed live |
| **B21** Report hostile `window_days`: `0`, `-1`, `10**9`, `"365"`, `None`, `True`, `1.5` | reject nonsense clearly | `0`/`-1` → clean `ValueError`; `10**9` → uncaught `OverflowError`; `"365"`/`None` → opaque `TypeError`; `True`/`1.5` silently accepted | partial — finding B4 (MINOR) |

### NOT RUN

| Scenario | Reason |
|---|---|
| Plan flow **3** — bench UI: press "Cancel auto-renewal" in Settings → Plan & billing; assert the new confirm copy, no "your plan ends on X" banner, no Resume, "Set up auto-renewal" offered | No browser could be driven on this host. Chrome extension not connected (`list_connected_browsers` → `[]`). The repo's Playwright harness cannot start: the vite dev server returns HTTP 500 (its frappe-ui plugin shells out to `bench list-app-sites`, absent from this frappe build) and binds IPv6-only, so chromium cannot reach the configured `localhost:8080` — the pre-existing `tests/e2e/mobile.spec.js` fails the same way. The bench-served bundle needs Administrator credentials I do not have. |
| Plan flow **4** — bench UI: one-shot "Cancel subscription" still shows the ending-plan banner, the `Ends D MMM` pill and Resume | Same. |
| Plan flow **2** — "Set up auto-renewal" minting a replacement mandate | Requires live Razorpay credentials; `razorpay_key_secret` is unset on `test_site`. The `can_reauthorize: true` half was confirmed live on the real payload. |

Both NOT-RUN UI scenarios are recorded as finding B3 (MAJOR). The server halves of both were executed
here over real HTTP (B12–B14) and the pane logic is covered by 12 component specs plus pass A's
live-payload harness — but neither pass opened Settings in a browser, which is what those two
scenarios ask for.

VERDICT: RED
