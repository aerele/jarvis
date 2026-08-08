# Flow review — 2026-08-08-checkout-due-today-row — round 6
Reviewer: Opus (strict-reviewer)
Date: 2026-08-08
Scope: the hosted checkout card's summary render — the `/session` projection built on a real
Frappe server, and the real shipped bootstrap JS executed against it.

## How it was executed, and what was carried forward

**Executed this round.** A real Frappe server on `test_site` produced the actual `/session`
projection for **20 intent shapes** (EC1-EC11 plus all 10 `BILLING_FLOWS` × shape combinations),
via `bench --site test_site console`. Those projections were then fed to the **real shipped
bootstrap**, extracted from `shell.py` exactly as the browser receives it and executed in Node
through the `window.__jarvisRenderSummary` seam, asserting the row's `hidden` state and that the
amount span and pay button are untouched. 20 render scenarios + 9 fail-safe injections =
**29 scenarios, 0 BREAK**. The live control plane on `jarvis_admin_v2.local:8002` was never
touched and testing there was never enabled. No DB rows were created or modified — the projections
are pure computation over in-memory intent snapshots. Working tree byte-identical afterwards.

**Carried forward from r5, on proven equivalence — not on trust.** r5's 12 browser-only scenarios
(refresh after the clean-URL rewrite, browser Back, deep-link with no token, double-click Pay, slow
`/session`, malformed `/session` body, XSS into `plan_name`, hostile `due_today_display`, console
errors, 320 px + keyboard-only, rate limiting) were **not** re-run in Chromium this round. They do
not need to be, and the reason is measured rather than asserted:

- **The client bytes are identical.** `shell.py` is untouched since r5 (mtime 22:43, before r5's
  verdict files at 23:18/23:19; and r5's cited line numbers `243-244` and `823` still land on
  exactly those constructs). `shell.render_shell()` output: SHA-256
  `edc82f38b7f762bb784f33b995becf089727369cacca6dc526eb7bce0413eb41`, 58 527 bytes. The browser is
  served the same HTML and the same JS, byte for byte.
- **The server bytes are identical.** Every field the card consumes — `plan amount_display`,
  `due_today_display`, `show_due_today`, `trial_note`, `pay_cta_label` — was recomputed live this
  round for all 20 shapes and matches r5's Chromium-recorded values exactly (EC2 `₹1,234.00` with
  an empty note and `Pay ₹1,234.00 and set up auto-pay`; EC5 `₹3,500.00` plan vs `₹5.00` due with
  `Set up auto-pay, ₹5.00 refundable today`; EC11 with no occurrence of "trial"; and so on).
- **The only files that changed since r5** are a comment block that the Python parser discards, a
  test harness that is never shipped, and a CI workflow that is never shipped. There is no
  mechanism by which a browser-level scenario could observe a difference.

## Break attempts

| Scenario | Expected | Actual | Result |
|---|---|---|---|
| EC1 signup order, real server projection → real bootstrap | row HIDDEN | `plan ₹100.00 / due ₹100.00 / show false` → `rowHidden=true`; note "One-time payment. This plan does not auto-renew."; CTA `Pay ₹100.00` | PASS |
| EC2 mandate UPGRADE, equal amounts | row VISIBLE and labelled | `show true` → `rowHidden=false`, `due ₹1,234.00`, note `""`, CTA `Pay ₹1,234.00 and set up auto-pay` | PASS |
| EC3 trial + fee == recurring price | row VISIBLE | `rowHidden=false`, due `₹3,500.00`, "7-day free trial. Then ₹3,500.00 every month, starting 15 Aug 2026." | PASS |
| EC4 autopay, no trial, no fee | row VISIBLE | `rowHidden=false`, due `₹100.00`, "Billed ₹100.00 every month." | PASS |
| EC5 fee-less trial (due != amount) | row VISIBLE | `rowHidden=false`, plan `₹3,500.00` vs due `₹5.00`, "Today's ₹5.00 is a refundable authorization, not a charge." | PASS |
| EC7 ORDER-shaped UPGRADE, equal amounts (r2 BLOCKER) | row VISIBLE | `rowHidden=false`, `₹1,234.00` still labelled next to a positive "One-time payment" note | PASS |
| EC8 order-shaped RENEW | row VISIBLE | `rowHidden=false`, due `₹100.00` | PASS |
| EC9 unrecognised purpose | row VISIBLE | `show true` → `rowHidden=false` | PASS |
| EC10 zero-amount signup order | row VISIBLE | plan `₹0.00`, due `₹5.00`, `rowHidden=false`, CTA `Pay ₹5.00` | PASS |
| EC11 order signup carrying `trial_days=7` | row HIDDEN and no trial promised | `rowHidden=true`; note "One-time payment. This plan does not auto-renew." — the word "trial" does not appear in any projected field | PASS |
| All 10 `BILLING_FLOWS` × {order, mandate} (RENEW, UPGRADE, REAUTH, DOWNGRADE_MANDATE, DUNNING) | row VISIBLE in all 10 | `rowHidden=false` in all 10, including the three nominal-hold mandate shapes that project `due ₹5.00` against a `₹100.00` plan | PASS |
| Fail-safe: `show_due_today` absent from the response (older server) | row VISIBLE | `hidden=false` | PASS |
| Fail-safe: `null`, `0`, `""`, `"false"`, `"0"`, `true`, `[]`, `{}` (8 runs) | row VISIBLE in all 8 | `hidden=false` in all 8 — `=== false` rather than falsy is doing real work | PASS |
| Collateral damage: does the rule touch anything but the row? | amount span and pay button unaffected | `due-today.hidden=false` and `pay-btn.hidden=false` in every one of the 20 shapes | PASS |
| Regression sensitivity: bootstrap that ignores the flag | flow must break | deleting `dueRow.hidden` → 2 harness failures; inverting → 4; always-hide → 7; falsy instead of `=== false` → 4; wrong element → 9. Unmutated control 9/9 pass | PASS |
| Refresh, Back, deep-link, double-click Pay, slow `/session`, malformed `/session`, XSS in `plan_name`, hostile `due_today_display`, console errors, 320 px + keyboard, rate limiting (12 scenarios) | as r5 | CARRIED FORWARD — served bytes proven identical (shell SHA-256 unchanged, projection values identical for all 20 shapes); no shipped file changed since r5's Chromium run | PASS (r5) |

29 scenarios executed this round, 0 BREAK. 12 carried forward on measured byte-equivalence.

## Note

The two open findings are both in the **code** review and both MINOR: an inaccurate `ci.yml`
comment about Node's test discovery, and an overstated sentence in the `endpoints.py` comment.
Neither is a shipped-behaviour change, so fixing them does not invalidate this flow review — but
per `references/flow-review.md` §4, if either fix touches `endpoints.py` or `shell.py` beyond
comment text, this review goes stale and must be re-run.

VERDICT: GREEN
