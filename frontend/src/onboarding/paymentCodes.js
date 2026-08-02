/**
 * The pay page's vocabulary: every code the bench facade can hand this surface,
 * and the sentence and actions it renders for it.
 *
 * Pure on purpose - no Vue, no imports, no `@/` alias - so `node --test` runs it
 * with no bundler (see vitest.config.js on the *.test.js / *.spec.js split).
 *
 * The list mirrors `jarvis/onboarding_contract.py` lines 46-102, which is the
 * source of truth: 12 codes admin publishes plus 6 the bench facade adds for
 * failures that never reached admin's contract at all. A code with no row here
 * is the exact defect this table exists to remove - a wizard with no copy for a
 * state renders whatever state it happened to be left in, which is how a
 * declined card came to show a reconnect screen for a code nobody ever sent.
 *
 * Two rules the table itself enforces, both asserted in paymentCodes.test.js:
 *
 *   - **Status first.** Wherever both recovery actions are offered, Check comes
 *     before Initiate. The customer's risk here is asymmetric: checking a
 *     payment that already succeeded costs a round trip, while paying again for
 *     one that already succeeded costs them money.
 *   - **No internals.** Never an exception class, never a traceback, never an
 *     HTTP status read out loud. The page keys on the code and speaks English.
 */

// --------------------------------------------------------------------------
// admin's vocabulary (append-only; a shipped code's meaning never changes)
// --------------------------------------------------------------------------
export const CODES = {
	ACCOUNT_ALREADY_EXISTS: "ACCOUNT_ALREADY_EXISTS",
	SIGNUP_VERIFICATION_REQUIRED: "SIGNUP_VERIFICATION_REQUIRED",
	PAYMENT_CONFIRMATION_PENDING: "PAYMENT_CONFIRMATION_PENDING",
	PAYMENT_DECLINED: "PAYMENT_DECLINED",
	PAYMENT_ALREADY_ACTIVE: "PAYMENT_ALREADY_ACTIVE",
	NO_CURRENT_INTENT: "NO_CURRENT_INTENT",
	ACCOUNT_RECONNECT_REQUIRED: "ACCOUNT_RECONNECT_REQUIRED",
	PAYMENT_AUTHORIZED_PENDING_CONFIRM: "PAYMENT_AUTHORIZED_PENDING_CONFIRM",
	INTENT_HANDLE_UNAVAILABLE: "INTENT_HANDLE_UNAVAILABLE",
	SIGNUP_TERMINAL: "SIGNUP_TERMINAL",
	INVALID_REQUEST: "INVALID_REQUEST",
	PAYMENT_CHECK_RATE_LIMITED: "PAYMENT_CHECK_RATE_LIMITED",
	// bench-local: failures that never reached admin's contract
	BENCH_ADMIN_UNREACHABLE: "BENCH_ADMIN_UNREACHABLE",
	BENCH_ADMIN_AUTH_FAILED: "BENCH_ADMIN_AUTH_FAILED",
	BENCH_ADMIN_REJECTED: "BENCH_ADMIN_REJECTED",
	BENCH_RATE_LIMITED: "BENCH_RATE_LIMITED",
	BENCH_NO_SIGNUP_CONTEXT: "BENCH_NO_SIGNUP_CONTEXT",
	BENCH_AWAITING_RECONCILIATION: "BENCH_AWAITING_RECONCILIATION",
};

export const ADMIN_CODES = [
	CODES.ACCOUNT_ALREADY_EXISTS,
	CODES.SIGNUP_VERIFICATION_REQUIRED,
	CODES.PAYMENT_CONFIRMATION_PENDING,
	CODES.PAYMENT_DECLINED,
	CODES.PAYMENT_ALREADY_ACTIVE,
	CODES.NO_CURRENT_INTENT,
	CODES.ACCOUNT_RECONNECT_REQUIRED,
	CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM,
	CODES.INTENT_HANDLE_UNAVAILABLE,
	CODES.SIGNUP_TERMINAL,
	CODES.INVALID_REQUEST,
	CODES.PAYMENT_CHECK_RATE_LIMITED,
];

export const BENCH_CODES = [
	CODES.BENCH_ADMIN_UNREACHABLE,
	CODES.BENCH_ADMIN_AUTH_FAILED,
	CODES.BENCH_ADMIN_REJECTED,
	CODES.BENCH_RATE_LIMITED,
	CODES.BENCH_NO_SIGNUP_CONTEXT,
	CODES.BENCH_AWAITING_RECONCILIATION,
];

