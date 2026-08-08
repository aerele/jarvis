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
	// Admin's parked-money hold on an EXISTING account (billing, not signup): an
	// unresolved incident refuses every initiation until an operator places it.
	PAYMENT_UNDER_REVIEW: "PAYMENT_UNDER_REVIEW",
	// The losing half of a concurrent double-submit: another initiation for this
	// same subscription is mid-flight. A timed WAIT, never a decline.
	INTENT_CREATION_IN_PROGRESS: "INTENT_CREATION_IN_PROGRESS",
	// Admin has no live checkout to report on for this subscription.
	BILLING_NO_CURRENT_INTENT: "BILLING_NO_CURRENT_INTENT",
	// BENCH_SIGNUP_DETAILS_REJECTED - admin refused the customer's OWN submitted
	// details (a malformed GSTIN, an unusable phone number) BEFORE any gateway was
	// contacted. It used to collapse into BENCH_ADMIN_REJECTED, whose copy says "the
	// payment service refused this request" and offers Check status / Initiate
	// again. Every word of that was wrong: no payment service was involved, nothing
	// existed to check, and retrying with the same data failed identically forever.
	// The customer was left in an unrecoverable dead end, three screens past the
	// field that actually caused it.
	BENCH_SIGNUP_DETAILS_REJECTED: "BENCH_SIGNUP_DETAILS_REJECTED",
	// plan-09 WS7 (bench-local): the admin-hosted pay-page cutover.
	//
	// PAYMENT_PAGE_REDIRECT — a token-bearing answer that is navigable (the bench's
	// configured origin is attested against admin's non-navigable digest). The
	// orchestrator top-level-navigates to the pay page from it, so its copy is only
	// ever seen for the instant before the browser leaves.
	PAYMENT_PAGE_REDIRECT: "PAYMENT_PAGE_REDIRECT",
	// BENCH_PAY_ORIGIN_UNCONFIGURED — a token arrived but the bench cannot navigate
	// with it: jarvis_pay_origin is unset/invalid, or its digest did not match
	// admin's attestation. NO FALLBACK (owner decision 4): an honest config hold
	// with a support hint, never a gateway sheet on this origin.
	BENCH_PAY_ORIGIN_UNCONFIGURED: "BENCH_PAY_ORIGIN_UNCONFIGURED",
	// CLIENT_UPGRADE_REQUIRED — a pre-cutover admin answered with raw provider
	// handles and NO token. The bench opens no sheet for those any more; this is
	// the CLIENT_UPGRADE_REQUIRED-style honest hold (§R P0-4).
	CLIENT_UPGRADE_REQUIRED: "CLIENT_UPGRADE_REQUIRED",
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
	CODES.PAYMENT_UNDER_REVIEW,
	CODES.INTENT_CREATION_IN_PROGRESS,
	CODES.BILLING_NO_CURRENT_INTENT,
];

export const BENCH_CODES = [
	CODES.BENCH_ADMIN_UNREACHABLE,
	CODES.BENCH_ADMIN_AUTH_FAILED,
	CODES.BENCH_ADMIN_REJECTED,
	CODES.BENCH_RATE_LIMITED,
	CODES.BENCH_NO_SIGNUP_CONTEXT,
	CODES.BENCH_AWAITING_RECONCILIATION,
	CODES.BENCH_SIGNUP_DETAILS_REJECTED,
	// plan-09 WS7 cutover codes
	CODES.PAYMENT_PAGE_REDIRECT,
	CODES.BENCH_PAY_ORIGIN_UNCONFIGURED,
	CODES.CLIENT_UPGRADE_REQUIRED,
];

