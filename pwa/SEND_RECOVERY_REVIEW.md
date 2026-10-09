# PR #1785 — round 2 corrections (9 October 2026)

This section supersedes the earlier review below. Scope: Navin-S-R's round-2 comment
6066998312 against `c02675a5`. Followed architect → plan-check → implementation →
review-loop and wear-the-coat. Both reviews were single-agent self-reviews.

## CI integration correction — latest develop

The first round-2 local run tested the branch alone; GitHub tested its merge with
newer develop. CI exposed two tests using `maintenance` as an unknown-code fixture,
although this PR deliberately makes it a known code. Reproduced both failures after
merging `e690e59e` (no conflicts). The fallback tests now use `unknown_future_code`;
separate helper and mounted-dashboard assertions cover maintenance copy and the
re-enabled Retry button. No production behavior was changed to satisfy the tests.
The subsequent Node stage also required wiring develop's new activity-watchdog
boundary and real delta-timestamp helper into the desktop event test harness.

SOP: architect/plan-check scoped this as T1 test integration: retain known-code copy,
unknown-code fallback, escaping and actionable Retry/Stop. Implementation followed
those criteria. Review-loop self-review found no further issue: correctness/edges
are asserted by the actual helper/component/handler; API and concurrency behavior
are unchanged; security retains escaped error tests; no new runtime cost or logging;
resilience retains Retry and Stop tests. Wear-the-coat confirms that dashboard users
see the maintenance explanation and can retry, while unknown failures stay readable.
No new browser validation was needed for test-only corrections.

On the merged branch, 3,497 Vitest tests, 1,156 desktop Node tests and 347 combined
Desk-widget/shared/PWA Node tests pass. The existing single-file Vitest quarantine
is unchanged. Future PR checks must include the current base merge, rather than
assuming a green branch-only run represents GitHub's tested tree.

## Solution and acceptance

Keep the durable claim before dispatch and never reclaim an uncertain request.
Remove the safe-by-default effect markers. A read-only macro preflight returns a
controlled refusal before calling the pipeline; **every exception from the pipeline
is uncertain**, even a validation refusal raised after a committed message. The
legacy API keeps its existing exception contract. Correlated structured logging
contains the opaque key, exception type and code locations, excluding exception
text, locals and payload. It deliberately avoids Frappe's telemetry-forwarding logger.

Desktop requests live outside the view, including in-flight sends. Operation versions
prevent old failures/timeouts from undoing newer checks; settled evidence for the same
ID still wins. First-chat adoption requires its empty home surface with no newer or
parked draft. Otherwise the delivered request keeps its conversation link. Live events
restore Stop independently of receipt timing. Fresh server reads use site-aware dates.

PWA requests adopt their conversation independently of navigation. Empty recovery
snapshots cannot revive submitted model/auto choices. Explicit hero choices and
meaningful drafts survive; upload completion updates the same retained attachment.
A failed/incomplete attachment blocks retry rather than being silently omitted.

## Round-2 finding dispositions

