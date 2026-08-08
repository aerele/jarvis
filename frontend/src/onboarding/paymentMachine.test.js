// The pay step's transition table. Pure: no Vue, no network, no timers - the
// orchestrator owns every side effect so this file can assert the ONE rule the
// whole plan exists for: nothing but an authoritative paid answer leaves the pay
// step.
//
// plan-09 WS7 (the admin-hosted checkout cutover): the machine opens no gateway
// sheet. A payable answer is a pay-page TOKEN plus the bench's OWN attested
// origin, and the customer is TOP-LEVEL NAVIGATED to the pay page
// (NAVIGATED_TO_PAY → CHECKOUT_OPEN). This suite keeps every safety invariant that
// still applies (paid floor, generation/attempt fence, restart contract, rate
// limit, transport failure, summary/identity, provisioning ownership) and replaces
// the retired SDK-sheet mechanics with the navigation-topology rules.

import test from "node:test";
import assert from "node:assert/strict";

import { CODES, ADMIN_CODES, BENCH_CODES } from "./paymentCodes.js";
import {
	STATES,
	EVENTS,
	CHECKOUT_NAMESPACE,
	initialState,
	reduce,
	canNavigateToPay,
	canSafelyRestart,
	payPageUrl,
	isTerminalForPayment,
	provisioningOwner,
	remainingCooldownSeconds,
	remainingResendCooldownSeconds,
	noteVerificationResent,
} from "./paymentMachine.js";

const ORIGIN = "https://fleet.klerk.in";

const CONTRACT = (over = {}) => ({
	type: EVENTS.CONTRACT_STATE,
	decoded: {
		ok: true,
		code: CODES.PAYMENT_CONFIRMATION_PENDING,
		message: "",
		recovery: "",
		data: {
			attempt_id: "att_1",
			generation: 1,
			can_initiate_payment: true,
			can_check_status: true,
		},
		context: {},
		httpStatus: 200,
		...over,
	},
});

function at(code, data = {}) {
	return CONTRACT({
		code,
		ok: true,
		data: { attempt_id: "att_1", generation: 1, ...data },
	});
}

// A navigable pay-page token answer: token + bench origin + admin's attestation.
function token(over = {}) {
	return at(CODES.PAYMENT_PAGE_REDIRECT, {
		pay_page_token: "tok_1",
		pay_origin: ORIGIN,
		pay_origin_attested: true,
		payment_provider: "razorpay",
		can_check_status: true,
		...over,
	});
}

// ---------------------------------------------------------------------------
// the shape of a fresh page
// ---------------------------------------------------------------------------
test("initialState starts on review with nothing to check and nothing to pay", () => {
	const s = initialState();
	assert.equal(s.value, STATES.REVIEW);
	assert.equal(s.attemptId, null);
	assert.equal(s.generation, null);
	assert.equal(s.canCheck, false);
	assert.equal(s.canInitiate, false);
	assert.equal(s.payPageToken, "");
	assert.equal(s.payOriginAttested, false);
});

// ---------------------------------------------------------------------------
// legal transitions, keyed on the CODE and never on an HTTP status
// ---------------------------------------------------------------------------
test("submitting review starts the signup", () => {
	const s = reduce(initialState(), { type: EVENTS.SUBMIT_REVIEW });
	assert.equal(s.value, STATES.STARTING_SIGNUP);
	assert.equal(s.busy, "starting");
});

test("SIGNUP_VERIFICATION_REQUIRED parks on the verification screen with no pay button", () => {
	const s = reduce(
		initialState(),
		at(CODES.SIGNUP_VERIFICATION_REQUIRED, {
			pending_verification: true,
			verification_expires_at: "2026-08-03 00:00:00",
			can_initiate_payment: false,
		})
	);
	assert.equal(s.value, STATES.VERIFICATION_REQUIRED);
	assert.equal(s.canInitiate, false);
	assert.equal(s.verificationExpiresAt, "2026-08-03 00:00:00");
});

