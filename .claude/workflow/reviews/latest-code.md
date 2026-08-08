# Code review — 2026-08-08-checkout-due-today-row — round 6
Reviewer: Opus (strict-reviewer)
Date: 2026-08-08
Scope: `jarvis_admin_v2/billing/checkout/endpoints.py`, `jarvis_admin_v2/billing/checkout/shell.py`,
`jarvis_admin_v2/tests/billing/test_checkout_due_today_row.py` (untracked),
`jarvis_admin_v2/tests/frontend/test_due_today_row.mjs` (untracked),
`jarvis_admin_v2/tests/frontend/test_opener.mjs`, `.github/workflows/ci.yml`.
`jarvis_admin_v2/www/dashboard.html` excluded (user's separate work, stays uncommitted).
Plan: `apps/jarvis/.claude/workflow/plans/2026-08-08-checkout-due-today-row.md` (STATUS: APPROVED).

## Verification of the "nothing executable changed since r5" claim

Verified independently, not accepted:

| File | mtime | vs r5 verdict files (23:18 / 23:19) | Conclusion |
|---|---|---|---|
| `shell.py` | 22:43:26 | before | untouched since r5 |
| `test_checkout_due_today_row.py` | 22:49:35 | before | untouched since r5 |
| `test_opener.mjs` | 22:49:35 | before | untouched since r5 |
| `endpoints.py` | 23:21:13 | after | changed |
| `test_due_today_row.mjs` | 23:20:33 | after | changed |
| `ci.yml` | 23:20:51 | after | changed |

Corroborated by line-number forensics rather than mtime alone. r5 cited `shell.py:243-244` and
`shell.py:823`; both land on exactly those lines today. r5 cited twelve line numbers inside
`test_checkout_due_today_row.py` (55, 63, 71, 79, 86, 96, 103, 114, 121, 134, 137, 146); all twelve
still land on the cited construct. In `endpoints.py`, every executable site r5 cited BEFORE the new
block is unshifted (`:388`, `:394-396`, `:448`, `:453`, `:461`) and every site AFTER it has moved up
by exactly 11 lines (`:595` → `:584` `**trial_fields`; rule `:522-526` → `:511-515`) — the precise
delta of a 32-line comment becoming 21 lines. The change to `endpoints.py` is comment-only;
the rule, the projection key, the flag read, the row id and the render seam are byte-identical to
what r5 reviewed. Claim CONFIRMED.

## Findings

| # | Severity | Location | What breaks | Required fix |
|---|----------|----------|-------------|--------------|
| 1 | MINOR | `.github/workflows/ci.yml:463-466` (the "Explicit glob, not the directory" paragraph) | The corrected diagnosis is **still wrong**, and this is the second consecutive round it has shipped wrong. The comment says the files "are named test_*.mjs, which matches none of Node's default discovery patterns, **so** `node --test <dir>` runs the directory AS a script", and adds "(Node is fine - a *.test.mjs in the same directory IS discovered.)". Both claims are false on Node 24.12.0, the version this job pins (`setup-node node-version: "24"`). Measured: a directory containing only `thing.test.mjs` → `Error: Cannot find module …/b`, exit 1. Containing only `thing.test.js` → same. Containing `test/plain.mjs` → same. Same result with an absolute path, a `./` path and a trailing slash. Discovery happens only with **no positional argument** (`cd b && node --test` → `pass 1`) or with a glob (`node --test 'b/*.mjs'` → `pass 1`). The filenames are irrelevant to the failure; a positional directory is never searched, it is loaded as a module. Failure scenario: a maintainer trusts the parenthetical, renames the harnesses to `due_today_row.test.mjs`, reverts to `node --test <dir>`, and gets a red build with a MODULE_NOT_FOUND stack that says nothing about test discovery — the natural next move being to weaken or delete the step, which is the only gate standing between a shell regression and a hidden payment disclosure. | Replace the causal claim and the parenthetical with the measured rule: on Node 24 a positional **directory** argument is never searched for test files — it is resolved as a module and fails with `Cannot find module`, whatever the files inside are named. A quoted glob (or no positional argument) is what triggers discovery. Keep the `ls` guard note, which is correct. |
| 2 | MINOR | `jarvis_admin_v2/billing/checkout/endpoints.py:501-503` | The comment overstates `_due_today_minor` in the direction of inviting deletion of a load-bearing conjunct. It asserts "for ANY order `_due_today_minor` returns `amount_minor` verbatim, so an amounts-differ test is always true". False for `amount_minor <= 0`: `endpoints.py:394-396` replaces the figure with the provider's nominal hold (verified live this round — a zero-amount signup order projects `plan ₹0.00 / due ₹5.00 / show_due_today true`). Secondarily the sentence inverts its own terms: the *amounts-differ* test (`due != amount`) is always **false** for orders; what is always true is the equality. Failure scenario: a maintainer reads "for ANY order … verbatim", concludes the third conjunct `due_today_minor == int(intent.amount_minor or 0)` can never be false on the SIGNUP-order path, and deletes it as dead code; the row then hides on a zero-amount signup order whose real charge is a ₹5 hold, leaving `₹0.00` on the plan row and `Pay ₹5.00` on the button with nothing labelling the gap. Contained to a red suite rather than a shipped defect — `test_zero_amount_signup_order_shows_the_row` fails — hence MINOR, not MAJOR. | Qualify it: "for an order with a positive amount, `_due_today_minor` returns `amount_minor` verbatim, so the equality is always true and the rule degenerates to 'hide on every order'; the zero/negative case falls to the nominal hold, which is the only thing this conjunct decides (EC10)." |

No BLOCKER. No MAJOR. The four r5 findings are all resolved (see below).

## r5 findings — resolution verified

| r5 # | Sev | Status |
|---|---|---|
| 1 | MAJOR | RESOLVED. `npx prettier@2.7.1 --check 'jarvis_admin_v2/tests/frontend/*.mjs'` → "All matched files use Prettier code style!", run from the repo root so it resolves `.editorconfig`/config exactly as the pre-commit hook does. `.mjs` is not in `.editorconfig`'s tab-indent glob list, so prettier's 2-space output is stable. |
| 2 | MINOR | RESOLVED and the guard is load-bearing. Measured: `bash -e` script file, `ls <no-match> > /dev/null` → the script exits **1** and the following line never runs ("REACHED SECOND LINE" not printed). Against the real glob the step runs 23 tests and exits 0. Without the guard, `node --test '<no-match>'` prints `tests 0` and exits 0. GitHub Actions' default shell for `run:` on Linux is `bash -e {0}` — a script file, which is what I reproduced. |
| 3 | MINOR | NOT RESOLVED — see finding 1. The comment was rewritten to a different wrong diagnosis. |
| 4 | MINOR | RESOLVED. 32 comment lines → 21. The two paragraphs r5 objected to are gone or folded: the dropped edge-case paragraph (a conjunct that no longer exists) is deleted outright, and the `ClaimContext` point survives as one clause. Nothing load-bearing was lost — "why purpose, not shape", "why not the arithmetic", "RENEW/DUNNING excluded", "snapshot not the Plan doctype" and "unrecognised purpose shows the row" all remain. The `ClaimContext.purpose` claim is accurate: `intent_ledger.py:97 PURPOSE_SIGNUP = "SIGNUP"`, `intent_ledger.py:188 purpose: str = PURPOSE_SIGNUP` inside `class ClaimContext` (`:179`). |

## Plan conformance

The rule is the plan's amended (post-r2) allowlist:

```python
show_due_today = not (
    purpose == ledger.PURPOSE_SIGNUP
    and not is_autopay
    and due_today_minor == int(intent.amount_minor or 0)
)
```

`endpoints.py` adds only the rule and one projection key; `shell.py` adds the flag read, the row
`id` and the render seam; `test_opener.mjs` is a one-line doc correction; `ci.yml` adds the step r4
demanded (test infrastructure serving T3's acceptance criterion, not scope creep). Single projection
path confirmed: `_trial_summary_fields` has exactly one caller (`endpoints.py:572`) and
`_display_projection` exactly one (`endpoints.py:674`, `handle_session`), so there is no second
render path that could disagree. `id="due-today-row"` occurs once in `_SHELL_HTML` and once in the
bootstrap. `www/dashboard.html` is untouched by this change set and must not be staged.

## Edge-case verification

Every row below re-derived this round from live server output on `test_site` plus execution of the
real shipped bootstrap — not carried over from r5's table.

| Plan edge case | Handling site | Test | Verified |
|---|---|---|---|
| EC1 one-time order, due == amount → HIDE | `endpoints.py:511-515` | `test_checkout_due_today_row.py:55`; harness `test_due_today_row.mjs:115` | YES — live projection `plan ₹100.00 / due ₹100.00 / show false / "One-time payment…" / Pay ₹100.00`; real bootstrap sets `rowHidden=true` |
| EC2 mandate UPGRADE, equal amounts → SHOW | `endpoints.py:512` (purpose conjunct) | `…py:63`; `.mjs:120` | YES — `₹1,234.00`, `trial_note ""`, `show true`, `rowHidden=false`, CTA `Pay ₹1,234.00 and set up auto-pay` |
| EC3 trial + fee == price → SHOW | `endpoints.py:513` (`not is_autopay`) | `…py:71` | YES — `show true` beside "7-day free trial. Then ₹3,500.00 every month, starting 15 Aug 2026." |
| EC4 autopay, no trial/fee, equal → SHOW | `endpoints.py:513` | `…py:79` | YES — `show true`, "Billed ₹100.00 every month." |
| EC5 due != amount (trial/fee/hold) → SHOW | `endpoints.py:514` | `…py:86`, `…py:114` | YES — fee-less trial projects `plan ₹3,500.00` vs `due ₹5.00`, CTA `Set up auto-pay, ₹5.00 refundable today` |
| EC6 flag absent → SHOW (fail-safe) | `shell.py:243-244` (`=== false`, not falsy) | `…py:146` (source pin) + `.mjs:134,139` | YES — all 9 injections (absent, `null`, `0`, `""`, `"false"`, `"0"`, `true`, `[]`, `{}`) leave `hidden=false` when driven through the real bootstrap |
| EC7 ORDER-shaped UPGRADE → SHOW | `endpoints.py:512` | `…py:96` | YES — `show true`, `₹1,234.00`, and this branch emits the positive "One-time payment" note, so the row is the only label on the prorated figure |
| EC8 every `BILLING_FLOWS` purpose, both shapes → SHOW | `endpoints.py:512` | `…py:103` (10 subtests) | YES — all 10 combinations projected live, `show true` in every one |
| EC9 unrecognised purpose → SHOW | `endpoints.py:512` | `…py:121` | YES at unit level. Not constructible in a browser: `Jarvis Payment Intent.purpose` is a Select limited to the six known flows — an additional defense, not a gap |
| EC10 (r3) zero-amount signup order → SHOW | `endpoints.py:514` + nominal-hold fallback `endpoints.py:394-396` | `…py:114` | YES — `plan ₹0.00 / due ₹5.00 / show true`. This is the sole behaviour the amount conjunct decides (and the subject of finding 2) |
| EC11 (r4, conjunct dropped) order signup with `trial_days` | `endpoints.py:461` gates the trial sentence on `is_autopay and trial_days > 0` | live projection | YES — projects `show false` with `trial_note` = "One-time payment. This plan does not auto-renew."; the card promises no trial, so the row has nothing to qualify |

## Mutation evidence — reproduced independently, without touching the repo

Server rule, derived from the live projection matrix rather than by editing the tree (each conjunct
has a shape whose answer flips when it is removed, and a named test asserting that shape):

| Conjunct dropped | Shape that flips | Test that goes red |
|---|---|---|
| `purpose == PURPOSE_SIGNUP` | EC7 order UPGRADE (`due == amount`, not autopay) → would hide | `test_order_shaped_upgrade_shows_the_row`, plus 5 of the 10 EC8 subtests |
| `not is_autopay` | EC3/EC4 mandate SIGNUP (`due == amount`) → would hide | `test_autopay_without_trial_or_fee_shows_the_row`, `test_trial_with_a_signup_fee_equal_to_the_price_shows_the_row` |
| `due == amount` | EC10 zero-amount signup order → would hide | `test_zero_amount_signup_order_shows_the_row` |
| whole rule inverted | EC1 → would show | `test_one_time_order_hides_the_row` |

Shell: the repo harness was copied to a scratch path and pointed at **mutated copies** of `shell.py`
(the repo file was never modified — final `git status` and `git diff --stat` are byte-identical to
the invocation snapshot):

| Mutation of the bootstrap | Repo harness result |
|---|---|
| delete the `dueRow.hidden` line | 7 pass / **2 fail** |
| `=== true` (inverted) | 5 pass / **4 fail** |
| `dueRow.hidden = true` (always hide) | 2 pass / **7 fail** |
| `!s.show_due_today` (falsy instead of `=== false`) | 5 pass / **4 fail** |
| target `$("due-today")` instead of the row | 0 pass / **9 fail** |
| unmutated control | **9 pass / 0 fail** |

This reproduces r5's table exactly and independently confirms the central claim: the seam plus the
Node harness distinguish "obeys the flag" from "ignores it", which no source-grep test can.
Removing `id="due-today-row"` from `_SHELL_HTML` is invisible to the harness (its stub
`getElementById` manufactures any id) and is caught instead by
`test_checkout_due_today_row.py:134`. Neither layer alone is sufficient; together they are.

## Attack pass

| Attack | Outcome |
|---|---|
| Hiding removes a figure that differs from the plan row | STRUCTURALLY IMPOSSIBLE — the hide fires only when the two minors are equal, and both strings come from the same `_format_amount` with the same `symbol`/`currency` (`endpoints.py:571` and `:453`). Equal minors ⇒ byte-identical strings. Hiding can never drop a different number. |
| Type confusion (`amount_minor` as `"10000"`, float, `None`) desynchronises the rule from the display | DEFENDED — `_format_amount` itself does `int(amount_minor or 0)` (`endpoints.py:296`), and the rule does `int(intent.amount_minor or 0)` (`:514`). Both sides coerce identically; they cannot disagree for any input. |
| An order-shaped signup carrying `signup_fee_minor` hides a row that was disclosing the fee | NOT ATTRIBUTABLE AND HARMLESS — `_due_today_minor`'s non-autopay branch ignores `signup_fee_minor` (pre-existing, `endpoints.py:387-388`), so the row would have displayed the *same* string as the plan row anyway. Hiding removes no information. |
| Second projection path renders the summary without the flag | DEFENDED — one caller each for `_trial_summary_fields` (`:572`) and `_display_projection` (`:674`); `/start` serves only `return_url`, never a summary. Grep-verified across the whole app. |
| `hidden` defeated by `.row { display: flex }` | DEFENDED — `shell.py:823` `[hidden] { display: none !important; }`. |
| Sticky hide across re-renders (row hidden once, never restored) | DEFENDED — `shell.py:244` is an unconditional assignment on every `renderSummary`, not a one-way hide. |
| Row visible for a frame before being hidden (flash of a redundant figure) | DEFENDED — `hidden` is set at `:244`, `show("view-summary")` at `:256`; the view is revealed only after the decision is applied. |
| Client re-derives the decision from the two amounts (the r1 defect) | DEFENDED — `shell.py:243-244` reads the flag only; pinned by `test_checkout_due_today_row.py:137` and by the harness. |
| `purpose` case/whitespace drift (`" signup"`, `"signup"`) — not normalised, unlike `shape` at `:448` | SAFE BY DIRECTION — any mismatch fails the equality and SHOWS the row. Only exactly `"SIGNUP"` can hide. |
| `window.__jarvisRenderSummary` shipped on a payment page | ACCEPTED, NO FINDING — reachable only from same-origin script (post-XSS, where `location = evil` is already available). Every path it drives is inert: `text()` uses `textContent` (`shell.py:53`) and `rememberReturn` → `safeReturn` (`shell.py:87-99`) enforces an http(s) scheme, rejects control characters and rejects any authority containing `@`. It buys real coverage — removing it turns 9 tests red. |
| `ls` guard passes while one of the two harnesses has been deleted or renamed | ACCEPTED, NO FINDING — the guard closes the empty-glob hole r5 raised; partial deletion is a deliberate act visible in the diff and is out of the guard's remit. |
| Concurrency / dependency failure | N/A as the plan states — pure computation over an already-read immutable snapshot; no write, no network, no cache. Re-confirmed by reading the function. |

## Test / lint status reproduced independently

- `bench --site test_site run-tests --module …test_checkout_due_today_row` → **12/12 OK**
- `…test_checkout_shell` → **35/35 OK**; `…test_checkout_transport` → **78/78 OK**
- `node --test 'jarvis_admin_v2/tests/frontend/*.mjs'` (the exact CI command, from the repo root) → **23/23 pass**, exit 0
- `ruff 0.14.10` (the pinned version, from the pre-commit cache) — `check` all passed; `check --select=I` all passed; `format --check` 3 files already formatted
- `prettier 2.7.1 --check` on both `.mjs` → clean
- `eslint 8.44.0` with the repo `.eslintrc` on both `.mjs` → exit **0**
- `no-committed-secrets` local hook (`python3 jarvis_admin_v2/tests/test_no_committed_secrets.py`) → exit 0
- `check-ast` equivalent on the new Python module → parses; no `breakpoint()`/`pdb` in any changed file
- `trailing-whitespace` (hook regex `jarvis_admin_v2.*`) → no trailing whitespace in any changed file; `.editorconfig` `insert_final_newline`/`trim_trailing_whitespace` satisfied
- `check-yaml` equivalent → `ci.yml` parses; `frontend-tests` has 4 steps; the new step carries **no** `working-directory`, so it runs from the repo root, which its glob requires
- New files are not gitignored (`git check-ignore` → no match), so `git add` will pick them up
- The `tests` job runs `bench --site test_site run-parallel-tests --app jarvis_admin_v2` (`ci.yml:276-277`), which shards per test file — the new module is auto-discovered, no wiring needed
- Working tree after the review is byte-identical to the invocation snapshot

VERDICT: GREEN