| # | Fix | Evidence |
|---|---|---|
| 1 | Timeout reinjects the request after transcript replacement. | Actual desktop send handler: clear rendered transcript, fire timeout, recovery row reappears. |
| 2 | Late start **and delta-only** events restore run/message IDs and actionable Stop; refresh uses `toLocalMs`; unrelated events no longer invalidate every read. | Actual event and Stop handlers, stopped-run fences, fresh/stale refresh tests; timezone helper source inspected. |
| 3 | In-tab desktop store survives unmount and keeps requests already in flight. | Two view-handler instances share the store; original ID survives, old failure cannot unlock new check. |
| 4 | Late first-chat acceptance adopts only its empty home surface; newer/parked drafts stay put. | Actual send-handler timelines with empty/newer/parked drafts. |
| 5 | Submitted first-send picks are cleared; empty draft snapshots do not restore them. | Recovery store tests and mounted PWA recovery suite. |
| 6 | Empty hero visits do not save implicit choices or override updated defaults. | Mounted hero: visit, leave empty, change settings default, return. |
| 7 | Authentication, CSRF and permission failures explain sign-in/reload/access recovery while retaining the request ID. | Shared error-copy tests; real frappe-ui exception shape inspected. A failed check never proves the original send had no effect. |
| 8 | Unexpected failures emit safe correlated diagnostics and retain the claim. | Real receipt tests and logger-field/privacy assertions. |
| 9 | Removed all mutable effect markers; pipeline exceptions cannot become safe rejections by omission. | Actual send handler commits a message, then dispatch raises a validation error; same-ID retry leaves one message and one dispatch attempt. |
| 10 | Operation versions protect newer checks/retries from an original POST failure. | Desktop actual-handler race and PWA recovery state tests for check and retry; existing mounted late-response tests. |
| 11 | Off-route hero acceptance sets request conversation and refreshes sidebar; next new-chat send is independent. | Mounted hero unmount/late acceptance/remount/send test. |
| 12 | Uploads retain object identity through drafts/unmount; incomplete files cannot be retried silently. | Mounted hero: upload pending, unmount, complete, remount and send with the file URL. Failed uploads expose remove/reattach copy. |
| 13 | Accepted first-send auto mode mirrors server state, including recovery on an existing chat. | Actual home-adoption handler test, fresh-read tests and response-path inspection. |
| 14 | Confirmed control messages retire from the request store; accepted messages reconcile against persisted IDs. | Typed-success/failure handler tests, existing late-confirmation tests and store reconciliation inspection. |
| 15 | Historical rejection codes use shared human copy, including maintenance. | Shared rejection-copy coverage and recovered-result handler inspection. |

Four targeted tests fail against the original `c02675a5` handler: timeout after
transcript replacement, late home adoption, late run:start Stop, and delta-only Stop.
They pass against the corrected handler. These are actual-handler tests with mocked
services, not full desktop browser or live LLM tests.

## Smaller findings and deliberate limits

- MariaDB 1020: a test explicitly enables snapshot isolation, creates a real
  competing connection/write, observes error 1020, and verifies a successful claim
  retry before exactly one dispatch. It runs in the normal database test suite;
  it fails rather than silently skipping if the isolation feature is unavailable.
- Deleted conversations have an explicit receipt status and human explanation.
  The claim remains a replay fence; deletion cannot authorize repeating old effects.
- Same-ID desktop submission respects boot/maintenance/compaction/model gates;
  read-only delivery checks remain available. Dismiss removes the rendered row.
  Fast home acceptance no longer sweeps a separately saved home draft into its result.
- Conservative interrupted outcomes remain intentional: a generic exception does
  not prove absence of committed or remote effects. The explicit macro preflight
  is safe; all other unexpected pipeline failures require checking the conversation.
- The vacuous context source assertion now targets the real send handler and checks
  that the context-consumption call exists. Executable typed-outcome tests cover
  composer unlock and request retirement as well.
- Sidebar dot after desktop unmount: intentionally cleared because that view owns
  the event listener; remount reconciles server state. A cross-view live activity
  indicator would need shell-level event ownership and is deferred, not claimed fixed.
- Receipt cost/retention: removed the redundant unique field declaration; the opaque
  primary key already enforces uniqueness. Keep the ORM insert and pre-dispatch commit
  for correctness. No latency improvement is claimed without a benchmark. Opaque replay
  fences cannot have a TTL while arbitrary old retries are accepted. Wipe scrubs
  metadata; a separate compaction/storage policy remains a future optimization.

## Review-loop and user journey

