/**
 * The external-checkout navigation marker (X3 / B2-3).
 *
 * A full-page gateway checkout (the Cashfree autopay mandate) leaves this page,
 * so a session-scoped, non-secret marker records that we left; on the way back a
 * bfcache restore / tab refocus reads it and drives the machine's explicit, safe
 * RETURNED_FROM_CHECKOUT exit (usePaymentFlow.returnFromCheckout), instead of
 * leaving the customer on a frozen "Opening secure checkout…" screen.
 *
 * The marker used to be a bare "1" cleared only inside checkout states, so a
 * leftover from attempt N could drive a returnFromCheckout during attempt N+1's
 * LIVE in-page sheet - dropping a real confirm (the reviewer's Probe-B). The fix
 * is here: the marker is STAMPED with the attempt id, and the honour decision is a
 * pure, testable function that refuses a marker whose attempt does not match the
 * live sheet. The view also clears it unconditionally on mount when no sheet is
 * open, so a stale marker cannot survive into a later attempt at all.
 */

export const CHECKOUT_NAV_KEY = "jarvis-onboarding-checkout-nav";

/**
 * Should a checkout-return be honoured right now?
 *
 * @param {object} args
 * @param {string} args.marker   the stored marker (an attempt id, or "" when none)
 * @param {boolean} args.inCheckout  is the machine in checkout_open/confirming?
 * @param {string|null} args.attemptId  the live intent's attempt id
 * @returns {boolean}
 */
export function shouldHonorCheckoutReturn({ marker, inCheckout, attemptId } = {}) {
	if (!marker) return false;
	// A return only makes sense while a sheet is actually open/confirming - a stray
	// pageshow in any other state is not a checkout return.
	if (!inCheckout) return false;
	// Honour ONLY a marker we can POSITIVELY match to the live sheet's attempt.
	// A marker stamped for a DIFFERENT attempt is attempt N's stale leftover, and a
	// live attemptId that is null/"" cannot prove the marker is ours either - so
	// honouring on an unknown attempt (the X3 residual) let a stale marker drive a
	// returnFromCheckout that dropped attempt N+1's LIVE confirm. Fail closed: no
	// verifiable attempt match, no honour. A genuine bfcache-restored sheet carries
	// its own attemptId, so this refuses only the unverifiable case.
	const live = attemptId == null ? "" : String(attemptId);
	if (!live) return false;
	return marker === live;
}
