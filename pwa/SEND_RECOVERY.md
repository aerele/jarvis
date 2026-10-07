# Conversation send recovery

`ChatView` preserves each outgoing request separately from the server transcript and composer draft. The snapshot includes text, uploaded file URLs/names, the originating conversation and the displayed approval-token order. In-app route changes retain requests and drafts in memory. Reloading, closing the tab, or signing out clears them; this feature is not a persistent offline outbox. The new-chat hero stages the same recovery request, including its model, thinking and auto-mode choices, then opens `/send-recovery`. Returning to the hero exposes the pending request rather than sending it again.

Confirmed rejections offer Retry, Edit and Discard. Release-update rejection offers Reload instead of Retry, through a shared Sheet that explains tab-memory loss and offers a downloadable backup of all preserved text and file links. Reload is always an explicit user action. Editing uses the shared Sheet and never replaces a newer composer draft. Retrying allocates a new request ID and reuses all files and the original displayed token order; it does not reselect numbered approval targets. The server's existing unnumbered approval-sweep semantics remain unchanged. Discard removes the local request, not uploaded files or accepted work.

An exception, malformed response, or a POST still pending after 30 seconds becomes Delivery not confirmed. Check delivery only reads `jarvis.chat.send_requests.check_delivery`; it cannot execute a second send. Late responses cannot overwrite a newer retry or downgrade an already settled result. Delivery reads have a 15-second deadline; timeout unlocks Check delivery without permitting replay, and late read results cannot affect a subsequent check. Normal acceptance retains a conversation/run-scoped busy state until matching lifecycle evidence arrives; fetching the user row alone does not unlock the composer. Normal acceptance is reconciled by the exact returned message ID, never by matching text. Typed confirmations are handled before ordinary ok:false rejection because they can execute without a user-message row or new turn.

If a stale conversation is retargeted to another chat, its destination composer is preserved. A colliding source draft appears in a saved-draft card; Use this draft swaps it with the composer, preserving text and files on both sides. It never sends a request or transfers approval tokens.

Queue feedback is separate from transcript reconciliation. Once a saved user message replaces its local recovery card, an accepted queued turn keeps its status and composer lock. Existing owner-checked admission endpoints discover queued/preparing/ready turns on open, focus and resync, and poll a pending run every 10 seconds. Reads are bounded and fenced against navigation and lifecycle events. Cancellation uses `cancel_queued_turn`; lost responses keep the status and offer a read-only status check. Matching start/terminal events retire queue feedback. Unknown states and failed reads retain the last known status.

## Backend contract and limits

Updated PWA and desktop chat clients call `jarvis.chat.send_requests.send_message` with a random 128-bit client request ID. `Jarvis Chat Send Request` stores a user-scoped key and payload fingerprint. The dedicated endpoint commits this claim before dispatching to the existing send pipeline, which can perform internal commits and typed confirmations. The filtered result is then stored in the normal request transaction. A missing result or pending claim never authorizes replay. Concurrent duplicates cannot dispatch again, and changed payloads under the same ID are refused.

Receipts contain no message text, attachment URLs, or tool results. They have no expiry: do not purge them without durable replacement tombstones. Database restore or manual receipt deletion can invalidate the guarantee. This is at-most-one dispatch for one ID, not exactly-once remote ERP effects, offline persistence, or deduplication of separately composed messages.

Deploy with `bench --site <site> migrate` before serving updated assets. Missing receipt storage fails closed. Rollback may restore older assets/endpoints, but must retain receipt rows. The old `jarvis.chat.pwa_send` Redis endpoints remain compatible with old tabs; those older clients and callers of the legacy unkeyed send endpoint do not acquire the new guarantee retroactively.

Desktop keeps uncertain requests on their original bubble with Check delivery and an explicit Dismiss confirmation; a check never posts the payload again. Confirmed rejection can start a new attempt. Receipt reads do not clear a current maintenance hold; accepted receipts refresh the conversation while preserving newer composer selections.

## Deployment and verification

Deploy and migrate the backend before the new client bundles. Roll back the clients first and retain the receipt table; legacy send callers are unchanged. Do not force reload on release-update rejection while the request is held only in tab memory.

- `node --test pwa/src/lib/*.test.js`
- From `frontend`: `npm test -- src/components/PwaSendRecovery.spec.js src/components/PwaChatSendRecovery.spec.js`
- In the bench Python environment with this checkout on PYTHONPATH: `python -m unittest jarvis.tests.test_pwa_send_receipts`
- From `pwa`: `npm run build`

The legacy receipt unit suite mocks Redis. `jarvis.tests.test_chat_send_requests` uses real database transactions and concurrent connections on a dedicated `.test` site after migration, with dispatch stubbed to avoid ERP/LLM effects. Real ERP actions and Postgres concurrency require separate integration validation. Component tests use the real recovery card and ChatView with mocked APIs; browser checks should include keyboard focus, Escape, newer-draft preservation, slow HTTP responses and route changes.

Browser captures are documented in the PR description, not committed as repository assets. Shared Sheet tests cover keyboard focus, Escape, nested sheets and scroll cleanup.