| Dimension | Assessment |
|---|---|
| Correctness/stability | Actual send/event/Stop handlers, mounted views, real claim/commit/rollback and snapshot-conflict tests pass. |
| Security/data | Ownership checks remain; claims fence retries after wipe; diagnostics exclude private payload and exception text. In-tab stores clear with full reload/logout. |
| Performance/concurrency | Bounded claim retries, no recovery polling loop; stale-operation protection. Macro preflight adds one owned-conversation read to a new attempt. No benchmark claim. |
| Edge cases | Empty/newer/parked drafts, unmount, partial confirmations, missing/deleted receipts, settings changes and pending uploads covered. |
| Maintainability/API | Existing APIs and legacy exception behavior retained; removes manual effect-boundary maintenance. |
| Testing | Full frontend suites, focused database suite, builds, hooks; four before-fix reproductions. Mocked services distinguished from actual database/browser evidence. |
| Resilience | Same-ID recovery survives response loss and navigation; positive evidence cannot be downgraded by an older error. |
| Operability | Clear refusal versus uncertainty, actionable auth copy, request-correlated crash logs and deleted-conversation explanation. |

Wear-the-coat persona: an operations user sending invoices/files and follow-ups,
occasionally moving to Files/Approvals or losing connectivity. Coverage: send,
inspect uncertainty, check/retry the same request, follow the accepted conversation,
and Stop late streaming. Gaps: this round closes the identified draft/upload/control
failures; full-reload/cross-tab recovery is still unavailable. Good-to-haves: shell-owned
live activity and a measured receipt compaction policy. Failure/recovery: text, files,
voice provenance and newer drafts remain separate; unknown effects are never
represented as safe to repeat. Existing voice-store tests pass; no physical-device
microphone or remote ERP-side-effect validation was performed in this round.

## Verification

- 1,124 desktop Node tests and 3,306 Vitest tests pass under Node 24 (CI's existing
  `support-thread.test.js` quarantine retained). 130 PWA Node tests pass.
- 23 real MariaDB receipt tests pass on dedicated `jarvis.test`, including actual
  send/message commit, post-commit validation failure, concurrent claims, wipe,
  lost acknowledgement, and an explicitly observed 1020 conflict. Local Frappe is
  development v17; supported v15/v16 compatibility still relies on CI.
- Desktop and PWA production builds pass. Existing large-chunk warnings remain.
- Built PWA exercised in headless Chrome at 390px using synthetic intercepted API
  responses: failed POST → recovery → identical-payload/ID retry → adopted chat
  with one saved message; no page errors. This is a browser simulation, not live ERP.
- Scoped repository hooks pass. No production deployment or live tenant changes.

No further material defect found within this reviewed scope. This is not a guarantee
against future defects, nor independent review. CI must pass on the pushed head.

---

# Earlier review history (superseded where this section conflicts)

# PR #1785 — reviewer corrections

Review scope: Navin-S-R’s findings against `59dadbb0`, desktop and mobile interactive sends, receipt transactions, and workspace wipe. Workflow: ct-architect → ct-plan-check → ct-implement → ct-review-loop + ct-wear-the-coat. These are single-agent reviews, not independent verification.

## Design and acceptance

Separate historical delivery recovery from starting a new turn. Checks may update only the original request’s outcome; they must not cancel a run, alter Stop, navigate, transfer drafts, restore old preferences, or raise old availability gates. An uncertain retry retains the exact request ID and payload. A proven refusal permits a new ID. A UI deadline exposes recovery while retaining the eventual authoritative response.

The backend commits an opaque claim before dispatch. Losing inserts roll back the whole transaction before a non-locking receipt read. Only the instrumented pipeline can prove an exception preceded send/action effects; after that boundary, failures remain uncertain. Wipe scrubs receipt content into tombstones rather than deleting duplicate protection. Empty-conversation metadata creation can precede the effect boundary.

## Finding dispositions

