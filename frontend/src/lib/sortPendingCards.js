// One source of truth for the parked confirmation-card order on THIS client.
//
// A typed "confirm 2" selects by the number the user sees: the server binds it to
// the token this client showed in that position (approval_tokens), so every surface
// that shows numbered cards orders them stably, by (created_at ascending, then
// token by CODE UNIT). created_at (P0c) is the mint time, stable even under a
// future non-uniform TTL - unlike expires_at, kept only as a fallback for a card
// minted before created_at existed (a mixed deploy).
//
// Code unit, not localeCompare / Intl.Collator: locale rules disagree on
// mixed-case tokens, so the clients (and the server's own listing) would number
// the same cards differently.
//
// The SPA, PWA and Desk widget are separate builds and cannot import one shared
// module, so this file is duplicated per client. Each copy is pinned by its own
// unit test (sortPendingCards.spec.js) so the three cannot silently drift - the
// forked, untested copies are exactly how the Desk-widget ordering bug shipped.
function sortEpoch(c) {
	return c.created_at ?? c.expires_at ?? 0;
}

export function comparePendingCards(a, b) {
	return sortEpoch(a) - sortEpoch(b) || (a.token < b.token ? -1 : a.token > b.token ? 1 : 0);
}

export function sortPendingCards(cards) {
	return [...(cards || [])].sort(comparePendingCards);
}
