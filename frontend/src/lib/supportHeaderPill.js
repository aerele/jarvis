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
// Straight to the one ticket's thread ONLY when both numbers agree: the
// backend's awaiting_count total is exactly 1, AND the store's already-loaded
// ticket list (loadTickets() - never fetched here, so this stays cheap) shows
// exactly one row matching it. `tickets`/`isAwaiting` are passed in rather
// than importing the store, so this stays pure and testable with a plain
// array + a plain predicate. Anything short of that confident match - the
// list not loaded yet, or a mismatch between the two counts - falls back to
// the full list rather than guessing which ticket, or making a fresh network
// call just to resolve one click.
export function supportAwaitingRoute(count, tickets, isAwaiting) {
	if (count === 1) {
		const matches = (tickets || []).filter((t) => isAwaiting(t.status));
		if (matches.length === 1) {
			return { name: "SupportTicket", params: { ticket: matches[0].name } };
		}
	}
	return { name: "Support", query: { status: "awaiting" } };
}
