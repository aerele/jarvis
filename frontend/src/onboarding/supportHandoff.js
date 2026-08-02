/**
 * The client-local "offer a human" counter.
 *
 * Pure, so `node --test` runs it with no bundler. Nothing on the server counts a
 * customer's status checks - deliberately: a check is a READ of gateway truth,
 * and a read that mutates a server counter is a write nobody asked for (and one
 * that would then need its own reset rules, generation handling, and race
 * fences). The count lives here instead, keyed to the attempt+generation it is
 * about, and a new intent starts it over.
 *
 * The ceiling exists because ON_HOLD mandates pend forever by design - a bank
 * can leave an e-NACH awaiting approval indefinitely - so "poll until it
 * resolves" has no natural end, and the page must eventually stop implying one
 * and offer to put a person on it instead.
 */

/** Checks against one intent before the support moment is surfaced. */
export const SUPPORT_AFTER_CHECKS = 4;

/** A counter keyed to nothing yet. */
export function emptyCounter() {
	return { key: null, checks: 0 };
}

// The count is per (attempt, generation): the generation advance IS a new
// intent, and a customer who just retried has a live checkout in front of them
// again, so carrying the previous intent's frustration into it would offer
// support before they have even tried to pay. A missing attempt id (a legacy
// control plane) still gets a stable-per-answer key rather than collapsing every
// such answer into one bucket that never resets.
function keyOf({ attemptId, generation } = {}) {
	return `${attemptId || "?"}\u0000${generation == null ? "?" : generation}`;
}

/**
 * Record one status check. Never mutates the counter it is given.
 *
 * @param {{key: string|null, checks: number}} counter
 * @param {{attemptId?: string, generation?: number}} intent
 */
export function recordCheck(counter, intent = {}) {
	const key = keyOf(intent);
	if (!counter || counter.key !== key) {
		return { key, checks: 1 };
	}
	return { key, checks: counter.checks + 1 };
}

/** Has this intent been checked enough times to warrant offering a human? */
export function shouldOfferSupport(counter) {
	return !!counter && counter.checks >= SUPPORT_AFTER_CHECKS;
}

/**
 * The (attempt, generation) key, exported so the orchestrator can persist the
 * check count under the SAME key this module counts by (P2-5) - a refresh
 * between checks then restores the count instead of resetting the support offer.
 * Non-secret by construction: an attempt-id tail plus a generation number, the
 * same identity the recovery card already renders as a masked reference.
 */
export function counterKey(intent) {
	return keyOf(intent);
}