/** The recovery affordances a row may ask for. */
export const ACTIONS = {
	/** Ask admin to reconcile gateway truth for the current intent. Never charges. */
	CHECK: "check",
	/** Create or reuse a checkout intent and open the gateway. May charge. */
	INITIATE: "initiate",
	/**
	 * Go back to the checkout THIS SIGNUP ALREADY HAS. Creates nothing.
	 *
	 * The pay step could hold a live, attested, unexpired token and still offer the
	 * customer no way to reach it: the only two payment verbs were CHECK and
	 * INITIATE, and `navigateToPay` was reachable only from the first signup, the
	 * email-verification return, and INITIATE - all of which mint or replace an
	 * intent. So a customer who opened checkout, got distracted, and came back
	 * inside their own 45 minute window had exactly one way forward, and it threw
	 * away the perfectly good checkout they already had and created a second
	 * Razorpay subscription (jarvis#685, admin-v2#248).
	 *
	 * Offered dynamically wherever `canNavigateToPay` is true rather than listed on
	 * individual rows, because navigability is a property of the machine's live
	 * token, not of the code that happens to be showing.
	 */
	RESUME: "resume",
	// There is deliberately NO client-driven CONFIRM affordance. See the
	// PAYMENT_AUTHORIZED_PENDING_CONFIRM row: admin's confirm_payment
	// signature-verifies before any branch and needs a gateway-issued
	// payment id + signature from a live Checkout callback, which the browser
	// cannot have - admin emits that code precisely because it could not resolve
	// the authorization payment id itself. A Confirm button there was a button
	// that returned a byte-identical screen forever.
	/** Paid: leave the payment step. */
	CONTINUE: "continue",
	/** Email a code and connect this site to an account that already paid. */
	RECONNECT: "reconnect",
	/** Re-poll after the customer clicks the magic link. */
	VERIFY: "verify",
	/**
	 * Send a fresh verification email to the SAME address (jarvis#297 P0-2a). Never
	 * listed on a row's static `actions` - like RESUME, its availability is a
	 * property of the LIVE answer (paymentMachine's `canResendVerification`,
	 * server-granted and fail-closed), not of the code that happens to be showing.
	 * See OnboardingView's `verifyActions` for where it is spliced in.
	 */
	RESEND: "resend",
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
		// jarvis#297 P0-2a: a wrong address is a real case (Verify alone dead-ends a
		// typo) and RESEND, the mail-amplifier risk, is capability-gated at the view
		// instead of listed here (see ACTIONS.RESEND). RESTART is safe on this code
		// by construction - see paymentMachine's RESTART_SAFE_CODES comment - and
		// walks the customer back to Details with their typed values still in
		// place, so "Start again" would undersell it: nothing is being restarted,
		// one field needs correcting.
		actions: [ACTIONS.VERIFY, ACTIONS.RESTART],
		actionLabels: { [ACTIONS.RESTART]: "Use a different email" },
	},
	[CODES.PAYMENT_CONFIRMATION_PENDING]: {
		// A WAIT state, same treatment as PAYMENT_AUTHORIZED_PENDING_CONFIRM below
		// (jarvis#705). This row cannot tell apart a checkout the customer never
		// touched from a mandate they already authorized whose webhook has not
		// landed yet, so it must never offer INITIATE for either: a live e2e run
		// authorized a second mandate by clicking it 84 seconds after the first,
		// before admin's own committed-money guard had anything to refuse. Nobody
		// is stranded by CHECK alone - while the token lives, RESUME (added
		// dynamically, see OnboardingView.vue) reopens the SAME checkout, and CHECK
		// itself resolves a genuinely untouched one to NO_CURRENT_INTENT, which
		// offers INITIATE.
		headline: "We have not confirmed this payment.",
		body: "Check the status before doing anything else. If money already moved, please do not pay again, as that would authorize a second one.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK],
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
		headline: "Your payment isn't finished yet.",
		body: "Nothing has been charged. Start the payment when you're ready - it only takes a moment.",
		tone: TONE.STATUS,
		// INITIATE FIRST, and this is the one row where that is right.
		//
		// "Status first" exists because checking a payment that already succeeded
		// costs a round trip while paying twice costs money. That asymmetry needs a
		// payment to exist. This code means there is NO current intent, so there is
		// nothing to check and nothing to double-charge: the read-only button leads
		// to "we checked, nothing has changed", forever, while the one button that
		// actually moves the customer forward sits second and greyer. Both of the
		// owner's stuck sites sat in exactly this state.
		actions: [ACTIONS.INITIATE, ACTIONS.CHECK],
	},
	[CODES.ACCOUNT_RECONNECT_REQUIRED]: {
		headline: "This paid account belongs to an existing setup.",
		body: "Nothing to pay again - we will email a code to confirm it is you and connect this site to it.",
		tone: TONE.STATUS,
		actions: [ACTIONS.RECONNECT],
	},
	[CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM]: {
		// A WAIT state, not an action state. The authorization is real and sits at
		// the gateway; what has NOT happened is the bank confirming the mandate.
		// Nothing the browser can send finishes it (admin's confirm_payment needs a
		// gateway-issued payment id + signature that only a live Checkout callback
		// produces, and admin emits this code exactly when it could not resolve
		// that id), so the resolution is server-side: the gateway webhook activates
		// the subscription when the mandate confirms. Checking is the only safe
		// thing to offer, and after a few checks the support handoff is the exit -
		// an e-NACH can pend indefinitely by design.
		headline: "Your payment is authorized.",
		body:
			"Your bank is still confirming the auto-pay mandate, which can take a little while. " +
			"We are watching for it - please do not pay again, as that would authorize a second mandate.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK],
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
		body: "Nothing was charged. Checking the status is safe, and a fresh attempt clears it.",
		tone: TONE.ALERT,
		// A refused request bought nothing, so the recovery is a NEW attempt - and
		// the refused receipt must never be sent again (see mintFreshKey). CHECK
		// rides along because the refusal arrives as a failure envelope, which
		// carries no capability flags at all: without a read-only action here the
		// screen could render with its single button disabled and nothing else.
		actions: [ACTIONS.CHECK, ACTIONS.INITIATE],
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
		// Reconnect FIRST: the usual cause is the account being reconnected on
		// another site, which rotates the key this one holds. Reconnect is a guest
		// call, so it still works from a bench that cannot sign in.
		body: "Your payment is not affected. If you reconnected your account on another site, reconnect this one to bring it back.",
		tone: TONE.ALERT,
		actions: [ACTIONS.RECONNECT, ACTIONS.SUPPORT],
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
	[CODES.INTENT_CREATION_IN_PROGRESS]: {
		// Two clicks raced; the other one owns the payment page. Waiting is the whole
		// instruction - starting again is what would mint a second gateway object.
		headline: "Your payment page is still opening.",
		body: "Another attempt for this subscription is already being set up. Give it a moment, then check the status.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK],
	},
	[CODES.BILLING_NO_CURRENT_INTENT]: {
		headline: "There is no payment in progress.",
		body: "Nothing is waiting to be paid on this subscription right now.",
		tone: TONE.STATUS,
		actions: [ACTIONS.INITIATE],
	},
	[CODES.PAYMENT_UNDER_REVIEW]: {
		// The billing-page twin of BENCH_AWAITING_RECONCILIATION below. A refusal,
		// but a temporary one a person is already working on - so it states that
		// plainly and offers the read-only check, never a second payment.
		headline: "We are still confirming a payment on this account.",
		body:
			"A payment we could not place automatically is being checked by someone here. " +
			"Nothing more is owed - please do not pay again.",
		tone: TONE.STATUS,
		actions: [ACTIONS.CHECK],
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
	[CODES.BENCH_SIGNUP_DETAILS_REJECTED]: {
		// A rejection of what the CUSTOMER typed, raised before any gateway existed.
		// The headline therefore names the details, not a payment service, and the
		// only sensible action is to go back and fix the field. `message` carries
		// admin's own specific sentence ("billing.gstin failed its checksum check"),
		// which the page renders underneath as the detail line, so two different
		// causes no longer read as one identical failure.
		headline: "We could not accept some of your details.",
		body: "Nothing has been charged. Check the highlighted field and continue again.",
		tone: TONE.ALERT,
		// RESTART walks back to the Details step. It is safe here by construction:
		// admin refuses this before it creates any provider object, so there is never
		// a payment sitting behind it (see RESTART_SAFE_CODES in paymentMachine).
		actions: [ACTIONS.RESTART, ACTIONS.SUPPORT],
		// Per-row label override: "Start again" undersells this to the point of being
		// misleading. Nothing is being started again - one field needs correcting.
		actionLabels: { [ACTIONS.RESTART]: "Fix your details" },
	},
	// ---- plan-09 WS7: the admin-hosted pay-page cutover -----------------
	[CODES.PAYMENT_PAGE_REDIRECT]: {
		// The orchestrator top-level-navigates away the instant this lands, so this
		// row is only ever a flash. It is the copy MOMENT the plan names.
		headline: "Taking you to the secure payment page…",
		body: "Nothing is charged until you complete payment on the next screen.",
		tone: TONE.STATUS,
		actions: [],
	},
	[CODES.BENCH_PAY_ORIGIN_UNCONFIGURED]: {
		// A token arrived but this site cannot navigate to checkout: the pay origin
		// is unset, or its digest did not match admin's attestation. NO FALLBACK -
		// no sheet opens on this origin. Honest hold + a human.
		headline: "Secure payment isn't set up on this site yet.",
		body:
			"Nothing has been charged. This site's payment page still needs to be configured - " +
			"please contact support and we'll get you paid up without any double charge.",
		tone: TONE.ALERT,
		actions: [ACTIONS.SUPPORT],
	},
	[CODES.CLIENT_UPGRADE_REQUIRED]: {
		// A pre-cutover control plane answered with raw provider handles and no
		// token, OR admin refused this client's capability advert. The bench no longer
		// opens a sheet for those (§R P0-4); this is the honest hold, never a silent
		// fallback to the old SDK path. Nothing was created (the refusal precedes any
		// provider object), so "Start again" is provably safe (U4) - and its own copy
		// promises "try again", which without a RESTART button was a dead end whose
		// only exit was a hard reload nobody mentioned.
		headline: "Secure checkout is temporarily unavailable.",
		body:
			"Nothing has been charged. Our payment service is being updated - " +
			"please try again shortly, or contact support if it persists.",
		tone: TONE.ALERT,
		actions: [ACTIONS.RESTART, ACTIONS.SUPPORT],
	},
};

/**
 * The reconciliation FLAG's variant of the pending row.
 *
 * Deliberately NOT a code of its own: admin answers the ordinary
 * PAYMENT_CONFIRMATION_PENDING when money is parked, because a decline there
 * would invite a second payment. The flag only changes the SENTENCE now
 * (jarvis#705 made the plain row check-only too, same as this one); it still
 * vetoes the backend can_initiate_payment flag one layer down, in
 * paymentMachine.js.
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

/**
 * The label to render for one action on one row.
 *
 * Defaults to the shared vocabulary below, so two screens still cannot disagree,
 * but lets a row override a label whose generic wording would mislead in its own
 * context (see BENCH_SIGNUP_DETAILS_REJECTED, where "Start again" means "correct
 * one field" and saying the former would send the customer looking for a way to
 * abandon a signup that is fine).
 */
export function actionLabelFor(copy, action) {
	const override = copy && copy.actionLabels && copy.actionLabels[action];
	return override || ACTION_LABELS[action] || "";
}

/** Human labels for the affordances. One place, so two screens cannot disagree. */
export const ACTION_LABELS = {
	[ACTIONS.CHECK]: "Check payment status",
	// Names the destination, not the machinery: the customer is going back to the
	// payment page they already opened, and nothing is created by going there.
	[ACTIONS.RESUME]: "Continue to payment",
	// "again" is honest here: INITIATE always mints or replaces an intent, and on
	// Razorpay that means a new subscription object (admin-v2#248). It must never
	// be the easy default when RESUME is available.
	[ACTIONS.INITIATE]: "Start a new payment",
	[ACTIONS.CONTINUE]: "Continue",
	[ACTIONS.RECONNECT]: "Reconnect this site",
	[ACTIONS.VERIFY]: "I've verified my email",
	// The countdown variant ("Resend in Ns") is rendered separately in
	// OnboardingView's resendLabel, the same way checkLabel overrides this one
	// for ACTIONS.CHECK.
	[ACTIONS.RESEND]: "Resend the link",
	[ACTIONS.SUPPORT]: "Contact support",
	[ACTIONS.RESTART]: "Start again",
};

/**
 * What to tell the customer about how long their checkout link lasts (#669).
 *
 * The link dies about 45 minutes after it is minted, and until now that was
 * disclosed ONLY by the failure message after it had already aged out. Stepping
 * away to fetch a card is a normal thing to do mid-payment, so the limit belongs
 * on screen while the link still works.
 *
 * Takes SECONDS REMAINING (admin sends a duration, not a deadline, so a customer
 * whose clock is wrong is still told the truth) and returns "" when there is
 * nothing honest to say: no live token, or a value that is missing, unparseable
 * or not positive.
 *
 * Rounds DOWN, deliberately. Some seconds always pass between admin computing
 * this and the customer reading it, so rounding up would be the one direction
 * that can promise time the link does not have.
 */
export function payLinkDeadlineNote(expiresInS) {
	const secs = Number(expiresInS);
	if (!Number.isFinite(secs) || secs <= 0) return "";
	const mins = Math.floor(secs / 60);
	// Under a minute there is no honest number to give: by the time it is read it
	// may already be gone, so say that instead of counting down to zero.
	if (mins < 1) {
		return "This payment link is about to expire. If it stops working, start the payment again to get a fresh one.";
	}
	const unit = mins === 1 ? "minute" : "minutes";
	return `This payment link works for about ${mins} more ${unit}. If it expires, start the payment again to get a fresh one.`;
}
