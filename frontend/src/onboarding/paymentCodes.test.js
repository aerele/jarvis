// The copy table: every code the bench facade can hand this page, and the
// sentence + actions it renders for it. The test is the contract's mirror -
// jarvis/onboarding_contract.py is the source of truth for the vocabulary, and
// a code that reaches the page with no row here is the "wizard renders whatever
// state it was left in" bug the whole contract exists to remove.

import test from "node:test";
import assert from "node:assert/strict";

import {
	CODES,
	ADMIN_CODES,
	BENCH_CODES,
	ACTIONS,
	actionLabelFor,
	copyFor,
	payLinkDeadlineNote,
	UNKNOWN_COPY,
} from "./paymentCodes.js";

test("the vocabulary is 15 admin codes + 10 bench codes = 25", () => {
	// +3 bench codes from the plan-09 WS7 cutover: PAYMENT_PAGE_REDIRECT (the
	// navigate-to-pay signal), BENCH_PAY_ORIGIN_UNCONFIGURED (a token we cannot
	// navigate with) and CLIENT_UPGRADE_REQUIRED (a pre-cutover admin's raw handles).
	// +1 for BENCH_SIGNUP_DETAILS_REJECTED: admin refusing the customer's own
	// submitted details used to collapse into BENCH_ADMIN_REJECTED, whose copy blames
	// a payment service that was never contacted and offers a retry that can only
	// fail identically forever.
	assert.equal(ADMIN_CODES.length, 15);
	assert.equal(BENCH_CODES.length, 10);
	assert.equal(new Set([...ADMIN_CODES, ...BENCH_CODES]).size, 25);
});

test("every code in the vocabulary has its own copy row", () => {
	for (const code of [...ADMIN_CODES, ...BENCH_CODES]) {
		const entry = copyFor(code);
		assert.ok(entry, `no copy row for ${code}`);
		assert.ok(entry.headline, `no headline for ${code}`);
		assert.ok(entry.body, `no body for ${code}`);
		assert.ok(Array.isArray(entry.actions), `no actions for ${code}`);
		assert.notEqual(entry, UNKNOWN_COPY, `${code} fell through to the unknown row`);
	}
});

test("BENCH_AWAITING_RECONCILIATION is its OWN row, not a pending variant", () => {
	const own = copyFor(CODES.BENCH_AWAITING_RECONCILIATION);
	const pending = copyFor(CODES.PAYMENT_CONFIRMATION_PENDING);
	assert.notEqual(own.headline, pending.headline);
	// Money is parked with a human. Offering another payment is the one thing
	// this state must never do.
	assert.ok(!own.actions.includes(ACTIONS.INITIATE));
	assert.ok(own.actions.includes(ACTIONS.CHECK));
});

test("awaiting_manual_reconciliation renders as a FLAG-VARIANT of the pending copy", () => {
	const plain = copyFor(CODES.PAYMENT_CONFIRMATION_PENDING);
	const flagged = copyFor(CODES.PAYMENT_CONFIRMATION_PENDING, {
		awaitingReconciliation: true,
	});
	// Same code, different sentence - the flag names WHY the wait is happening.
	// Neither variant offers to pay again (jarvis#705): admin's own contract gives
	// PAYMENT_CONFIRMATION_PENDING no recovery action at all, and inventing one is
	// how a waiting customer gets talked into a second mandate.
	assert.notEqual(flagged.body, plain.body);
	assert.ok(!plain.actions.includes(ACTIONS.INITIATE));
	assert.ok(!flagged.actions.includes(ACTIONS.INITIATE));
	assert.ok(flagged.actions.includes(ACTIONS.CHECK));
});

