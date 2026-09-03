// Pure helpers for the chat header's support-button count pill (ChatView.vue's
// jv-support-btn) - the header-side sibling of lib/supportCopyFormat.js, which
// already extracts that same button's copy-to-ticket formatting for the same
// reason: dependency-free logic is unit-testable without mounting the view.

// The pill and the button's title/aria-label cap the SAME way: single digits
// read fine at 17px, but a wide count would blow past the button's own 32px
// box, so anything double-digit reads as "9+" rather than truncating oddly.
export function supportPillLabel(count) {
	const n = Number(count) || 0;
	if (n <= 0) return "";
	return n > 9 ? "9+" : String(n);
}

// The plain "N ticket(s) awaiting your reply" phrase, shared by the button's
// title (as-is) and its aria-label (prefixed with "Support, " at the call
// site) - one singular/plural rule instead of two copies drifting apart.
export function supportAwaitingPhrase(count) {
	const n = Number(count) || 0;
	return `${n} ${n === 1 ? "ticket" : "tickets"} awaiting your reply`;
}

// Where the header menu's "Tickets awaiting reply" row sends the viewer.
//
// A previous version routed straight to a ticket's thread when the count was
// exactly 1 and the store's already-loaded ticket list agreed on which one.
// Dropped: awaitingCount is a 60s-polled number (UserMenu's timer) while
// store.tickets is only populated by an actual visit to a Support page and is
// never re-fetched here, so the two can drift - a customer could reply on
// another tab, or a second ticket could get a reply between the poll and the
// click, and the "confident" single match would then route to a ticket that
// is no longer (or never uniquely) the one awaiting a reply. The list route
// is always correct, so it is the only route.
export function supportAwaitingRoute() {
	return { name: "Support", query: { status: "awaiting" } };
}
