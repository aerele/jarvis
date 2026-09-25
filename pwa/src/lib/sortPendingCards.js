// One source of truth for the parked confirmation-card order on the PWA.
//
// A typed "confirm 2" selects by the number the user sees: the server binds it to
// the token this client showed in that position (approval_tokens), so numbered
// cards order stably, by (created_at ascending, then token by CODE UNIT) - the
// key every client (and the server's own listing) uses. created_at (P0c) is the
// mint time; expires_at is only the fallback for a card minted before created_at
// existed (a mixed deploy).
//
// Code unit, not localeCompare: locale rules disagree on mixed-case tokens and
// would number the same cards differently from client to client.
//
// Duplicated per client build (SPA/PWA/Desk cannot share a module); each copy is
// pinned by its own test so they cannot drift. See sortPendingCards.test.js.
function sortEpoch(c) {
	return c.created_at ?? c.expires_at ?? 0;
}

export function comparePendingCards(a, b) {
	return sortEpoch(a) - sortEpoch(b) || (a.token < b.token ? -1 : a.token > b.token ? 1 : 0);
}

export function sortPendingCards(cards) {
	return [...(cards || [])].sort(comparePendingCards);
}