| Reviewer item | Change and evidence |
| --- | --- |
| 1 — Menu | Restored store import; actual Menu click passes in mounted-view tests and Chrome. |
| 2 — request ID / draft loss | Cryptographic `getRandomValues` before composer mutation; injected RNG-failure tests preserve files/text/unlocked state. Chrome HTTP origin has no `randomUUID` and successfully stages sends. |
| 3 — permanent pending refusal | Internal preparation/effect boundary; real pipeline macro refusal is actionable and settled. Pre-effect rollback and post-effect uncertainty tests pass. |
| 4 — no offline recovery | Both clients offer same-ID retry; exact payload and ID verified in actual-view/function tests. Real DB duplicate tests assert one dispatch. |
| 5 — checks mutate run state | Desktop recovery has a separate handler; active-run tests assert unchanged run IDs, Stop-related state, cards and composer. |
| 6 — historical gates | Stored subscription, maintenance and release rejections affect only the bubble. No automatic reload. Mobile same-ID retries also cannot re-raise an old hold. |
| 7 — hero loses newer work | Hero draft holds text, uploaded files and preferences; blocked dictation remains draft content. Component tests exercise redirect and remount lifecycle. |
| 8 — rejected model immutable | Edit message and model restores the original request on the hero, retaining files and a newer draft. Actual picker/edit/retry passes in Chrome. Fast refusal before unmount cannot overwrite the request with the cleared composer. |
| 9 — typed failure silent | Stored results retain categorized error copy without private details. Desktop displays it on the settled bubble with an explicit conversation link. |
| 10 — late success discarded | Send observer keeps awaiting the original response after its UI deadline. Tests cover late typed outcomes, correct voice acknowledgement and newer work. Bubbles are reactive so late success renders without unrelated state changes. |
| 11 — voice dismissal trap | Dismiss orphans only the request’s retained recordings, exposing Restore/Download/Discard. Real voice-store test verifies the guard changes from invisible live work to actionable retained work. |
| 12 — check blocked by busy | Dedicated check bypasses send guards and leaves the active run alone; function tests cover busy and sending states. |
| 13 — wrong-chat draft transfer | Desktop receipt resolution never navigates, changes preferences or writes connector focus. View conversation opens another tab. Sentinel-to-real-scope retry still sends immutable original wire arguments. |
| 14 — snapshot conflict / lock upgrade | Full rollback after duplicate or transaction conflict, then plain read; bounded insert retry, no ambiguous commit retry. Real concurrent tests pass with snapshot isolation on and off. The old claim reproduced error 1020 with isolation on. |
| 15 — private retention / wipe | Filter summaries and arbitrary confirmation error text; actual wipe entry point scrubs receipt fields. Tests verify erased rows cannot settle again or dispatch again. Opaque keys and timestamps remain as replay tombstones. |

Lower-priority corrections: historical desktop receipts cannot revive queued chips; tests use Python-3.10-compatible patch cleanup; recovery composer and Edit as new preserve first-send choices; dismissal uses tenant branding; transcript refresh failure cannot downgrade a confirmed approval; concurrency tests actually hold an uncommitted result; recovered user rows reconcile only by exact message ID. Outcome classification and bounded-read helpers are shared.

Optional cleanups deliberately deferred: legacy Redis receipt endpoints/result fields and unkeyed send wrappers remain compatible with older callers. Their removal is a separate API retirement. The ORM claim insert, extra durability commit and redundant `request_key` unique index remain; this change does not claim a send-latency improvement. The primary-key claim remains the dispatch arbiter. A raw-insert/index migration needs separate performance evidence and compatibility review.

## Review-loop coverage