// ---------------------------------------------------------------------------
// plan-09 WS7: the navigate-to-pay capability
// ---------------------------------------------------------------------------
test("a navigable token answer lands on UNKNOWN and is navigable to pay", () => {
	const s = reduce(initialState(), token());
	assert.equal(s.value, STATES.UNKNOWN);
	assert.equal(s.payPageToken, "tok_1");
	assert.equal(s.payOrigin, ORIGIN);
	assert.equal(s.payOriginAttested, true);
	assert.equal(canNavigateToPay(s), true);
	assert.equal(s.provider, "razorpay");
});

test("payPageUrl builds {origin}/jarvis-checkout#t=<token> from the ATTESTED origin only", () => {
	const s = reduce(initialState(), token());
	assert.equal(payPageUrl(s), `${ORIGIN}/${CHECKOUT_NAMESPACE}#t=tok_1`);
});

test("NAVIGATED_TO_PAY moves a navigable state to checkout_open (the away marker)", () => {
	const nav = reduce(initialState(), token());
	const open = reduce(nav, { type: EVENTS.NAVIGATED_TO_PAY });
	assert.equal(open.value, STATES.CHECKOUT_OPEN);
	// ...and checkout_open is NOT a navigable source: a second navigate no-ops
	// (double-click cannot double-navigate).
	assert.equal(canNavigateToPay(open), false);
	const again = reduce(open, { type: EVENTS.NAVIGATED_TO_PAY });
	assert.equal(again.value, STATES.CHECKOUT_OPEN);
	assert.equal(again.illegalTransitions, 1);
});

test("NAVIGATED_TO_PAY from a non-navigable state is illegal, never a blind navigate", () => {
	assert.throws(
		() => reduce(initialState(), { type: EVENTS.NAVIGATED_TO_PAY }, { strict: true }),
		/illegal transition/
	);
});

test("NO FALLBACK: a token whose origin is NOT attested fails closed, never navigates", () => {
	const s = reduce(initialState(), token({ pay_origin_attested: false }));
	assert.equal(s.value, STATES.FAILED_TERMINAL);
	assert.equal(s.code, CODES.BENCH_PAY_ORIGIN_UNCONFIGURED);
	assert.equal(canNavigateToPay(s), false);
	assert.equal(payPageUrl(s), "");
});

test("NO FALLBACK: a token with no configured origin fails closed", () => {
	const s = reduce(initialState(), token({ pay_origin: "", pay_origin_attested: false }));
	assert.equal(s.value, STATES.FAILED_TERMINAL);
	assert.equal(s.code, CODES.BENCH_PAY_ORIGIN_UNCONFIGURED);
	assert.equal(canNavigateToPay(s), false);
});

test("NO FALLBACK: a pre-cutover admin's raw handles (CLIENT_UPGRADE_REQUIRED) never navigate", () => {
	// The flow's effectiveCode maps raw-handles-no-token to CLIENT_UPGRADE_REQUIRED;
	// the reducer renders it as an honest terminal hold with no payment action.
	const s = reduce(
		initialState(),
		at(CODES.CLIENT_UPGRADE_REQUIRED, { can_check_status: false })
	);
	assert.equal(s.value, STATES.FAILED_TERMINAL);
	assert.equal(canNavigateToPay(s), false);
	assert.equal(s.canInitiate, false);
});

test("a live token OVERRIDES a bare INTENT_HANDLE_UNAVAILABLE (read-envelope re-serve)", () => {
	// The flow's effectiveCode already resolved code -> PAYMENT_PAGE_REDIRECT for a
	// coded INTENT_HANDLE_UNAVAILABLE carrying a token; the reducer then navigates.
	const s = reduce(initialState(), token());
	assert.equal(s.value, STATES.UNKNOWN);
	assert.equal(canNavigateToPay(s), true);
});

test("a stale token cannot linger: a non-token answer clears the navigate capability", () => {
	const nav = reduce(initialState(), token());
	assert.equal(canNavigateToPay(nav), true);
	// A subsequent ordinary pending poll carries no token -> not navigable any more.
	const after = reduce(nav, at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 1 }));
	assert.equal(after.payPageToken, "");
	assert.equal(canNavigateToPay(after), false);
});

