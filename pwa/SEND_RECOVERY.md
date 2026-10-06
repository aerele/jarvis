# Conversation send recovery

`ChatView` preserves each outgoing request separately from the server transcript and composer draft. The snapshot includes text, uploaded file URLs/names, the originating conversation and the displayed approval-token order. In-app route changes retain requests and drafts in memory. Reloading, closing the tab, or signing out clears them; this feature is not a persistent offline outbox. The new-chat hero retains its existing send behavior.

Confirmed rejections offer Retry, Edit and Discard. Release-update rejection offers Reload instead of Retry, through a shared Sheet that explains tab-memory loss and offers a downloadable backup of all preserved text and file links. Reload is always an explicit user action. Editing uses the shared Sheet and never replaces a newer composer draft. Retrying allocates a new request ID and reuses all files and the original displayed token order; it does not reselect numbered approval targets. The server's existing unnumbered approval-sweep semantics remain unchanged. Discard removes the local request, not uploaded files or accepted work.

An exception, malformed response, or a POST still pending after 30 seconds becomes Delivery not confirmed. Check delivery only reads `jarvis.chat.pwa_send.check_delivery`; it cannot execute a second send. Late responses cannot overwrite a newer retry or downgrade an already settled result. Delivery reads have a 15-second deadline; timeout unlocks Check delivery without permitting replay, and late read results cannot affect a subsequent check. Normal acceptance retains a conversation/run-scoped busy state until matching lifecycle evidence arrives; fetching the user row alone does not unlock the composer. Normal acceptance is reconciled by the exact returned message ID, never by matching text. Typed confirmations are handled before ordinary ok:false rejection because they can execute without a user-message row or new turn.

If a stale conversation is retargeted to another chat, its destination composer is preserved. A colliding source draft appears in a saved-draft card; Use this draft swaps it with the composer, preserving text and files on both sides. It never sends a request or transfers approval tokens.

Queue feedback is separate from transcript reconciliation. Once a saved user message replaces its local recovery card, an accepted queued turn keeps its status and composer lock. Existing owner-checked admission endpoints discover queued/preparing/ready turns on open, focus and resync, and poll a pending run every 10 seconds. Reads are bounded and fenced against navigation and lifecycle events. Cancellation uses `cancel_queued_turn`; lost responses keep the status and offer a read-only status check. Matching start/terminal events retire queue feedback. Unknown states and failed reads retain the last known status.

## Backend contract and limits

The PWA conversation screen calls `jarvis.chat.pwa_send.send_message`. It keeps the existing send endpoint's gates and behavior, adding a site/user-scoped random-ID claim and a seven-day Redis outcome receipt. Claiming is atomic. Reuse of a retained ID never executes the send twice; a changed payload cannot reuse its outcome. A Frappe after_commit callback publishes the filtered receipt only after the request transaction commits; this wrapper never commits the transaction itself. Rollback or commit failure leaves the claim unknown. Stored fields exclude request text, attachments and tool results. Delivery lookup rechecks access and ownership.

This is a temporary outcome lookup, not a durable idempotency ledger. Pending, unavailable, evicted and expired receipts all mean unknown, never rejection. No automatic replay or fallback to the legacy endpoint is permitted after an uncertain result. If lookup cannot establish the outcome, the card offers Discard and Edit as new message with explicit warnings that the original work may already have executed. Discard is local removal, not cancellation. Edit as new moves text/files into the composer, parks any newer draft separately, drops prior approval selections, and never automatically sends. The UI never claims a timeout rolled back business work. Independent file access/extraction failures after acceptance still use the existing tool/file error paths.

## Deployment and verification

Deploy backend before the new PWA bundle. No schema migration is required. Roll back the client first; other send callers are unchanged. Do not force reload on release-update rejection while the request is held only in tab memory.

- `node --test pwa/src/lib/*.test.js`
- From `frontend`: `npm test -- src/components/PwaSendRecovery.spec.js src/components/PwaChatSendRecovery.spec.js`
- In the bench Python environment with this checkout on PYTHONPATH: `python -m unittest jarvis.tests.test_pwa_send_receipts`
- From `pwa`: `npm run build`

The receipt unit suite mocks Redis and the send boundary. Real ERP actions and Redis outage/restart behavior still require integration validation on a dedicated test site. Component tests use the real recovery card and ChatView with mocked APIs; browser checks should include keyboard focus, Escape, newer-draft preservation, slow HTTP responses and route changes.

Browser captures are documented in the PR description, not committed as repository assets. Shared Sheet tests cover keyboard focus, Escape, nested sheets and scroll cleanup.
