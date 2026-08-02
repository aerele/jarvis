/**
 * The payment machine's transition telemetry sink (X6 / B2-7).
 *
 * usePaymentFlow emits a PII-free event on every state change
 * (`payment_transition`) and on every counted illegal transition
 * (`payment_illegal_transition`). These are forwarded to the shared client error
 * reporter (errorReporter) - which bounds itself with a SINGLE per-session budget
 * (MAX_PER_SESSION) across every surface. A chatty payment page (poll loops,
 * cooldown ticks, a customer who retries a lot) could otherwise spend that whole
 * budget on ordinary transitions and crowd out real errors from the rest of the
 * SPA.
 *
 * So transitions get their OWN small per-session cap here, separate from the error
 * budget. Illegal transitions are the ones that actually matter (a bug in the
 * machine), so they BYPASS the cap and are always reported.
 */

// Ordinary `payment_transition` events reported per session before we stop. Small
// on purpose: a handful of transitions is plenty to reconstruct a session's shape;
// the error budget is reserved for actual errors. Illegal transitions ignore this.
export const TRANSITION_REPORT_CAP = 20;

/**
 * Build the telemetry sink passed to createPaymentFlow.
 *
 * @param {(ctx: object) => void} report  the errorReporter `report` function
 * @param {{cap?: number}} [opts]
 * @returns {(ev: object) => void}
 */
export function makeTelemetryReporter(report, opts = {}) {
	const cap = opts.cap == null ? TRANSITION_REPORT_CAP : opts.cap;
	let transitionsSent = 0;
	return (ev) => {
		try {
			if (!ev) return;
			const forward = () =>
				report({
					surface: "onboarding_payment",
					error_code: ev.event,
					message: JSON.stringify(ev),
				});
			// Illegal transitions always report - they are the signal, not the noise.
			if (ev.event === "payment_illegal_transition") {
				forward();
				return;
			}
			// Ordinary transitions are bounded separately from the error budget.
			if (transitionsSent >= cap) return;
			transitionsSent += 1;
			forward();
		} catch (e) {
			/* telemetry must never break the flow */
		}
	};
}
