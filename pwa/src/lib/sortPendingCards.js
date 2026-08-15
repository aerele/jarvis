// One source of truth for the parked confirmation-card order on the PWA.
//
// A typed "confirm 2" selects by the number the user sees, and the server
// resolves that number against the tokens this client sends in this order, so
// numbered cards MUST order by (expires_at ascending, then token by CODE UNIT) -
// the same key the server uses (`sorted(key=(expires_at, token))`).
//
// Code unit, not localeCompare: locale rules disagree on mixed-case tokens and
// would renumber a card between screen and server, i.e. run the wrong write.
//
// Duplicated per client build (SPA/PWA/Desk cannot share a module); each copy is
// pinned by its own test so they cannot drift. See sortPendingCards.test.js.
export function comparePendingCards(a, b) {
	return (
		(a.expires_at || 0) - (b.expires_at || 0) ||
		(a.token < b.token ? -1 : a.token > b.token ? 1 : 0)
	);
}

export function sortPendingCards(cards) {
	return [...(cards || [])].sort(comparePendingCards);
}