test("canNavigateToPay is refused for every settled/verification/paid source state", () => {
	// P1-1 as the navigate-to-pay eligibility rule: even holding a live attested
	// token, none of these may navigate.
	const withToken = {
		payPageToken: "tok_1",
		payOrigin: ORIGIN,
		payOriginAttested: true,
	};
	for (const value of [
		STATES.VERIFICATION_REQUIRED,
		STATES.CONFIRM_REQUIRED,
		STATES.RECONNECT,
		STATES.FAILED_TERMINAL,
		STATES.PAID,
		STATES.PROVISIONING,
		STATES.PROVISIONING_DELAYED,
		STATES.CHECKOUT_OPEN,
		STATES.REVIEW,
	]) {
		assert.equal(canNavigateToPay({ ...withToken, value }), false, value);
	}
	// Only a live, unsettled intent may navigate.
	assert.equal(canNavigateToPay({ ...withToken, value: STATES.UNKNOWN }), true);
	assert.equal(canNavigateToPay({ ...withToken, value: STATES.FAILED_RETRYABLE }), true);
});

// ---------------------------------------------------------------------------
// verdicts that are not a payable navigate
// ---------------------------------------------------------------------------
test("a decline is retryable and stays on the pay step", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_DECLINED, { can_initiate_payment: true }));
	assert.equal(s.value, STATES.FAILED_RETRYABLE);
});

test("SIGNUP_TERMINAL is terminal: no blind payment retry", () => {
	const s = reduce(initialState(), at(CODES.SIGNUP_TERMINAL, { can_initiate_payment: true }));
	assert.equal(s.value, STATES.FAILED_TERMINAL);
	assert.equal(s.canInitiate, false);
	assert.equal(isTerminalForPayment(s.value), true);
});

test("PAYMENT_ALREADY_ACTIVE is the only code that means paid", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	assert.equal(s.value, STATES.PAID);
});

test("PAYMENT_AUTHORIZED_PENDING_CONFIRM lands on confirm_required, not a retry", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM, { can_initiate_payment: false })
	);
	assert.equal(s.value, STATES.CONFIRM_REQUIRED);
	assert.equal(s.canInitiate, false);
	assert.equal(isTerminalForPayment(s.value), false);
});

test("ACCOUNT_RECONNECT_REQUIRED lands on reconnect - never a fake paid state", () => {
	const s = reduce(
		initialState(),
		at(CODES.ACCOUNT_RECONNECT_REQUIRED, { can_reconnect: true })
	);
	assert.equal(s.value, STATES.RECONNECT);
	assert.equal(s.canReconnect, true);
});

test("can_reconnect on any envelope raises the offer without changing the state", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_DECLINED, { can_reconnect: true, can_initiate_payment: true })
	);
	assert.equal(s.value, STATES.FAILED_RETRYABLE);
	assert.equal(s.canReconnect, true);
});

// ---------------------------------------------------------------------------
// paid is a floor
// ---------------------------------------------------------------------------
test("a late pending answer never regresses a paid page", () => {
	const paid = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	const after = reduce(paid, at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 9 }));
	assert.equal(after.value, STATES.PAID);
});

test("a late decline never regresses a paid page either", () => {
	const paid = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	const after = reduce(paid, at(CODES.PAYMENT_DECLINED, { generation: 9 }));
	assert.equal(after.value, STATES.PAID);
});

test("a late navigate cannot take a settled signup back to the pay page", () => {
	const paid = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	const after = reduce(paid, { type: EVENTS.NAVIGATED_TO_PAY });
	assert.equal(after.value, STATES.PAID);
});

test("a late token answer cannot re-arm navigation on a paid page", () => {
	const paid = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	const after = reduce(paid, token({ generation: 9 }));
	assert.equal(after.value, STATES.PAID);
	assert.equal(canNavigateToPay(after), false);
});

test("provisioning does not regress to a payment state", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	s = reduce(s, { type: EVENTS.PROVISIONING_STARTED });
	assert.equal(s.value, STATES.PROVISIONING);
	const after = reduce(s, at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 9 }));
	assert.equal(after.value, STATES.PROVISIONING);
});

