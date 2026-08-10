// One source of truth for the parked confirmation-card order in the Desk widget.
//
// A typed "confirm 2" selects by the number the user sees, and the server
// resolves that number against the tokens this client sends in this order, so
// numbered cards MUST order by (expires_at ascending, then token by CODE UNIT) -
// the same key the server uses (`sorted(key=(expires_at, token))`).
//
// Code unit, not localeCompare: locale rules disagree on mixed-case tokens and
// would renumber a card between screen and server, i.e. run the wrong write.
//
// This widget is a separate build from the SPA/PWA, so the comparator is
// duplicated here and pinned by its own test (pending_order.test.mjs). The bug
// this guards against was not the comparator itself but a card built WITHOUT
// expires_at, so the tests also assert the field survives onto the item.

export function comparePendingCards(a, b) {
  return (
    (a.expires_at || 0) - (b.expires_at || 0) ||
    (a.token < b.token ? -1 : a.token > b.token ? 1 : 0)
  );
}

export function sortPendingCards(cards) {
  return [...(cards || [])].sort(comparePendingCards);
}