/** The recovery affordances a row may ask for. */
export const ACTIONS = {
	/** Ask admin to reconcile gateway truth for the current intent. Never charges. */
	CHECK: "check",
	/** Create or reuse a checkout intent and open the gateway. May charge. */
	INITIATE: "initiate",
	/** Finish a payment the gateway has already authorized. Never a second intent. */
	CONFIRM: "confirm",
	/** Paid: leave the payment step. */
	CONTINUE: "continue",
	/** Email a code and connect this site to an account that already paid. */
	RECONNECT: "reconnect",
	/** Re-poll after the customer clicks the magic link. */
	VERIFY: "verify",
	/** A human. Offered on terminal states and after the client-local check ceiling. */
	SUPPORT: "support",
	/** Back to the top of the wizard - nothing exists to recover. */
	RESTART: "restart",
};

/**
 * `role` for the region that renders the row.
 *
 * plan 02 §Accessibility: `status` for pending (polite, does not interrupt),
 * `alert` only for an ACTIONABLE failure. A pending payment announced as an
 * alert on every poll is the screen-reader equivalent of a flashing banner.
 */
export const TONE = { STATUS: "status", ALERT: "alert", SUCCESS: "success" };

/** The row for a code this build has never heard of - additive admin releases. */
export const UNKNOWN_COPY = Object.freeze({
	headline: "We could not determine the payment status.",
	body: "Nothing has been assumed about your payment. Check the status to see where it stands.",
	tone: TONE.STATUS,
	actions: [ACTIONS.CHECK],
});

const TABLE = {
	// ---- admin ----------------------------------------------------------
	[CODES.ACCOUNT_ALREADY_EXISTS]: {
		headline: "This email and company already have an account.",
		body: "If that account is yours, reconnect this site to it - there is nothing to pay again.",
		tone: TONE.ALERT,
		actions: [ACTIONS.RECONNECT, ACTIONS.RESTART],
	},
	[CODES.SIGNUP_VERIFICATION_REQUIRED]: {
		headline: "Verify your email to continue.",
		body: "Click the link we sent you, then come back here. Nothing is charged until you do.",
		tone: TONE.STATUS,
		actions: [ACTIONS.VERIFY],
	},
	[CODES.PAYMENT_CONFIRMATION_PENDING]: {
		headline: "We have not confirmed this payment.",
		body: "If money was deducted, check the status before starting another payment.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK, ACTIONS.INITIATE],
	},
	[CODES.PAYMENT_DECLINED]: {
		headline: "The payment was declined. No payment was confirmed.",
		body: "You can start a new payment. Check the status first if you think money left your account.",
		tone: TONE.ALERT,
		actions: [ACTIONS.CHECK, ACTIONS.INITIATE],
	},
	[CODES.PAYMENT_ALREADY_ACTIVE]: {
		headline: "Payment is confirmed.",
		body: "Nothing more is owed. We are continuing your setup.",
		tone: TONE.SUCCESS,
		actions: [ACTIONS.CONTINUE],
	},
	[CODES.NO_CURRENT_INTENT]: {
		headline: "There is no checkout open for this signup.",
		body: "Nothing has been charged. Start the payment whenever you are ready.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK, ACTIONS.INITIATE],
	},
	[CODES.ACCOUNT_RECONNECT_REQUIRED]: {
		headline: "This paid account belongs to an existing setup.",
		body: "Nothing to pay again - we will email a code to confirm it is you and connect this site to it.",
		tone: TONE.STATUS,
		actions: [ACTIONS.RECONNECT],
	},
	[CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM]: {
		headline: "Your payment is authorized.",
		body:
			"The gateway has your authorization and we only need to finish it off. " +
			"Do not start another payment - that would authorize a second one.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CONFIRM, ACTIONS.CHECK],
	},
	[CODES.INTENT_HANDLE_UNAVAILABLE]: {
		headline: "The payment provider did not open a checkout session.",
		body: "Nothing was charged. This is usually temporary.",
		tone: TONE.ALERT,
		actions: [ACTIONS.CHECK, ACTIONS.INITIATE],
	},
	[CODES.SIGNUP_TERMINAL]: {
		headline: "This signup cannot be continued.",
		body: "The subscription it belongs to has ended. Renew from your account, or talk to us and we will sort it out.",
		tone: TONE.ALERT,
		actions: [ACTIONS.SUPPORT],
	},
	[CODES.INVALID_REQUEST]: {
		headline: "That payment request could not be accepted.",
		body: "Nothing was charged. Starting a fresh attempt clears it.",
		tone: TONE.ALERT,
		// A refused request bought nothing, so the recovery is a NEW attempt - and
		// the refused receipt must never be sent again. See mintFreshKey.
		actions: [ACTIONS.INITIATE],
		mintFreshKey: true,
	},
	[CODES.PAYMENT_CHECK_RATE_LIMITED]: {
		headline: "We are checking with your payment provider.",
		// Deliberately says NOTHING about the payment. A 429 is "you asked too
		// often", and rendering it as a verdict is how a backoff becomes a second
		// charge.
		body: "That was a lot of checks in a row. Give it a moment and check again.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK],
	},
	// ---- bench-local ----------------------------------------------------
	[CODES.BENCH_ADMIN_UNREACHABLE]: {
		headline: "We could not reach the payment service.",
		body: "This says nothing about your payment. Try the check again in a moment.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK],
	},
	[CODES.BENCH_ADMIN_AUTH_FAILED]: {
		headline: "This site could not sign in to your account.",
		body: "Your payment is not affected. We need to look at this with you.",
		tone: TONE.ALERT,
		actions: [ACTIONS.SUPPORT],
	},
	[CODES.BENCH_ADMIN_REJECTED]: {
		headline: "The payment service refused this request.",
		body: "Nothing was charged. Check the status, or start a new payment.",
		tone: TONE.ALERT,
		actions: [ACTIONS.CHECK, ACTIONS.INITIATE],
	},
	[CODES.BENCH_RATE_LIMITED]: {
		headline: "Too many requests from this site just now.",
		body: "Nothing is wrong with your payment. Wait a moment and try again.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK],
	},
	[CODES.BENCH_NO_SIGNUP_CONTEXT]: {
		// The day-one answer. It is reached by somebody whose only action was to
		// open the page, so it must read like a welcome and never like an
		// incident - the shape this replaced sent a first-time visitor to
		// "admin authentication failed; contact support".
		headline: "No signup has been started on this site yet.",
		body: "Pick a plan and enter your details to get going.",
		tone: TONE.STATUS,
		actions: [ACTIONS.RESTART],
	},
	[CODES.BENCH_AWAITING_RECONCILIATION]: {
		// Its OWN row, not a variant: this is the bench refusing before the
		// network, and the customer is entitled to know a person is on it.
		headline: "We are still confirming a payment on this signup.",
		body:
			"The provider is holding a payment we could not match to this signup automatically, " +
			"and someone here is placing it. Nothing more is owed - please do not pay again.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK],
	},
};

