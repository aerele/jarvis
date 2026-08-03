/**
 * The payment machine's transition telemetry sink (X6 / B2-7).
 *
 * usePaymentFlow emits a PII-free event on every state change
 * (`payment_transition`) and on every counted illegal transition
 * (`payment_illegal_transition`). These are forwarded to the shared client error
 * reporter (errorReporter) - which bounds itself with a per-session budget
 * (MAX_PER_SESSION) across every surface. Each forwarded event here consumes one
 * slot of THAT shared budget, so an unbounded payment sink (poll loops, cooldown
 * ticks, a customer who retries a lot, or a machine that wedges into an illegal-
 * transition loop) could crowd out real errors from the rest of the SPA.
 *
 * So both kinds get their own caps HERE, on top of the shared budget:
 *   - ordinary transitions: a small cap (a handful is plenty to reconstruct a
 *     session's shape);
 *   - illegal transitions: a much larger cap, because they are the signal that
 *     actually matters - but STILL a cap, so a runaway machine cannot spend the
 *     entire shared error budget by itself (an unbounded bypass could).
 *
 * Both caps are per SESSION, not per mount. The counters live at MODULE scope so
 * that remounting the pay page (a route bounce, a retry that unmounts and
 * remounts, a returned bfcache) cannot silently reset the budget - which is
 * exactly what a per-closure counter did, letting each remount spend the cap
 * afresh. A full page load (a genuinely new session) resets the module, and tests
 * reset it explicitly via `_resetTelemetryCapsForTest`.
 */

// Ordinary `payment_transition` events reported per SESSION before we stop.
export const TRANSITION_REPORT_CAP = 20;
// Illegal transitions reported per SESSION before we stop. Far larger than the
// ordinary cap - they are the real diagnostic signal - but bounded, so a machine
// stuck emitting illegal transitions cannot exhaust the shared error budget.
export const ILLEGAL_REPORT_CAP = 50;

// Per-SESSION counters at module scope (see the header): they persist across
// every reporter created during the page's lifetime and reset only on a full page
// load or the test hook below.
let transitionsSent = 0;
let illegalsSent = 0;

/** Test-only: reset the per-session counters so each test starts from zero. */
export function _resetTelemetryCapsForTest() {
	transitionsSent = 0;
	illegalsSent = 0;
}

/**
 * Build the telemetry sink passed to createPaymentFlow.
 *
 * @param {(ctx: object) => void} report  the errorReporter `report` function
 * @param {{cap?: number, illegalCap?: number}} [opts]
 * @returns {(ev: object) => void}
 */
export function makeTelemetryReporter(report, opts = {}) {
	const cap = opts.cap == null ? TRANSITION_REPORT_CAP : opts.cap;
	const illegalCap = opts.illegalCap == null ? ILLEGAL_REPORT_CAP : opts.illegalCap;
	return (ev) => {
		try {
			if (!ev) return;
			const forward = () =>
				report({
					surface: "onboarding_payment",
					error_code: ev.event,
					message: JSON.stringify(ev),
				});
			// Illegal transitions are the signal, so they get their own (large) cap -
			// but a cap all the same, bounded per session so a runaway machine cannot
			// eat the whole shared error budget.
			if (ev.event === "payment_illegal_transition") {
				if (illegalsSent >= illegalCap) return;
				illegalsSent += 1;
				forward();
				return;
			}
			// Ordinary transitions are bounded separately, also per session.
			if (transitionsSent >= cap) return;
			transitionsSent += 1;
			forward();
		} catch (e) {
			/* telemetry must never break the flow */
		}
	};
}