// ---------------------------------------------------------------------------
// the generation + attempt fence (P1-4)
// ---------------------------------------------------------------------------
test("a response from an OLDER generation is ignored outright", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 5 }));
	const after = reduce(s, at(CODES.PAYMENT_DECLINED, { generation: 4 }));
	assert.equal(after.value, s.value);
	assert.equal(after.code, s.code);
});

test("a NEWER generation supersedes the current intent", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 1 }));
	const after = reduce(s, token({ generation: 2 }));
	assert.equal(after.generation, 2);
	assert.equal(after.payPageToken, "tok_1");
	assert.equal(canNavigateToPay(after), true);
});

test("an envelope with no generation is not treated as generation zero", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 3 }));
	const after = reduce(
		s,
		CONTRACT({ code: CODES.PAYMENT_DECLINED, data: { attempt_id: "att_1" } })
	);
	// generation missing -> the known generation survives, and the answer is NOT
	// rejected as a stale gen-0.
	assert.equal(after.generation, 3);
	assert.equal(after.value, STATES.FAILED_RETRYABLE);
});

test("P1-4: a different attempt at the SAME generation is judged on its own terms", () => {
	const s = reduce(initialState(), token({ attempt_id: "att_1", generation: 1 }));
	const after = reduce(
		s,
		at(CODES.PAYMENT_DECLINED, {
			attempt_id: "att_2",
			generation: 1,
			can_initiate_payment: true,
		})
	);
	assert.equal(after.attemptId, "att_2");
	assert.equal(after.value, STATES.FAILED_RETRYABLE);
	// the new attempt's answer carries no token, so the old attempt's navigate
	// capability is cleared.
	assert.equal(after.payPageToken, "");
});

test("P1-4: a new attempt at a LOWER generation is a replacement, not a stale reject", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 5 }));
	const after = reduce(s, token({ attempt_id: "att_2", generation: 1 }));
	assert.equal(after.attemptId, "att_2");
	assert.equal(after.generation, 1);
	assert.equal(canNavigateToPay(after), true);
});

test("P1-4: a LOSING generation of the SAME attempt is still rejected outright", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 5 }));
	const after = reduce(s, at(CODES.PAYMENT_DECLINED, { attempt_id: "att_1", generation: 4 }));
	assert.equal(after.value, s.value);
});

// ---------------------------------------------------------------------------
// the rate limit
// ---------------------------------------------------------------------------
test("a 429 leaves the payment state exactly where it was", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING));
	const after = reduce(s, { type: EVENTS.RATE_LIMITED, retryAfterSeconds: 30, nowMs: 1000 });
	assert.equal(after.value, s.value);
	assert.equal(after.checkCooldownUntil, 31000);
});

test("a COLD 429 renders the rate-limit row, not the alarming catch-all", () => {
	const s = reduce(
		initialState(),
		CONTRACT({
			ok: false,
			code: CODES.PAYMENT_CHECK_RATE_LIMITED,
			retryAfterSeconds: 12,
			data: {},
		})
	);
	assert.equal(s.code, CODES.PAYMENT_CHECK_RATE_LIMITED);
});

test("a 429 over a KNOWN payment state leaves that state's code alone", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_DECLINED));
	const after = reduce(
		s,
		CONTRACT({ ok: false, code: CODES.PAYMENT_CHECK_RATE_LIMITED, data: {} })
	);
	assert.equal(after.code, CODES.PAYMENT_DECLINED);
});

test("a 429 with no hint still cools down for a sane default", () => {
	const after = reduce(initialState(), { type: EVENTS.RATE_LIMITED, nowMs: 0 });
	assert.equal(after.checkCooldownUntil, 30000);
});

test("the countdown counts down, rounds UP, and never goes negative", () => {
	const s = { checkCooldownUntil: 5400 };
	assert.equal(remainingCooldownSeconds(s, 0), 6);
	assert.equal(remainingCooldownSeconds(s, 5000), 1);
	assert.equal(remainingCooldownSeconds(s, 5400), 0);
	assert.equal(remainingCooldownSeconds(s, 9000), 0);
});

