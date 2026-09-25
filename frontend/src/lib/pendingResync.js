// Pure reconcile logic behind resyncPendingConfirmations (P0c in-flight
// suppression). ChatView.vue has no component-mount test tooling, so this is
// extracted to be directly testable.
//
// `existing` is the current pendingActions queue; `conversationId` scopes the
// reconcile to one conversation (other conversations' cards pass through
// untouched); `items` is the server's authoritative live set for this
// conversation; `inflightTokens` are tokens with a Confirm/Discard RPC
// currently in flight. A stale resync read racing that RPC's own removal must
// never resurrect a card the user already acted on, so an in-flight token is
// excluded from `toAdd` even when the server still (momentarily) lists it.
//
// Returns { kept, toAdd }:
//   kept  - the survivors of `existing`: cards for OTHER conversations
//           unchanged; a BUSY card for this conversation kept regardless of
//           `items` (an in-flight action is never yanked from under itself);
//           everything else for this conversation dropped unless the server
//           still lists it.
//   toAdd - the `items` entries not already in `kept` and not in-flight, in
//           order, ready for the caller to shape into queue entries.
export function reconcilePending(existing, conversationId, items, inflightTokens) {
	const live = new Set((items || []).map((it) => it.token));
	const kept = (existing || []).filter(
		(pa) => pa.conversation !== conversationId || pa.busy || live.has(pa.token)
	);
	const keptTokens = new Set(kept.map((pa) => pa.token));
	const toAdd = [];
	for (const it of items || []) {
		if (!it.token) continue;
		if (inflightTokens && inflightTokens.has(it.token)) continue;
		if (keptTokens.has(it.token)) continue;
		keptTokens.add(it.token);
		toAdd.push(it);
	}
	return { kept, toAdd };
}