| Dimension | Evidence and verdict |
| --- | --- |
| Correctness/stability | Real transaction tests, actual send-function tests and mounted mobile views cover the stated invariants. No remaining substantiated defect in this scope. |
| Security/data | User-scoped keys, conversation ownership, payload mismatch, missing schema and wipe tested. Receipt filter source-inspected and private-text assertions pass. |
| Performance/concurrency | Real simultaneous claims and uncommitted settlement reads; three bounded pre-dispatch attempts, no lock upgrade. ORM/commit cost is retained, not benchmarked. |
| Edge cases | No request received, lost acknowledgement, late result, malformed envelope, RNG failure, changed conversation, rejected model and current newer work exercised. |
| Maintainability/API | Shared ID/classification/deadline helpers; recovery no longer re-enters desktop send. Legacy wire compatibility retained. |
| Testing | Pre-fix Menu/RNG/privacy/refusal failures and old snapshot-isolation failure reproduced. Tests assert prohibited side effects as well as outcomes. See environment limitations below. |
| Resilience | Uncertain retries cannot dispatch an existing ID again; late positive evidence wins. Guarded wipe settlement prevents restoring erased data. |
| Operability | Distinct refusal/uncertainty/action-failure copy; explicit recovery, model repair, retained audio and conversation navigation. No forced reload discards the outbox. |

## Wear-the-coat

Persona: an operations user submitting invoice questions and approving actions from desktop or phone, sometimes over an unreliable connection. Their priority is keeping their question/files and knowing whether an action ran without accidentally repeating it.

- **Coverage:** compose with files/model/effort, send, inspect uncertain delivery, retry safely, repair a rejected model, and recover retained voice. Actual mobile entry/Menu/model controls and desktop recovery Message controls were clicked in Chrome.
- **Gaps:** no remaining substantiated blocker within the changed journey. Full authenticated ERP effects and physical microphone recording were not exercised.
- **Good-to-haves:** persistent cross-tab outbox, more compact recovery controls and measured claim latency remain separate improvements. New controls retain the application’s current English-copy convention; translated copy was not validated.
- **Failure/recovery:** original payload stays separate from newer drafts. Reads do not interfere with desktop active work. Typed failures remain visible; dismissal does not discard audio or claim cancellation. Browser state is still tab-local and disappears on reload/close.

Browser evidence: Chrome at 390px on `http://jarvis-review.test`, `isSecureContext=false` and `crypto.randomUUID` unavailable, with backend RPCs intercepted using simulated responses. Real mobile Menu/model/edit/retry and newer-draft preservation passed; desktop Message keyboard activation, disabled pending check, retry event, outcome link and 390px/1280px layout passed. Screenshots inspected locally. This is component/view evidence, not a full authenticated platform end-to-end test.

## Executed verification and limits

- 17 real-database receipt tests plus 17 legacy receipt tests passed, both with session snapshot isolation ON and OFF on MariaDB 11.4.12. Dispatch stubbed to avoid ERP/LLM effects. This verifies the implicated configuration, not an actual MariaDB 11.6 installation or PostgreSQL.
- 1,104 desktop Node tests, 126 PWA Node tests and 3,302 Vue tests passed. Vue uses CI’s existing `support-thread.test.js` exclusion. Focused follow-up tests cover the final confirmation-refresh, reactive-late-result and fast-refusal/unmount corrections.
- Desktop and PWA production builds passed; desktop retains its large-chunk warning. Repository hooks pass after formatting.
- The broader local API/voice/typed suite ran 134 cases with 28 failures/errors. Re-running with the unchanged PR API loaded in memory produced the identical 28 cases. The local site has stale schema (`Jarvis Message.steps` missing) and send-gate fixture issues; this is not a passing broader integration run. Clean-runner CI remains the integration check.

Deployment still requires migration before updated assets, retaining receipt rows during rollback. Pending claims can remain unknown after a crash; the guarantee is at-most-one dispatch per ID, not exactly-once remote business effects. Database restoration or manual receipt deletion can invalidate it.

## Follow-up architecture and plan-check (2026-10-08)

Scope: reviewer comment 6038948409. T3 because a mistaken rejection/replay can
repeat consequential actions. Keep receipt claims as the dispatch fence. Do not
expire/reclaim pending claims or infer delivery from matching message text.

