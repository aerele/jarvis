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

// VR5-3 / VR4-2: merge a scope's origin-kept failed optimistic bubbles into the rendered `messages`
// array WITHOUT duplicating any already present (dedupe by bubble name). BOTH loadConversation() (on a
// switch/return) and newChat() call this: newChat() promotes the new-chat sentinel to the real id and
// then resets `messages`, but the route watcher no-ops (currentId already equals the new id), so
// loadConversation()'s peek never runs — a failed bubble + its voice-release token would stay INVISIBLE
// (and its leave guard unresolved) until some later away-and-back. Sharing one merge keeps newChat()
// hydrating "exactly as loadConversation does". Pure + array-returning so it is node-testable; returns
// the input array unchanged (same ref) when there is nothing new to inject.
export function injectPendingBubbles(messages, pendingBubbles) {
	const base = Array.isArray(messages) ? messages : [];
	if (!pendingBubbles || !pendingBubbles.length) return base;
	const have = new Set(base.map((m) => m && m.name));
	const add = pendingBubbles.filter((b) => b && b.name && !have.has(b.name));
	return add.length ? [...base, ...add] : base;
}

// R4-2 (VR4-2): an ORIGIN-conversation-scoped store of failed optimistic bubbles, DECOUPLED from the
// rendered `messages` array. When a send is rejected while the user has navigated to a DIFFERENT
// conversation, loadConversation() has already REPLACED `messages`, so a bubble kept only there is
// gone — and with it the voice-release TOKEN it carries, stranding the committed clips behind an
// armed guard with no resend action. Keeping the failed bubble here, keyed by its ORIGIN scope (a
// real conversation id, or the new-chat sentinel), lets loadConversation() re-inject it when the
// user returns to that conversation — so the resend affordance + token survive a mid-send switch,
// independent of what is on screen now. It is a plain store (not reactive): callers write into the
// reactive `messages` array from it. Entries are removed only when the send is RESOLVED (a
// successful resend or an explicit discard) — never on mere navigation — so they survive repeated
// switching. reassign() re-keys entries when the sentinel scope is promoted to a real id (R3-2/VR4-3).
export function createPendingSends() {
	const byScope = new Map(); // scopeKey -> Map(bubbleName -> bubble)
	// A stable key that keeps a real id, the sentinel string, and a bare null distinct.
	const _key = (scope) => (scope == null ? "__pending_null__" : String(scope));
	return {
		// Record (or refresh) a failed bubble for its origin scope; idempotent per bubble name.
		add(scope, bubble) {
			if (!bubble || !bubble.name) return;
			const k = _key(scope);
			let m = byScope.get(k);
			if (!m) {
				m = new Map();
				byScope.set(k, m);
			}
			m.set(bubble.name, bubble);
		},
		// This scope's failed bubbles (insertion order), WITHOUT removing them — loadConversation
		// re-injects them on every return while they remain unresolved.
		peek(scope) {
			const m = byScope.get(_key(scope));
			return m ? Array.from(m.values()) : [];
		},
		// Drop a resolved bubble (a successful resend, or an explicit discard) from its scope.
		remove(scope, name) {
			const k = _key(scope);
			const m = byScope.get(k);
			if (!m) return;
			m.delete(name);
			if (!m.size) byScope.delete(k);
		},
		has(scope) {
			const m = byScope.get(_key(scope));
			return !!(m && m.size);
		},
		// Move every entry from `fromScope` to `toScope` when the new-chat sentinel is promoted to
		// its real id, so a bubble that failed under the sentinel re-injects on the real conversation.
		reassign(fromScope, toScope) {
			const fk = _key(fromScope);
			const tk = _key(toScope);
			const src = byScope.get(fk);
			if (!src || !src.size || fk === tk) return;
			let dst = byScope.get(tk);
			if (!dst) {
				dst = new Map();
				byScope.set(tk, dst);
			}
			for (const [name, bubble] of src) if (!dst.has(name)) dst.set(name, bubble);
			byScope.delete(fk);
		},
	};
}
