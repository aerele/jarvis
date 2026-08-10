// One source of truth for the parked confirmation-card order on THIS client.
//
// A typed "confirm 2" selects by the number the user sees, and the server
// resolves that number against the tokens this client sends in this order, so
// every surface that shows numbered cards MUST order them the way the server
// does: by (expires_at ascending, then token by CODE UNIT).
//
// Code unit, not localeCompare / Intl.Collator: the server tiebreaks with
// Python's byte order (`sorted(key=(expires_at, token))`), and locale rules
// disagree on mixed-case tokens - which would number cards one way on screen
// and another on the server, i.e. run the wrong write.
//
// The SPA, PWA and Desk widget are separate builds and cannot import one shared
// module, so this file is duplicated per client. Each copy is pinned by its own
// unit test (sortPendingCards.spec.js) so the three cannot silently drift - the
// forked, untested copies are exactly how the Desk-widget ordering bug shipped.
export function comparePendingCards(a, b) {
	return (
		(a.expires_at || 0) - (b.expires_at || 0) ||
		(a.token < b.token ? -1 : a.token > b.token ? 1 : 0)
	);
}

export function sortPendingCards(cards) {
	return [...(cards || [])].sort(comparePendingCards);
}