Use additive fixed-code receipt diagnostics (`missing`, `pending`, `interrupted`,
`unavailable`). An observed exception after the effect boundary rolls back only
uncommitted work and records an unresolved outcome under the existing claim;
it must never become a retryable rejection. A hard process crash cannot prove an
outcome: report that absence of final evidence explicitly and retain the fence.
Never persist raw exception messages, including pre-effect ValidationErrors.

For desktop acceptance recovery, fetch current transcript separately from the
navigation/reload handler. Preserve drafts, choices, navigation, newer sends and
stopped/live runs. Fresh streaming metadata may restore Stop only for the same
accepted run when no newer work/event intervened. Consume only the original
one-shot context, using selection identity/revision rather than value equality.
Do not apply stale receipt queue positions or availability gates.

Plan-check: correctness/security/concurrency require real receipt rollback,
repeat-request and wipe checks. Edge/API/resilience require old unknown envelopes,
missing claims, pending crashes, interrupted outcomes and refresh failure. UI
checks require late success, same-ID acceptance, navigation/new send during refresh,
newer context and stopped-run preservation. Operability uses fixed explanatory
copy plus request identity; no model or business-result inference. No schema change.
Ready for scoped implementation; actual remote effects remain outside local tests.


### Follow-up implementation and verification

- Remaining #3 / crash diagnostics: distinguish missing claims, pending claims,
  observed interrupted execution and erased receipts. Exceptions after possible
  effects preserve the claim and record only a fixed interrupted marker after
  rolling back uncommitted work. Same-ID retries cannot dispatch again. Hard
  crashes still cannot be called rejected without evidence; copy says this
  explicitly instead of implying that another check will necessarily settle it.
- Exception privacy: pre-effect refusals use fixed copy even on the initial
  response. The macro-run refusal retains an actionable, controlled explanation.
- Late acceptance: both original late replies and same-ID recovery consume only
  their own context revision and voice provenance. A newer same-valued selection
  is a different revision and survives.
- Transcript/live controls: a separate read refreshes the currently viewed chat.
  Fresh same-run streaming metadata can restore run/message IDs, Stop state,
  steps/narration and event fences. Receipt queue positions/gates are never
  applied. Active work, navigation, new sends, realtime events and changed state
  invalidate/skip the refresh; drafts, files, preferences and stopped runs survive.
  Refresh failure cannot downgrade a confirmed outcome.

Review-loop (single-agent): correctness and concurrency checked with actual
handler execution plus real receipt transactions; security with fixed copy,
permission paths and wipe fences; performance with one read per recovered result
and no polling loop; edge cases with old envelopes, stale responses, failures and
new selections; API changes are additive with no schema migration; resilience
retains positive outcomes and duplicate protection; operability distinguishes
uncertain causes. No further substantiated defect in this scoped follow-up.

Wear-the-coat (same operations-user persona): coverage improves explanation,
same-request recovery and seeing accepted work; the unavoidable gap is an outcome
that cannot be proven after a crash; persistent cross-tab recovery remains optional
future work; failure/recovery preserves newer work, audio and positive receipts.
Existing status announcements/buttons render the fixed copy as text. A mounted
Message test clicks same-ID retry and verifies escaping. No new full-browser,
physical-device or live ERP-effect test was run for this follow-up.

Validation: 37 real database tests pass with snapshot isolation ON and OFF on
MariaDB 11.4.12; 1,114 desktop Node tests, 127 PWA Node tests and 56 focused mounted
Vue tests pass. Desktop and PWA production builds pass (existing desktop chunk-size
warning remains). Scoped hooks pass. These results do not establish actual MariaDB
11.6/PostgreSQL behavior or live remote effects. Follow-up patch does not reclaim
unknown claims, so it does not weaken the at-most-one-dispatch contract.

CI follow-up: the full frontend suite caught the new refresh epoch increment
before the bootstrap guard. Moved it after the guard, preserving the existing
no-side-effects-during-boot invariant; readiness and recovery regressions rerun.
