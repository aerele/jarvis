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
	copyFor,
	UNKNOWN_COPY,
} from "./paymentCodes.js";

test("the vocabulary is 12 admin codes + 9 bench codes = 21", () => {
	// +3 bench codes from the plan-09 WS7 cutover: PAYMENT_PAGE_REDIRECT (the
	// navigate-to-pay signal), BENCH_PAY_ORIGIN_UNCONFIGURED (a token we cannot
	// navigate with) and CLIENT_UPGRADE_REQUIRED (a pre-cutover admin's raw handles).
	assert.equal(ADMIN_CODES.length, 12);
	assert.equal(BENCH_CODES.length, 9);
	assert.equal(new Set([...ADMIN_CODES, ...BENCH_CODES]).size, 21);
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
	// Same code, different sentence - the flag is what distinguishes them,
	// because admin deliberately answers the ordinary pending code so no wizard
	// invites a second payment.
	assert.notEqual(flagged.body, plain.body);
	assert.ok(plain.actions.includes(ACTIONS.INITIATE));
	assert.ok(!flagged.actions.includes(ACTIONS.INITIATE));
	assert.ok(flagged.actions.includes(ACTIONS.CHECK));
});

test("status-first: check comes before initiate wherever both are offered", () => {
	for (const code of [...ADMIN_CODES, ...BENCH_CODES]) {
		const { actions } = copyFor(code);
		const check = actions.indexOf(ACTIONS.CHECK);
		const initiate = actions.indexOf(ACTIONS.INITIATE);
		if (check >= 0 && initiate >= 0) {
			assert.ok(check < initiate, `${code} offers a payment before a status check`);
		}
	}
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
