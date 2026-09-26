// Refreshes the Approvals badge when the caller's next chat card crosses the board
// delay (`pending_badge.next_in`, seconds from the server), so it shows on time
// rather than on the next 60 s poll. One timer: each schedule replaces it.

export const BADGE_SLACK_MS = 1000;

export function createBadgeTimer(refresh, clock = globalThis) {
	let id = null;
	function clear() {
		if (id !== null) clock.clearTimeout(id);
		id = null;
	}
	function schedule(nextIn) {
		clear();
		const s = Number(nextIn);
		if (nextIn == null || !Number.isFinite(s) || s < 0) return;
		id = clock.setTimeout(() => {
			id = null;
			refresh();
		}, s * 1000 + BADGE_SLACK_MS);
	}
	return { schedule, clear };
}
