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