test("PAYMENT_CONFIRMATION_PENDING is a WAIT state: check only, never a second payment", () => {
	// admin's own contract (signup.py's _RECOVERY_FOR_CODE) gives this code no
	// recovery action: "there is nothing for the caller to do but wait, and
	// inventing an action for it is how a waiting customer gets talked into
	// paying twice." The wizard used to disagree and offer Initiate here, which
	// is how a live e2e run authorized a second mandate 84 seconds after the
	// first while the original subscription was still resolving (jarvis#705).
	//
	// Nobody is stranded by dropping Initiate: while the token lives, RESUME
	// reopens the SAME checkout (added dynamically in OnboardingView.vue, not
	// listed here), and CHECK itself resolves a genuinely untouched checkout to
	// NO_CURRENT_INTENT, whose own row offers Initiate.
	const entry = copyFor(CODES.PAYMENT_CONFIRMATION_PENDING);
	assert.deepEqual(entry.actions, [ACTIONS.CHECK]);
	assert.ok(
		!entry.actions.includes(ACTIONS.INITIATE),
		"a second intent authorizes a second mandate while the first is still resolving"
	);
	assert.ok(/not pay again|don't pay again|do not pay again/i.test(entry.body));
});

// The ONE code exempt from status-first, and it is exempt on the rule's own terms.
// Status-first trades a wasted round trip against a double charge, which requires a
// payment to exist. NO_CURRENT_INTENT means there is none: nothing to check, nothing
// to double-charge. Leading with the read-only button there gave the customer a
// control that can only ever answer "nothing has changed" while the one action that
// moves them forward sat second and greyer. Any OTHER code added to this set is a
// real regression, which is why the set is asserted rather than the loop skipped.
const STATUS_FIRST_EXEMPT = new Set([CODES.NO_CURRENT_INTENT]);

test("status-first: check comes before initiate wherever both are offered", () => {
	for (const code of [...ADMIN_CODES, ...BENCH_CODES]) {
		const { actions } = copyFor(code);
		const check = actions.indexOf(ACTIONS.CHECK);
		const initiate = actions.indexOf(ACTIONS.INITIATE);
		if (check >= 0 && initiate >= 0 && !STATUS_FIRST_EXEMPT.has(code)) {
			assert.ok(check < initiate, `${code} offers a payment before a status check`);
		}
	}
});

test("the status-first exemption is exactly one code, and it is the no-intent one", () => {
	// Pins the carve-out so widening it needs a deliberate edit here, with a reason.
	assert.deepEqual([...STATUS_FIRST_EXEMPT], [CODES.NO_CURRENT_INTENT]);
	const { actions } = copyFor(CODES.NO_CURRENT_INTENT);
	assert.ok(actions.indexOf(ACTIONS.INITIATE) < actions.indexOf(ACTIONS.CHECK));
	// And it must still offer the check at all - a customer who believes money moved
	// needs the read-only path even when we think no intent exists.
	assert.ok(actions.includes(ACTIONS.CHECK));
});

test("RESUME is never baked into a copy row: it is a property of the live token", () => {
	// The view unshifts it when canNavigateToPay() holds. A row that hardcoded it
	// would offer a navigate on a signup whose token has since expired.
	for (const code of [...ADMIN_CODES, ...BENCH_CODES]) {
		assert.ok(
			!copyFor(code).actions.includes(ACTIONS.RESUME),
			`${code} hardcodes RESUME; it must be offered dynamically instead`
		);
	}
	assert.equal(actionLabelFor({}, ACTIONS.RESUME), "Continue to payment");
});

test("a rate limit is never rendered as a decline and never offers a payment", () => {
	const entry = copyFor(CODES.PAYMENT_CHECK_RATE_LIMITED);
	assert.ok(!/declin|fail/i.test(entry.headline + entry.body));
	assert.ok(!entry.actions.includes(ACTIONS.INITIATE));
});

test("a paid signup offers no payment action at all", () => {
	for (const code of [CODES.PAYMENT_ALREADY_ACTIVE, CODES.ACCOUNT_RECONNECT_REQUIRED]) {
		const { actions } = copyFor(code);
		assert.ok(!actions.includes(ACTIONS.INITIATE), `${code} offers to pay again`);
	}
});

test("PAYMENT_AUTHORIZED_PENDING_CONFIRM is a WAIT state: check only, never confirm, never pay", () => {
	// The client cannot confirm this. Admin emits this code precisely BECAUSE it
	// could not resolve the authorization payment id, and its confirm_payment
	// signature-verifies before any branch (api/tenant.py) - so a browser-built
	// payload is a guaranteed 402 and a Confirm button is a dead end that returns
	// a byte-identical screen forever. The gateway webhook is the real resolver;
	// the support handoff after N checks is this state's exit.
	const entry = copyFor(CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM);
	assert.deepEqual(entry.actions, [ACTIONS.CHECK]);
	assert.ok(
		!entry.actions.includes(ACTIONS.INITIATE),
		"a second intent authorizes a second mandate"
	);
	// The vocabulary itself must not carry a confirm affordance any more.
	assert.equal(ACTIONS.CONFIRM, undefined);
	assert.ok(/authoriz/i.test(entry.headline + entry.body));
	assert.ok(/not pay again|don't pay again|do not pay again/i.test(entry.body));
});

test("the day-one code invites a signup, never support", () => {
	const entry = copyFor(CODES.BENCH_NO_SIGNUP_CONTEXT);
	assert.ok(!entry.actions.includes(ACTIONS.SUPPORT));
	assert.ok(!/support/i.test(entry.body));
	assert.ok(entry.actions.includes(ACTIONS.RESTART));
});

test("INVALID_REQUEST tells the page to mint a fresh attempt, not to resend", () => {
	const entry = copyFor(CODES.INVALID_REQUEST);
	assert.equal(entry.mintFreshKey, true);
	assert.ok(entry.actions.includes(ACTIONS.INITIATE));
});

test("copy never interpolates a raw exception or a traceback", () => {
	for (const code of [...ADMIN_CODES, ...BENCH_CODES]) {
		const { headline, body } = copyFor(code);
		assert.ok(!/Error\b|Traceback|Exception/.test(headline + body), `${code} leaks internals`);
	}
});

test("an unheard-of code falls back to the honest unknown row", () => {
	const entry = copyFor("SOMETHING_ADMIN_ADDED_LAST_WEEK");
	assert.equal(entry, UNKNOWN_COPY);
	assert.ok(entry.actions.includes(ACTIONS.CHECK));
});

test("a bench that cannot sign in is offered the reconnect that fixes it", () => {
	// Reconnecting the account on another site rotates the key THIS one holds, so
	// the remedy is a reconnect - and it is a guest call, reachable without the
	// credentials that just stopped working. Support stays as the fallback.
	const entry = copyFor(CODES.BENCH_ADMIN_AUTH_FAILED);
	assert.equal(entry.actions[0], ACTIONS.RECONNECT, "reconnect is the primary action");
	assert.ok(entry.actions.includes(ACTIONS.SUPPORT));
});

// The ordering contract RESUME must respect, asserted on the table rather than on a
// mounted view so it cannot rot. The view inserts RESUME after CHECK when a row asks
// for CHECK, and first otherwise; these are the rows that pin why.
test("rows that warn about money already moving ask for CHECK first", () => {
	// If RESUME were ever promoted above CHECK on this row, the customer would be
	// steered back to the payment page ahead of the read that tells them they have
	// already paid. That is the double payment status-first exists to prevent, and
	// the row's own body says so out loud.
	const pending = copyFor(CODES.PAYMENT_CONFIRMATION_PENDING);
	assert.equal(pending.actions[0], ACTIONS.CHECK);
	assert.match(pending.body, /check the status before doing anything else/i);

	// The declined row carries the same warning and the same ordering.
	const declined = copyFor(CODES.PAYMENT_DECLINED);
	assert.equal(declined.actions[0], ACTIONS.CHECK);
});

// --------------------------------------------------------------------------- //
// #669: how long the pay link lasts, said while it still works
// --------------------------------------------------------------------------- //

test("the deadline note states the remaining minutes", () => {
	assert.match(payLinkDeadlineNote(45 * 60), /about 45 more minutes/);
	// Singular, because "1 more minutes" on a payment screen reads as a bug in
	// exactly the moment the customer is being asked to trust us with a card.
	assert.match(payLinkDeadlineNote(90), /about 1 more minute\b/);
});

test("the deadline note rounds DOWN, never promising time the link lacks", () => {
	// 119s is nearly two minutes, but saying "2" would overstate it, and seconds
	// keep passing between admin computing this and the customer reading it. The
	// only safe rounding direction is down.
	assert.match(payLinkDeadlineNote(119), /about 1 more minute\b/);
	assert.match(payLinkDeadlineNote(59 * 60 + 59), /about 59 more minutes/);
});

test("under a minute it warns instead of counting to zero", () => {
	const note = payLinkDeadlineNote(30);
	assert.match(note, /about to expire/i);
	// No number at all: whatever we printed could be wrong by the time it is read.
	assert.doesNotMatch(note, /\d/);
});

test("every note says how to recover, not just that time is passing", () => {
	// A deadline with no way out is just a nicer dead end, which is the failure
	// mode this issue is about in the first place.
	for (const secs of [45 * 60, 90, 30]) {
		assert.match(payLinkDeadlineNote(secs), /start the payment again/i);
	}
});

test("nothing is claimed when there is no honest number to give", () => {
	// null/undefined = no live token on the answer. 0 and negatives = expired.
	// Junk = an older admin that never sent the field. All render nothing rather
	// than a "0 minutes" scare over a link that still works.
	for (const bad of [null, undefined, 0, -1, "", "soon", NaN, {}]) {
		assert.equal(payLinkDeadlineNote(bad), "", `expected "" for ${String(bad)}`);
	}
});

// ---------------------------------------------------------------------------
// jarvis#297 P0-2a: the email-verification dead end.
// ---------------------------------------------------------------------------
test("verification offers a way past a mistyped address, not just VERIFY", () => {
	const entry = copyFor(CODES.SIGNUP_VERIFICATION_REQUIRED);
	assert.ok(entry.actions.includes(ACTIONS.VERIFY));
	assert.ok(entry.actions.includes(ACTIONS.RESTART));
	assert.equal(actionLabelFor(entry, ACTIONS.RESTART), "Use a different email");
	// "Start again" would undersell it - nothing else about the signup is being
	// restarted, only the address.
	assert.notEqual(actionLabelFor(entry, ACTIONS.RESTART), "Start again");
});

test("RESEND is never baked into the verification row either: it is a granted capability", () => {
	// Same reasoning paymentCodes.test.js already asserts for RESUME (above): the
	// view splices it in only once paymentMachine's canResendVerification says
	// the server actually granted it, never listed statically here.
	assert.ok(!copyFor(CODES.SIGNUP_VERIFICATION_REQUIRED).actions.includes(ACTIONS.RESEND));
	assert.equal(actionLabelFor({}, ACTIONS.RESEND), "Resend the link");
});

test("a lapsed signup is offered the page that can actually renew it", () => {
	// The dead end: copy said "renew from your account", only button was support.
	const copy = copyFor(CODES.SIGNUP_TERMINAL);
	assert.equal(copy.actions[0], ACTIONS.BILLING, "renewing is the PRIMARY way out");
	assert.ok(copy.actions.includes(ACTIONS.SUPPORT), "support stays available");
	assert.equal(actionLabelFor(copy, ACTIONS.BILLING), "Renew your plan");
	assert.match(copy.body, /billing page/i);
});

test("the wizard never grows its own renewal verb", () => {
	// Renewal lives on billing, with the cohort rules. This action routes, not charges.
	const copy = copyFor(CODES.SIGNUP_TERMINAL);
	assert.equal(copy.actions.includes(ACTIONS.INITIATE), false);
	assert.equal(copy.actions.includes(ACTIONS.RESUME), false);
});
