// voiceSendGlue — the PURE composer↔queue↔send glue that ChatView.vue delegates to for the two
// send-path decisions the queue-unit tests never exercised: promoting an unsaved new-chat scope to
// its real conversation id, and recovering a REJECTED send without losing voice audio. It is
// extracted (like voiceChunkQueue.js / eventFence.js) so those decisions are unit-testable with
// `node --test` WITHOUT mounting the (un-mountable) ChatView single-file component — the tests drive
// these functions with a mock queue + a plain drafts object + a mock send outcome. ChatView imports
// and calls them, so the tests exercise the REAL code, not a reimplementation.
//
// Codex round-3 findings closed here:
//   R3-2 — an implicit new-chat id-adoption must migrate ALL surviving state (queue records + their
//          mirror, the stashed draft, and the active recording take) off the _NEW_CHAT_SCOPE
//          sentinel BEFORE any later send/navigation, or a clip that commits late under the sentinel
//          strands there forever (a real-scope send/ack never matches it → guard permanently armed,
//          recovery routed to the wrong draft).
//   R3-3 — a rejected failed-bubble RESEND must preserve/recreate a bubble carrying the SAME
//          voiceAck (or restore text + provenance to the composer), never leave `done` records with
//          an armed guard and no action.

// R3-2: promote the unsaved new-chat composer to a REAL conversation id. Migrates, from `fromScope`
// to `toId`, in one place used by BOTH newChat() and the send-path id adoption:
//   * DRAFT ownership — the sentinel's stashed draft becomes the real conversation's draft (never
//     clobbering an existing one), and the sentinel key is dropped;
//   * the active RECORDING take scope — returned so the caller re-points it, so a clip emitted after
//     promotion enqueues under the real id;
//   * every surviving QUEUE record + its mirror — via queue.reassignScope(fromScope, toId), so a
//     later real-scope captureSent/acknowledge releases them (and a reload recovers them under the
//     real id) instead of stranding them under the sentinel.
// Idempotent and a no-op when `toId` is falsy or already `fromScope`. Returns the take scope the
// caller should adopt (unchanged when it wasn't the sentinel).
export function promoteNewChatScope({ queue, drafts, fromScope, toId, takeScope }) {
	if (!toId || toId === fromScope) return takeScope;
	if (drafts && Object.prototype.hasOwnProperty.call(drafts, fromScope)) {
		// Move the sentinel draft onto the real id unless the target already owns one — never
		// clobber a draft the real conversation already had — then drop the stale sentinel key.
		if (!Object.prototype.hasOwnProperty.call(drafts, toId) && drafts[fromScope])
			drafts[toId] = drafts[fromScope];
		delete drafts[fromScope];
	}
	const nextTake = takeScope === fromScope ? toId : takeScope;
	// Migrate the retained voice records + their IndexedDB mirror last so, even if this throws,
	// the draft/take are already coherent. reassignScope also re-points each moved clip's
	// conversationId, so a clip that commits AFTER this routes onCommit to the real conversation.
	if (queue) queue.reassignScope(fromScope, toId);
	return nextTake;
}

// R3-3: decide how to recover a send the server REJECTED ({ok:false} — single-flight, usage_limit,
// subscription_suspended, …) so no voice audio strands behind an armed leave guard with no action.
// The decision is the SAME across every rejection reason (the reason only changes the surfaced
// message, never whether the audio is protected):
//   * keepBubble — a failed-bubble RESEND (not fromMain) whose optimistic bubble carries a voiceAck
//     has NO composer text to fall back to (fromMain=false never restores `input`): KEEP its bubble
//     as a failed one carrying the SAME token so the user can resend again and eventually release
//     the committed clips. Dropping it (the old path) stranded those `done` records behind an armed
//     leave guard with no chip and no action.
//   * restoreText — a MAIN-composer send (fromMain) drops its bubble and restores its text to the
//     composer, where its still-retained voice records stay re-captureable on the next send.
// A MAIN send is left EXACTLY as rounds 1/2 (drop + restore); only the resend-with-voice path
// changes. A programmatic non-voice send drops the bubble as before.
export function planRejectedSend({ fromMain, bubbleVoiceAck }) {
	const hasVoice = !!(bubbleVoiceAck && bubbleVoiceAck.length);
	if (!fromMain && hasVoice) return { keepBubble: true, restoreText: false };
	return { keepBubble: false, restoreText: !!fromMain };
}