test("the cooldown lifts on its own once the clock passes it", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING));
	s = reduce(s, { type: EVENTS.RATE_LIMITED, retryAfterSeconds: 10, nowMs: 0 });
	assert.equal(s.canCheck, false);
	s = reduce(s, { type: EVENTS.COOLDOWN_ELAPSED, nowMs: 11000 });
	assert.equal(s.checkCooldownUntil, 0);
	assert.equal(s.canCheck, true);
});

test("COOLDOWN_ELAPSED lifts the cooldown when the clock arrives in OPTS (the flow's shape)", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING));
	s = reduce(s, { type: EVENTS.RATE_LIMITED, retryAfterSeconds: 10 }, { nowMs: 0 });
	s = reduce(s, { type: EVENTS.COOLDOWN_ELAPSED }, { nowMs: 11000 });
	assert.equal(s.checkCooldownUntil, 0);
	assert.equal(s.canCheck, true);
});

test("RATE_LIMITED takes its clock from opts too", () => {
	const s = reduce(
		initialState(),
		{ type: EVENTS.RATE_LIMITED, retryAfterSeconds: 10 },
		{ nowMs: 500 }
	);
	assert.equal(s.checkCooldownUntil, 10500);
});

// ---------------------------------------------------------------------------
// transport failures say nothing about the money
// ---------------------------------------------------------------------------
test("a transport failure does not overwrite a known payment state", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_DECLINED));
	const after = reduce(
		s,
		CONTRACT({ ok: false, code: CODES.BENCH_ADMIN_UNREACHABLE, data: {} })
	);
	assert.equal(after.code, CODES.PAYMENT_DECLINED);
	assert.equal(after.transportError, true);
	assert.equal(after.canCheck, true);
});

test("a transport failure on a page that knows NOTHING renders unknown", () => {
	const s = reduce(
		initialState(),
		CONTRACT({ ok: false, code: CODES.BENCH_ADMIN_UNREACHABLE, data: {} })
	);
	assert.equal(s.value, STATES.UNKNOWN);
	assert.equal(s.transportError, true);
	assert.equal(s.canCheck, true);
});

test("a transport failure over a KNOWN navigable state keeps it (P0-1: says nothing about the money)", () => {
	// The token IS the known payment state here; a failed check does not overwrite
	// it, exactly as a failed check does not overwrite a decline. Nothing navigates
	// off it automatically - only a fresh submit/initiate/verify re-fetch does, and
	// that re-reads server truth first.
	const nav = reduce(initialState(), token());
	const after = reduce(
		nav,
		CONTRACT({ ok: false, code: CODES.BENCH_ADMIN_UNREACHABLE, data: {} })
	);
	assert.equal(after.payPageToken, "tok_1");
	assert.equal(after.transportError, true);
	assert.equal(after.canCheck, true);
});

test("a transport failure on a page that knows NOTHING never leaves a navigable token", () => {
	const after = reduce(
		initialState(),
		CONTRACT({ ok: false, code: CODES.BENCH_ADMIN_UNREACHABLE, data: {} })
	);
	assert.equal(after.payPageToken, "");
	assert.equal(canNavigateToPay(after), false);
});

// ---------------------------------------------------------------------------
// bench-local codes
// ---------------------------------------------------------------------------
test("BENCH_NO_SIGNUP_CONTEXT is a fresh start, never a support screen", () => {
	const s = reduce(
		initialState(),
		CONTRACT({ ok: true, code: CODES.BENCH_NO_SIGNUP_CONTEXT, data: {} })
	);
	assert.equal(s.value, STATES.REVIEW);
	assert.equal(s.notStarted, true);
});

test("BENCH_AWAITING_RECONCILIATION suppresses the pay affordance", () => {
	const s = reduce(
		initialState(),
		CONTRACT({
			ok: false,
			code: CODES.BENCH_AWAITING_RECONCILIATION,
			message: "we're still confirming a payment",
			data: {},
		})
	);
	assert.equal(s.awaitingReconciliation, true);
	assert.equal(s.canInitiate, false);
	assert.equal(s.canCheck, true);
});

