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