/**
 * The reconciliation FLAG's variant of the pending row.
 *
 * Deliberately NOT a code of its own: admin answers the ordinary
 * PAYMENT_CONFIRMATION_PENDING when money is parked, because a decline there
 * would invite a second payment. The flag is the only thing that distinguishes
 * the two, and it is the flag - never the code - that suppresses Pay.
 */
const PENDING_AWAITING_RECONCILIATION = Object.freeze({
	headline: "We are still confirming your payment.",
	body:
		"The provider is holding a payment we could not match to this signup automatically, " +
		"and someone here is placing it. Nothing more is owed - please do not pay again.",
	tone: TONE.STATUS,
	actions: [ACTIONS.CHECK],
});

for (const key of Object.keys(TABLE)) Object.freeze(TABLE[key]);

/**
 * The row for one code, or the honest unknown row.
 *
 * @param {string} code
 * @param {{awaitingReconciliation?: boolean}} [flags]
 */
export function copyFor(code, flags = {}) {
	if (flags && flags.awaitingReconciliation && code === CODES.PAYMENT_CONFIRMATION_PENDING) {
		return PENDING_AWAITING_RECONCILIATION;
	}
	return TABLE[code] || UNKNOWN_COPY;
}

/** Human labels for the affordances. One place, so two screens cannot disagree. */
export const ACTION_LABELS = {
	[ACTIONS.CHECK]: "Check payment status",
	[ACTIONS.INITIATE]: "Initiate payment again",
	[ACTIONS.CONFIRM]: "Confirm payment",
	[ACTIONS.CONTINUE]: "Continue",
	[ACTIONS.RECONNECT]: "Reconnect this site",
	[ACTIONS.VERIFY]: "I've verified my email",
	[ACTIONS.SUPPORT]: "Contact support",
	[ACTIONS.RESTART]: "Start again",
};