test("BENCH_AWAITING_RECONCILIATION drops any stale navigate token", () => {
	const nav = reduce(initialState(), token());
	const after = reduce(
		nav,
		CONTRACT({ ok: false, code: CODES.BENCH_AWAITING_RECONCILIATION, data: {} })
	);
	assert.equal(after.payPageToken, "");
	assert.equal(canNavigateToPay(after), false);
});

test("BENCH_AWAITING_RECONCILIATION also arrives from the SIGNUP submit path", () => {
	let s = reduce(initialState(), { type: EVENTS.SUBMIT_REVIEW });
	s = reduce(s, CONTRACT({ ok: false, code: CODES.BENCH_AWAITING_RECONCILIATION, data: {} }));
	assert.equal(s.value, STATES.UNKNOWN);
	assert.equal(s.awaitingReconciliation, true);
	assert.equal(s.canInitiate, false);
});

test("the reconciliation FLAG on an ordinary pending answer also suppresses initiate", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			can_initiate_payment: true,
			awaiting_manual_reconciliation: true,
		})
	);
	assert.equal(s.awaitingReconciliation, true);
	assert.equal(s.canInitiate, false);
});

// ---------------------------------------------------------------------------
// cold-mount / no-flag invariants: a live read-only action always survives
// ---------------------------------------------------------------------------
test("INVARIANT: every code, arriving as a cold-mount FAILURE, leaves the check enabled", () => {
	for (const code of [...ADMIN_CODES, ...BENCH_CODES]) {
		const s = reduce(initialState(), CONTRACT({ ok: false, code, data: {} }));
		// Either it is a definite terminal/paid state (no action needed) or the
		// read-only check is live - never a screen whose only control is disabled.
		const okState =
			s.canCheck ||
			isTerminalForPayment(s.value) ||
			s.value === STATES.PAID ||
			s.value === STATES.VERIFICATION_REQUIRED ||
			s.value === STATES.REVIEW;
		assert.ok(okState, `${code} left no live action`);
	}
});

// ---------------------------------------------------------------------------
// return from the pay page (P0-2)
// ---------------------------------------------------------------------------
test("P0-2: RETURNED_FROM_CHECKOUT leaves checkout_open for a checkable unknown", () => {
	let s = reduce(initialState(), token());
	s = reduce(s, { type: EVENTS.NAVIGATED_TO_PAY });
	assert.equal(s.value, STATES.CHECKOUT_OPEN);
	const back = reduce(s, { type: EVENTS.RETURNED_FROM_CHECKOUT });
	assert.equal(back.value, STATES.UNKNOWN);
	assert.equal(back.checkRequired, true);
});

test("P0-2: RETURNED_FROM_CHECKOUT never assumes anything from a non-checkout state", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_DECLINED));
	const back = reduce(s, { type: EVENTS.RETURNED_FROM_CHECKOUT });
	assert.equal(back.value, s.value); // no-op
});

test("P0-2: a paid page ignores a stray return", () => {
	const paid = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	const back = reduce(paid, { type: EVENTS.RETURNED_FROM_CHECKOUT });
	assert.equal(back.value, STATES.PAID);
});

// ---------------------------------------------------------------------------
// the restart contract (P1-3)
// ---------------------------------------------------------------------------
test("P1-3: RESTART resets a day-one / account-exists state to a fresh review", () => {
	const s = reduce(
		initialState(),
		CONTRACT({ ok: true, code: CODES.BENCH_NO_SIGNUP_CONTEXT, data: {} })
	);
	assert.equal(canSafelyRestart(s), true);
	const after = reduce(s, { type: EVENTS.RESTART });
	assert.equal(after.value, STATES.REVIEW);
	assert.equal(after.payPageToken, "");
});

test("P1-3: RESTART preserves a state where a payment may be recoverable", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING));
	assert.equal(canSafelyRestart(s), false);
	const after = reduce(s, { type: EVENTS.RESTART });
	assert.equal(after.value, s.value); // held
});

