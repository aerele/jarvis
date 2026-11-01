// Pure merge logic behind loadPending's rows+backstop reconciliation (P0c
// in-flight suppression). Extracted for testability: ChatView.vue has no
// component-mount test tooling (no vitest/@vue/test-utils in this app).
//
// `base` is whatever is already on screen (passed through UNFILTERED - it is
// only kept when a fresh read failed/was unavailable, so there is nothing
// fresher to prefer). `freshSources` are ordered lowest-to-highest priority
// reads of CURRENT server truth (durable rows, then the Redis backstop); the
// first occurrence of a token wins, matching loadPending's own Map insertion
// order. A token in `inflightTokens` is dropped from every fresh source - a
// Confirm/Discard RPC in flight for it must not be resurrected by a stale
// resync read racing the RPC's own removal (see ChatView.vue's inflightToken).
export function mergePendingSources(base, freshSources, inflightTokens) {
	const byToken = new Map();
	for (const c of base || []) {
		if (c.token && !byToken.has(c.token)) byToken.set(c.token, c);
	}
	for (const list of freshSources || []) {
		for (const c of list || []) {
			if (!c.token || (inflightTokens && inflightTokens.has(c.token))) continue;
			if (!byToken.has(c.token)) byToken.set(c.token, c);
		}
	}
	return [...byToken.values()];
}

// D1: a typed "no" (ChatView.vue's send(), discardedTokens) settles a card by
// filtering it out of `pending.value`, but messages.value (fromRows' source)
// keeps tool_status "pending" until the next load(). A resync racing that
// window must not rebuild the card from that stale row. ChatView folds every
// newly-settled token in here (persisting past the single RPC inflightToken
// covers) and passes the running set as loadPending's exclusion, alongside
// inflightToken, so mergePendingSources never re-admits either kind.
export function withExcluded(exclusions, tokens) {
	const out = new Set(exclusions || []);
	for (const t of tokens || []) if (t) out.add(t);
	return out;
}

// Realtime confirmations use three event shapes: one direct action:pending
// event, or the pending list attached to run:end and run:error. Keep the
// admission decision in one pure helper so every live path applies the same
// settled-token and in-flight exclusions as the durable resync path.
export function admitRealtimePendingCards(
	current,
	event,
	excludedTokens,
	inflightToken,
	fallbackConversation,
) {
	const incoming =
		event?.kind === "action:pending"
			? [event]
			: Array.isArray(event?.pending)
				? event.pending
				: [];
	const next = [...(current || [])];
	const known = new Set(next.map((card) => card?.token).filter(Boolean));

	for (const card of incoming || []) {
		if (
			!card?.token ||
			card.token === inflightToken ||
			excludedTokens?.has(card.token) ||
			known.has(card.token)
		)
			continue;
		known.add(card.token);
		next.push({
			token: card.token,
			tool: card.tool || "",
			summary: card.summary || "",
			preview: card.preview ?? null,
			conversation: card.conversation || fallbackConversation,
			run_id: card.run_id,
			created_at: card.created_at ?? null,
			expires_at: card.expires_at ?? null,
			seq: card.seq ?? null,
			recent: card.recent !== false,
		});
	}
	return next;
}