test("P1-3: parked money is never wiped by restart", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { awaiting_manual_reconciliation: true })
	);
	assert.equal(canSafelyRestart(s), false);
});

test("U4: CLIENT_UPGRADE_REQUIRED is restart-safe (nothing was created)", () => {
	// The capability/upgrade refusal precedes any provider object, so "Start again"
	// is safe - and its own copy promises "try again", which without a working RESTART
	// was a dead end whose only exit was a hard reload.
	const s = reduce(
		initialState(),
		at(CODES.CLIENT_UPGRADE_REQUIRED, { can_check_status: false })
	);
	assert.equal(s.value, STATES.FAILED_TERMINAL); // still a terminal hold for THIS attempt
	assert.equal(canSafelyRestart(s), true);
	const after = reduce(s, { type: EVENTS.RESTART });
	assert.equal(after.value, STATES.REVIEW);
	assert.equal(after.payPageToken, "");
});

// ---------------------------------------------------------------------------
// illegal transitions
// ---------------------------------------------------------------------------
test("unknown -> provisioning is illegal and throws in strict mode", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING));
	assert.throws(() => reduce(s, { type: EVENTS.PROVISIONING_STARTED }, { strict: true }));
});

test("an illegal transition in production is ignored and counted, never applied", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING));
	const after = reduce(s, { type: EVENTS.PROVISIONING_STARTED });
	assert.equal(after.value, s.value);
	assert.equal(after.illegalTransitions, 1);
});

// ---------------------------------------------------------------------------
// provisioning ownership + terminal set
// ---------------------------------------------------------------------------
test("provisioning is owned by the readiness gate, not by the payment endpoints", () => {
	assert.equal(provisioningOwner(STATES.PROVISIONING), "readiness");
	assert.equal(provisioningOwner(STATES.PROVISIONING_DELAYED), "readiness");
	assert.equal(provisioningOwner(STATES.UNKNOWN), "payment");
});

test("isTerminalForPayment covers exactly the states that must never show a pay button", () => {
	for (const v of [
		STATES.PAID,
		STATES.PROVISIONING,
		STATES.PROVISIONING_DELAYED,
		STATES.FAILED_TERMINAL,
		STATES.RECONNECT,
	]) {
		assert.equal(isTerminalForPayment(v), true, v);
	}
	for (const v of [STATES.UNKNOWN, STATES.FAILED_RETRYABLE, STATES.CONFIRM_REQUIRED]) {
		assert.equal(isTerminalForPayment(v), false, v);
	}
});

// ---------------------------------------------------------------------------
// summary / identity from server truth (C02-3)
// ---------------------------------------------------------------------------
test("the summary takes identity from the server context, never from a prefill", () => {
	const s = reduce(
		initialState(),
		CONTRACT({
			ok: true,
			code: CODES.PAYMENT_CONFIRMATION_PENDING,
			data: {
				attempt_id: "att_1",
				generation: 1,
				email: "real@corp.com",
				company: "RealCo",
			},
		})
	);
	assert.equal(s.summary.email, "real@corp.com");
	assert.equal(s.summary.company, "RealCo");
});

test("trial disclosure survives into the summary", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			due_today_inr: 0,
			amount_inr: 3999,
			trial_days: 14,
		})
	);
	assert.equal(s.summary.trialDays, 14);
	assert.equal(s.summary.dueTodayInr, 0);
	assert.equal(s.summary.amountInr, 3999);
});

test("'last checked' is read from data, never invented", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { payment_last_checked_at: "2026-08-03 01:02:03" })
	);
	assert.equal(s.lastCheckedAt, "2026-08-03 01:02:03");
});

// ---------------------------------------------------------------------------
// #669: the pay link's remaining life travels WITH its token
// ---------------------------------------------------------------------------
test("a token answer carries how long that token is good for", () => {
	const s = reduce(initialState(), token({ pay_token_expires_in_s: 2700 }));
	assert.equal(s.payTokenExpiresInS, 2700);
});

test("a fresh page claims no deadline", () => {
	assert.equal(initialState().payTokenExpiresInS, null);
});

test("the countdown is gated on the token, exactly like the origin is", () => {
	// An answer with a duration but NO token must not leave a deadline behind: it
	// would paint a live-looking countdown over a link that cannot be opened.
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { pay_token_expires_in_s: 2700 })
	);
	assert.equal(s.payPageToken, "");
	assert.equal(s.payTokenExpiresInS, null);
});

test("only a positive finite duration is kept", () => {
	// Admin omits the key rather than sending 0 for a dead token, so a 0 or a
	// negative here is a bug or an older sender. Either way "expires in 0 minutes"
	// must never appear over a link that still works.
	for (const bad of [0, -30, "soon", null]) {
		const s = reduce(initialState(), token({ pay_token_expires_in_s: bad }));
		assert.equal(s.payPageToken, "tok_1", "the token itself is still honoured");
		assert.equal(s.payTokenExpiresInS, null, `expected null for ${String(bad)}`);
	}
});

test("a later answer without a token clears the earlier countdown", () => {
	// The regression this pins: a customer sits on the pay step, a later answer
	// drops the token, and a stale "43 more minutes" keeps reassuring them while
	// nothing behind it works any more.
	const live = reduce(initialState(), token({ pay_token_expires_in_s: 2580 }));
	assert.equal(live.payTokenExpiresInS, 2580);
	const after = reduce(live, at(CODES.PAYMENT_CONFIRMATION_PENDING, {}));
	assert.equal(after.payPageToken, "");
	assert.equal(after.payTokenExpiresInS, null);
});

// ---------------------------------------------------------------------------
// jarvis#297 P0-2a: the email-verification dead end - restart-to-change-email
// and the resend capability flag/cooldown.
// ---------------------------------------------------------------------------
test("SIGNUP_VERIFICATION_REQUIRED is restart-safe (no gateway object exists yet)", () => {
	const s = reduce(
		initialState(),
		at(CODES.SIGNUP_VERIFICATION_REQUIRED, { pending_verification: true })
	);
	assert.equal(s.value, STATES.VERIFICATION_REQUIRED);
	assert.equal(canSafelyRestart(s), true);
	const after = reduce(s, { type: EVENTS.RESTART });
	assert.equal(after.value, STATES.REVIEW);
	assert.equal(after.code, "");
});

test("canResendVerification defaults closed and only opens when the answer grants it", () => {
	const closed = reduce(
		initialState(),
		at(CODES.SIGNUP_VERIFICATION_REQUIRED, { pending_verification: true })
	);
	assert.equal(closed.canResendVerification, false);
	const open = reduce(
		initialState(),
		at(CODES.SIGNUP_VERIFICATION_REQUIRED, {
			pending_verification: true,
			can_resend_verification: true,
		})
	);
	assert.equal(open.canResendVerification, true);
});

test("canResendVerification is re-read fresh, never sticky", () => {
	// Unlike canReconnect (which latches once true), a capability that stops
	// being repeated must read as false again - the fail-closed default binds on
	// every answer, not just the first.
	const first = reduce(
		initialState(),
		at(CODES.SIGNUP_VERIFICATION_REQUIRED, {
			pending_verification: true,
			can_resend_verification: true,
		})
	);
	assert.equal(first.canResendVerification, true);
	const second = reduce(
		first,
		at(CODES.SIGNUP_VERIFICATION_REQUIRED, { pending_verification: true })
	);
	assert.equal(second.canResendVerification, false);
});

test("noteVerificationResent starts a client-side cooldown read by remainingResendCooldownSeconds", () => {
	const s = initialState();
	assert.equal(remainingResendCooldownSeconds(s, 1_000_000), 0);
	const resent = noteVerificationResent(s, 1_000_000);
	assert.equal(remainingResendCooldownSeconds(resent, 1_000_000), 30);
	assert.equal(remainingResendCooldownSeconds(resent, 1_015_000), 15);
	assert.equal(remainingResendCooldownSeconds(resent, 1_030_000), 0);
	assert.equal(remainingResendCooldownSeconds(resent, 1_999_999), 0);
});
