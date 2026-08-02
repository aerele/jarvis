// The pay page's transition table. Pure: no Vue, no network, no timers - the
// orchestrator owns every side effect so this file can assert the ONE rule the
// whole plan exists for: nothing but an authoritative paid answer leaves the
// Pay page.

import test from "node:test";
import assert from "node:assert/strict";

import { CODES, ADMIN_CODES, BENCH_CODES, ACTIONS, copyFor } from "./paymentCodes.js";
import {
	STATES,
	EVENTS,
	HANDLE_KEYS,
	initialState,
	reduce,
	canOpenCheckout,
	handlesForProvider,
	isTerminalForPayment,
	provisioningOwner,
	remainingCooldownSeconds,
	sanitizeCheckoutNote,
} from "./paymentMachine.js";

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

test("a live intent with handles is checkout-openable, and pending is not a failure", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			razorpay_order_id: "order_1",
			razorpay_key_id: "k",
			payment_provider: "razorpay",
			can_initiate_payment: true,
			can_check_status: true,
		})
	);
	assert.equal(s.value, STATES.UNKNOWN);
	assert.equal(s.provider, "razorpay");
	assert.equal(s.handles.razorpay_order_id, "order_1");
	assert.equal(s.canCheck, true);
});

test("a decline is retryable and stays on Pay", () => {
	const s = reduce(initialState(), at(CODES.PAYMENT_DECLINED, { can_initiate_payment: true }));
	assert.equal(s.value, STATES.FAILED_RETRYABLE);
	assert.equal(s.canInitiate, true);
});

test("SIGNUP_TERMINAL is terminal: no blind payment retry", () => {
	const s = reduce(
		initialState(),
		at(CODES.SIGNUP_TERMINAL, {
			subscription_status: "Cancelled",
			can_initiate_payment: false,
		})
	);
	assert.equal(s.value, STATES.FAILED_TERMINAL);
	assert.equal(s.canInitiate, false);
});

test("PAYMENT_ALREADY_ACTIVE is the only code that means paid", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_ALREADY_ACTIVE, {
			subscription_status: "Active",
			can_initiate_payment: false,
		})
	);
	assert.equal(s.value, STATES.PAID);
	assert.equal(s.canInitiate, false);
});

// ---------------------------------------------------------------------------
// the two NEW states
// ---------------------------------------------------------------------------
test("PAYMENT_AUTHORIZED_PENDING_CONFIRM lands on confirm_required, not on a retry", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM, {
			razorpay_subscription_id: "sub_1",
			payment_provider: "razorpay",
			can_initiate_payment: false,
			can_check_status: true,
		})
	);
	assert.equal(s.value, STATES.CONFIRM_REQUIRED);
	assert.equal(s.canInitiate, false);
	// Money already moved at the gateway; a second intent authorizes a second
	// mandate. The handles are kept because confirm needs them.
	assert.equal(s.handles.razorpay_subscription_id, "sub_1");
});

test("ACCOUNT_RECONNECT_REQUIRED lands on reconnect - never on a fake paid state", () => {
	const s = reduce(
		initialState(),
		at(CODES.ACCOUNT_RECONNECT_REQUIRED, {
			subscription_status: "Active",
			can_reconnect: true,
			can_initiate_payment: false,
		})
	);
	assert.equal(s.value, STATES.RECONNECT);
	assert.equal(s.canReconnect, true);
	assert.equal(s.canInitiate, false);
});

test("can_reconnect on any envelope raises the offer without changing the state", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { can_reconnect: true })
	);
	assert.equal(s.canReconnect, true);
	assert.equal(s.value, STATES.UNKNOWN);
});

// ---------------------------------------------------------------------------
// checkout: dismissal, late callbacks, and the mandatory check-on-failure
// ---------------------------------------------------------------------------
test("a dismissed sheet is unknown, never 'failed' - and never advances", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	s = reduce(s, { type: EVENTS.CHECKOUT_OPENED });
	assert.equal(s.value, STATES.CHECKOUT_OPEN);
	s = reduce(s, { type: EVENTS.CHECKOUT_DISMISSED });
	assert.equal(s.value, STATES.UNKNOWN);
	assert.equal(s.checkRequired, true, "a closed sheet must force a status check");
});

test("an SDK failure before the sheet opens is retryable and also forces a check", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	s = reduce(s, { type: EVENTS.CHECKOUT_FAILED, message: "Could not load the payment form." });
	assert.equal(s.value, STATES.FAILED_RETRYABLE);
	assert.equal(s.checkRequired, true);
});

test("a gateway callback confirms; a confirm timeout falls back to unknown, not to paid", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	s = reduce(s, { type: EVENTS.CHECKOUT_OPENED });
	s = reduce(s, { type: EVENTS.GATEWAY_CALLBACK });
	assert.equal(s.value, STATES.CONFIRMING);
	s = reduce(s, {
		type: EVENTS.CONFIRM_FAILED,
		decoded: { ok: false, code: "", message: "timeout" },
	});
	assert.equal(s.value, STATES.UNKNOWN);
	assert.equal(s.checkRequired, true);
});

test("a confirm that returns a coded decline renders the decline", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	s = reduce(s, { type: EVENTS.GATEWAY_CALLBACK });
	s = reduce(s, {
		type: EVENTS.CONFIRM_FAILED,
		decoded: { ok: false, code: CODES.PAYMENT_DECLINED, message: "not authorized" },
	});
	assert.equal(s.value, STATES.FAILED_RETRYABLE);
	assert.equal(s.code, CODES.PAYMENT_DECLINED);
});

test("CONFIRM_SUCCEEDED is paid", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	s = reduce(s, { type: EVENTS.GATEWAY_CALLBACK });
	s = reduce(s, { type: EVENTS.CONFIRM_SUCCEEDED, data: { tenant_status: "running" } });
	assert.equal(s.value, STATES.PAID);
});

// ---------------------------------------------------------------------------
// paid is monotonic
// ---------------------------------------------------------------------------
test("dismiss AFTER a success stays paid", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	s = reduce(s, { type: EVENTS.GATEWAY_CALLBACK });
	s = reduce(s, { type: EVENTS.CONFIRM_SUCCEEDED, data: {} });
	s = reduce(s, { type: EVENTS.CHECKOUT_DISMISSED });
	assert.equal(s.value, STATES.PAID);
});

test("a late pending answer never regresses a paid page", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_ALREADY_ACTIVE, { subscription_status: "Active" })
	);
	assert.equal(s.value, STATES.PAID);
	s = reduce(s, at(CODES.PAYMENT_CONFIRMATION_PENDING));
	assert.equal(s.value, STATES.PAID);
});

test("a late decline never regresses a paid page either", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	s = reduce(s, at(CODES.PAYMENT_DECLINED));
	assert.equal(s.value, STATES.PAID);
});

test("provisioning does not regress to a payment state", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	s = reduce(s, { type: EVENTS.PROVISIONING_STARTED });
	assert.equal(s.value, STATES.PROVISIONING);
	s = reduce(s, at(CODES.PAYMENT_CONFIRMATION_PENDING));
	assert.equal(s.value, STATES.PROVISIONING);
});

// ---------------------------------------------------------------------------
// the generation fence: two tabs, a stale response, a superseded intent
// ---------------------------------------------------------------------------
test("a response from an OLDER generation is ignored outright", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 3 }));
	const before = s;
	s = reduce(s, at(CODES.PAYMENT_DECLINED, { generation: 2 }));
	assert.equal(s, before, "a stale generation must not even produce a new object");
});

test("a NEWER generation supersedes, and clears the previous intent's handles", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			generation: 1,
			razorpay_order_id: "order_old",
		})
	);
	s = reduce(
		s,
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			generation: 2,
			razorpay_order_id: "order_new",
		})
	);
	assert.equal(s.generation, 2);
	assert.equal(s.handles.razorpay_order_id, "order_new");
});

test("an envelope with no generation is not treated as generation zero", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 4 }));
	const next = reduce(
		s,
		CONTRACT({ code: CODES.PAYMENT_DECLINED, data: { attempt_id: "att_1" } })
	);
	// No generation to compare: the answer is accepted (a legacy admin), but the
	// known generation survives it.
	assert.equal(next.value, STATES.FAILED_RETRYABLE);
	assert.equal(next.generation, 4);
});

// ---------------------------------------------------------------------------
// the rate limit is an OVERLAY, not a state
// ---------------------------------------------------------------------------
test("a 429 leaves the payment state exactly where it was", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	const before = s.value;
	s = reduce(s, {
		type: EVENTS.RATE_LIMITED,
		retryAfterSeconds: 30,
		nowMs: 1_000_000,
	});
	assert.equal(s.value, before);
	assert.equal(s.checkCooldownUntil, 1_000_000 + 30_000);
	assert.equal(s.canCheck, false, "the check button is the only thing a 429 disables");
});

test("a COLD 429 renders the rate-limit row, not the alarming catch-all", () => {
	// The first thing this page has ever been told is "you asked too often". The
	// cooldown alone left `code` empty, which rendered "We could not determine the
	// payment status" - a sentence about the MONEY - for a message that says
	// nothing about the money at all. The rate-limit row's own copy was
	// unreachable in production.
	const s = reduce(initialState(), {
		type: EVENTS.CONTRACT_STATE,
		decoded: {
			ok: false,
			code: CODES.PAYMENT_CHECK_RATE_LIMITED,
			retryAfterSeconds: 30,
			data: {},
			context: {},
		},
	});
	assert.equal(s.code, CODES.PAYMENT_CHECK_RATE_LIMITED);
	assert.equal(copyFor(s.code).headline, copyFor(CODES.PAYMENT_CHECK_RATE_LIMITED).headline);
	assert.ok(s.checkCooldownUntil > 0);
});

test("a 429 over a KNOWN payment state leaves that state's copy alone", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { can_check_status: true })
	);
	s = reduce(s, {
		type: EVENTS.CONTRACT_STATE,
		decoded: {
			ok: false,
			code: CODES.PAYMENT_CHECK_RATE_LIMITED,
			retryAfterSeconds: 30,
			data: {},
			context: {},
		},
	});
	assert.equal(s.code, CODES.PAYMENT_CONFIRMATION_PENDING, "the money is still where it was");
	assert.ok(s.checkCooldownUntil > 0, "but the check is still cooled down");
});

test("a 429 with no hint still cools down for a sane default", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING));
	s = reduce(s, { type: EVENTS.RATE_LIMITED, nowMs: 0 });
	assert.ok(s.checkCooldownUntil > 0);
});

test("the countdown counts down, rounds UP, and never goes negative", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING));
	s = reduce(s, { type: EVENTS.RATE_LIMITED, retryAfterSeconds: 30, nowMs: 1000 });
	assert.equal(remainingCooldownSeconds(s, 1000), 30);
	assert.equal(
		remainingCooldownSeconds(s, 1500),
		30,
		"half a second left still reads as a second"
	);
	assert.equal(remainingCooldownSeconds(s, 21_000), 10);
	assert.equal(remainingCooldownSeconds(s, 31_000), 0);
	assert.equal(remainingCooldownSeconds(s, 99_000), 0);
});

test("the cooldown lifts on its own once the clock passes it", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { can_check_status: true })
	);
	s = reduce(s, { type: EVENTS.RATE_LIMITED, retryAfterSeconds: 5, nowMs: 0 });
	assert.equal(s.canCheck, false);
	s = reduce(s, { type: EVENTS.COOLDOWN_ELAPSED, nowMs: 6000 });
	assert.equal(s.canCheck, true);
	assert.equal(s.checkCooldownUntil, 0);
});

// ---------------------------------------------------------------------------
// transport failures say nothing about the money
// ---------------------------------------------------------------------------
test("a transport failure does not overwrite a known payment state", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_DECLINED));
	s = reduce(s, {
		type: EVENTS.CONTRACT_STATE,
		decoded: { ok: false, code: CODES.BENCH_ADMIN_UNREACHABLE, message: "timeout", data: {} },
	});
	assert.equal(s.code, CODES.PAYMENT_DECLINED, "the last known payment code survives");
	assert.equal(s.value, STATES.FAILED_RETRYABLE);
	assert.ok(s.transportError, "but the page must say the check itself failed");
});

test("a transport failure on a page that knows NOTHING renders unknown", () => {
	const s = reduce(initialState(), {
		type: EVENTS.CONTRACT_STATE,
		decoded: { ok: false, code: CODES.BENCH_ADMIN_UNREACHABLE, message: "timeout", data: {} },
	});
	assert.equal(s.value, STATES.UNKNOWN);
});

// ---------------------------------------------------------------------------
// the day-one guard
// ---------------------------------------------------------------------------
test("BENCH_NO_SIGNUP_CONTEXT is a fresh start, never a support screen", () => {
	const s = reduce(initialState(), {
		type: EVENTS.CONTRACT_STATE,
		decoded: { ok: false, code: CODES.BENCH_NO_SIGNUP_CONTEXT, message: "", data: {} },
	});
	assert.equal(s.value, STATES.REVIEW);
	assert.equal(s.notStarted, true);
});

// ---------------------------------------------------------------------------
// the money-parked refusal
// ---------------------------------------------------------------------------
test("BENCH_AWAITING_RECONCILIATION suppresses the pay affordance", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { can_initiate_payment: true })
	);
	s = reduce(s, {
		type: EVENTS.CONTRACT_STATE,
		decoded: {
			ok: false,
			code: CODES.BENCH_AWAITING_RECONCILIATION,
			message: "still confirming",
			recovery: "check_status",
			data: {},
		},
	});
	assert.equal(s.canInitiate, false);
	assert.equal(s.awaitingReconciliation, true);
});

test("BENCH_AWAITING_RECONCILIATION also arrives from the SIGNUP submit path", () => {
	// start_signup refuses too, not only initiate: money is parked and the one
	// thing that must not happen next is another checkout - including a fresh
	// one opened by clicking Pay on a page that thinks nothing has started.
	let s = reduce(initialState(), { type: EVENTS.SUBMIT_REVIEW });
	assert.equal(s.value, STATES.STARTING_SIGNUP);
	s = reduce(s, {
		type: EVENTS.CONTRACT_STATE,
		decoded: {
			ok: false,
			code: CODES.BENCH_AWAITING_RECONCILIATION,
			message: "we're still confirming a payment on this signup",
			recovery: "check_status",
			data: {},
		},
	});
	assert.equal(s.value, STATES.UNKNOWN);
	assert.equal(s.awaitingReconciliation, true);
	assert.equal(s.canInitiate, false);
	assert.equal(s.canCheck, true, "check status is the recovery this code names");
	assert.equal(s.busy, null);
});

test("the reconciliation FLAG on an ordinary pending answer also suppresses it", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			can_initiate_payment: false,
			awaiting_manual_reconciliation: true,
		})
	);
	assert.equal(s.awaitingReconciliation, true);
	assert.equal(s.canInitiate, false);
});

// ---------------------------------------------------------------------------
// THE DEAD-END INVARIANT
//
// The class-level guard, not a point fix: walk every code the facade can send,
// on a COLD MOUNT (where a failure envelope carries no `data` at all, so no
// capability flag can ever arrive - onboarding_contract.failure returns only
// {ok:false, error, context}), and assert the rendered screen always leaves the
// customer at least one thing to press. A screen with no enabled control is a
// dead end whatever its copy says.
// ---------------------------------------------------------------------------
function enabledActionsFor(s) {
	const copy = copyFor(s.code, { awaitingReconciliation: s.awaitingReconciliation });
	const acts = [...copy.actions];
	if (s.supportOffered && !acts.includes(ACTIONS.SUPPORT)) acts.push(ACTIONS.SUPPORT);
	return acts.filter((a) => {
		if (a === ACTIONS.CHECK) return s.canCheck;
		if (a === ACTIONS.INITIATE) return s.canInitiate;
		return true; // support / reconnect / restart / verify / continue are always live
	});
}

// A screen is a DEAD END when nothing is pressable AND nothing tells the
// customer that something is about to become pressable. A rate-limit cooldown is
// deliberately NOT a dead end: its button is disabled, but it carries a live
// countdown ("Check again in 30s") and re-arms itself, which is a timed wait
// rather than a trap. Anything else with no live control is the real defect.
function isDeadEnd(s, nowMs = 0) {
	if (enabledActionsFor(s).length > 0) return false;
	return remainingCooldownSeconds(s, nowMs) <= 0;
}

test("INVARIANT: every code, arriving as a cold-mount FAILURE, leaves an enabled action", () => {
	for (const code of [...ADMIN_CODES, ...BENCH_CODES]) {
		const s = reduce(initialState(), {
			type: EVENTS.CONTRACT_STATE,
			// exactly what onboarding_contract.failure() puts on the wire: no `data`
			decoded: { ok: false, code, message: "", recovery: "", data: {}, context: {} },
		});
		assert.ok(
			!isDeadEnd(s),
			`DEAD END: ${code} (state=${s.value}) renders no enabled action and no countdown`
		);
	}
});

test("INVARIANT: every code, arriving as a SUCCESS with no capability flags, leaves an enabled action", () => {
	for (const code of [...ADMIN_CODES, ...BENCH_CODES]) {
		const s = reduce(initialState(), {
			type: EVENTS.CONTRACT_STATE,
			decoded: { ok: true, code, data: { attempt_id: "att_1" }, context: {} },
		});
		assert.ok(
			!isDeadEnd(s),
			`DEAD END: ${code} (state=${s.value}) renders no enabled action and no countdown`
		);
	}
});

test("INVARIANT: a rate-limited page still has a live action once the window passes", () => {
	let s = reduce(initialState(), {
		type: EVENTS.CONTRACT_STATE,
		decoded: { ok: false, code: CODES.PAYMENT_CHECK_RATE_LIMITED, data: {}, context: {} },
	});
	s = reduce(s, { type: EVENTS.RATE_LIMITED, retryAfterSeconds: 60, nowMs: 1000 });
	assert.equal(s.canCheck, false, "cooled down while the window is open");
	s = reduce(s, { type: EVENTS.COOLDOWN_ELAPSED, nowMs: 62_000 });
	assert.ok(enabledActionsFor(s).length > 0, "the check must come back after the window");
	assert.ok(!isDeadEnd(s, 62_000));
});

// ---------------------------------------------------------------------------
// the cooldown clock reaches the reducer however the caller passes it
// ---------------------------------------------------------------------------
test("COOLDOWN_ELAPSED lifts the cooldown when the clock arrives in OPTS (the flow's shape)", () => {
	// usePaymentFlow.apply() passes the clock in opts, not on the event. Reading
	// only event.nowMs made the guard see 0 forever, so the Check button looked
	// armed (its label recovers from the view's own clock) and was permanently
	// dead - leaving "pay again" as the only live control on the page.
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { can_check_status: true })
	);
	s = reduce(s, { type: EVENTS.RATE_LIMITED, retryAfterSeconds: 45, nowMs: 1_000_000 });
	assert.equal(s.canCheck, false);
	const viaOpts = reduce(s, { type: EVENTS.COOLDOWN_ELAPSED }, { nowMs: 1_046_000 });
	assert.equal(viaOpts.checkCooldownUntil, 0);
	assert.equal(viaOpts.canCheck, true);
});

test("RATE_LIMITED takes its clock from opts too", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { can_check_status: true })
	);
	s = reduce(s, { type: EVENTS.RATE_LIMITED, retryAfterSeconds: 30 }, { nowMs: 500_000 });
	assert.equal(s.checkCooldownUntil, 530_000);
});

// ---------------------------------------------------------------------------
// paid is a floor - the guards that had NO coverage (deleting them stayed green)
// ---------------------------------------------------------------------------
test("a late CONFIRM_FAILED cannot unseat a paid page", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	assert.equal(s.value, STATES.PAID);
	// code:"" deliberately - that is the shape confirmCashfreeLoop's ceiling exit
	// emits verbatim, and it is the ONLY shape that reaches CONFIRM_FAILED's own
	// paid-floor guard. A coded decline here would route through applyContract and
	// be caught by THAT floor instead, leaving this guard untested.
	s = reduce(s, {
		type: EVENTS.CONFIRM_FAILED,
		decoded: { ok: false, code: "", message: "late timeout" },
	});
	assert.equal(s.value, STATES.PAID);
});

test("a late CHECKOUT_FAILED cannot unseat a paid page", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	s = reduce(s, { type: EVENTS.CHECKOUT_FAILED, message: "sdk died after the fact" });
	assert.equal(s.value, STATES.PAID);
});

test("a late GATEWAY_CALLBACK cannot drag a paid page back into confirming", () => {
	let s = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	s = reduce(s, { type: EVENTS.GATEWAY_CALLBACK });
	assert.equal(s.value, STATES.PAID);
});

// ---------------------------------------------------------------------------
// the generation fence, including the gen-less hole
// ---------------------------------------------------------------------------
test("an answer with NO generation never merges its handles over a live intent", () => {
	// A legacy/failure answer carries no generation, so it cannot be attributed
	// to the current intent. Its CODE is still honoured (an older control plane
	// is entitled to report a decline), but its handles are not: merging them
	// resurrected a dead order id alongside a live one.
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			generation: 5,
			razorpay_order_id: "order_LIVE",
		})
	);
	s = reduce(
		s,
		CONTRACT({
			code: CODES.PAYMENT_DECLINED,
			data: { attempt_id: "att_1", razorpay_order_id: "order_ANCIENT" },
		})
	);
	assert.equal(s.generation, 5);
	assert.equal(
		s.handles.razorpay_order_id,
		"order_LIVE",
		"a gen-less answer must not replant handles"
	);
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
	const out = reduce(s, { type: EVENTS.PROVISIONING_STARTED });
	assert.equal(out.value, STATES.UNKNOWN);
	assert.equal(out.illegalTransitions, 1);
});

test("a checkout cannot be opened from a state with no handles", () => {
	const s = initialState();
	assert.throws(() => reduce(s, { type: EVENTS.CHECKOUT_OPENED }, { strict: true }));
});

// ---------------------------------------------------------------------------
// canOpenCheckout: the ONE predicate the orchestrator must ask before it opens
// ---------------------------------------------------------------------------
test("canOpenCheckout answers exactly what CHECKOUT_OPENED will accept", () => {
	const cold = initialState();
	assert.equal(canOpenCheckout(cold), false);
	const live = reduce(cold, at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" }));
	assert.equal(canOpenCheckout(live), true);
	assert.equal(reduce(live, { type: EVENTS.CHECKOUT_OPENED }).value, STATES.CHECKOUT_OPEN);
	// The handles the reducer REFUSED to merge are not openable, whatever the
	// answer that carried them said: a gen-less answer over a known generation
	// contributes none, and a losing generation is discarded whole.
	const unattributable = reduce(
		live,
		CONTRACT({
			code: CODES.PAYMENT_CONFIRMATION_PENDING,
			data: { attempt_id: "att_1", razorpay_order_id: "order_UNATTRIBUTABLE" },
		})
	);
	assert.equal(unattributable.handles.razorpay_order_id, "o");
	const noHandles = reduce(
		initialState(),
		CONTRACT({
			code: CODES.PAYMENT_CONFIRMATION_PENDING,
			data: { attempt_id: "att_1", generation: 1 },
		})
	);
	assert.equal(canOpenCheckout(noHandles), false);
	assert.equal(canOpenCheckout(null), false);
	assert.equal(canOpenCheckout({}), false);
});

// ---------------------------------------------------------------------------
// handlesForProvider: the sheet is built for ONE gateway
// ---------------------------------------------------------------------------
test("the provider partition covers every handle the reducer keeps", () => {
	// The fixture is BUILT FROM the real HANDLE_KEYS, so a key added to the table
	// enters `kept` automatically and fails here for having no gateway family.
	// Hardcoding the fixture made this alarm silent on exactly that drift: the
	// reducer copies only the keys an answer actually carried, so a new table
	// entry the fixture did not mention never reached the assertion.
	const answer = { not_a_handle: "x" };
	for (const k of HANDLE_KEYS) answer[k] = k === "amount_inr" ? 4999 : `v_${k}`;
	const s = reduce(initialState(), at(CODES.PAYMENT_CONFIRMATION_PENDING, answer));
	const kept = Object.keys(s.handles);
	assert.equal(kept.includes("not_a_handle"), false);
	assert.deepEqual(
		kept.slice().sort(),
		HANDLE_KEYS.slice().sort(),
		"the reducer keeps exactly the table"
	);
	const rzp = Object.keys(handlesForProvider(s.handles, "razorpay"));
	const cfr = Object.keys(handlesForProvider(s.handles, "cashfree"));
	for (const k of kept) {
		assert.ok(rzp.includes(k) || cfr.includes(k), `${k} belongs to no gateway family`);
	}
	// Only the price is shared; everything else lands in exactly one family.
	assert.deepEqual(
		rzp.filter((k) => cfr.includes(k)),
		["amount_inr"]
	);
	assert.deepEqual(rzp.slice().sort(), [
		"amount_inr",
		"razorpay_key_id",
		"razorpay_order_id",
		"razorpay_subscription_id",
	]);
	assert.deepEqual(cfr.slice().sort(), [
		"amount_inr",
		"cashfree_app_id",
		"cashfree_env",
		"cashfree_order_id",
		"cashfree_subscription_id",
		"payment_session_id",
		"subscription_session_id",
	]);
});

test("an unnamed or unrecognised gateway is not a licence to drop handles", () => {
	const h = { razorpay_order_id: "o", razorpay_key_id: "k", cashfree_env: "sandbox" };
	assert.deepEqual(handlesForProvider(h, ""), h);
	assert.deepEqual(handlesForProvider(h, null), h);
	assert.deepEqual(handlesForProvider(h, "some_new_gateway"), h);
	// ...and the recognised names are matched the way billingCheckout matches them.
	assert.deepEqual(handlesForProvider(h, " Razorpay "), {
		razorpay_order_id: "o",
		razorpay_key_id: "k",
	});
	assert.deepEqual(handlesForProvider(null, "razorpay"), {});
});

test("a named gateway that can open nothing yields to the one that can", () => {
	// The provider field is sticky, so it can name a gateway whose keys are all
	// stale. That is a stale LABEL over a set that is coherent for the OTHER
	// gateway - only one family can hold the openable handles once the named one
	// holds none - so the sheet is built from that family rather than from the
	// whole set. Handing over the named family's leftovers is what let a rider
	// key (one that opens nothing but still classifies) claim the sheet.
	const cashfreeOnly = {
		payment_session_id: "ps",
		cashfree_order_id: "co",
		cashfree_env: "sandbox",
	};
	assert.deepEqual(handlesForProvider(cashfreeOnly, "razorpay"), cashfreeOnly);
	// razorpay_key_id opens nothing on its own, so it does not make the razorpay
	// family the one to build from - and it does not ride along either.
	assert.deepEqual(
		handlesForProvider({ ...cashfreeOnly, razorpay_key_id: "k" }, "razorpay"),
		cashfreeOnly
	);
	// The mirror: a stale Cashfree subscription id is not openable, so a live
	// Razorpay order decides the set - and no Cashfree key reaches the sheet.
	assert.deepEqual(
		handlesForProvider(
			{
				cashfree_subscription_id: "cs",
				cashfree_env: "sandbox",
				razorpay_order_id: "o",
				razorpay_key_id: "k",
			},
			"cashfree"
		),
		{ razorpay_order_id: "o", razorpay_key_id: "k" }
	);
	// Nothing openable anywhere: there is no family to prefer, so the set is left
	// exactly as it was (runCheckout has already refused to open it).
	const dead = { razorpay_key_id: "k", cashfree_env: "sandbox" };
	assert.deepEqual(handlesForProvider(dead, "razorpay"), dead);
});

test("paid is a floor for the OPEN too - a late sheet cannot reopen a settled signup", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	// Reach paid the way the flow always does - a real confirm-in-progress. (The
	// reducer now refuses CONFIRM_SUCCEEDED from any state but `confirming`, so a
	// shortcut here would be testing an event the orchestrator never fires.)
	s = reduce(s, { type: EVENTS.GATEWAY_CALLBACK });
	s = reduce(s, { type: EVENTS.CONFIRM_SUCCEEDED, data: {} });
	assert.equal(s.value, STATES.PAID);
	// The handles survive the confirm by design, so nothing but the floor stands
	// between a late/duplicate open and a live sheet over a paid signup.
	assert.equal(s.handles.razorpay_order_id, "o");
	assert.equal(canOpenCheckout(s), false);
	const out = reduce(s, { type: EVENTS.CHECKOUT_OPENED }, { strict: true });
	assert.equal(out.value, STATES.PAID, "a late open is a no-op above the floor");
	assert.equal(out.illegalTransitions, 0, "...and a no-op, not a counted illegality");
});

// ---------------------------------------------------------------------------
// the provisioning boundary
// ---------------------------------------------------------------------------
test("provisioning is owned by the readiness gate, not by the payment endpoints", () => {
	assert.equal(provisioningOwner(STATES.PROVISIONING), "readiness");
	assert.equal(provisioningOwner(STATES.PROVISIONING_DELAYED), "readiness");
	assert.equal(provisioningOwner(STATES.UNKNOWN), "payment");
	assert.equal(provisioningOwner(STATES.CONFIRM_REQUIRED), "payment");
});

test("isTerminalForPayment covers exactly the states that must never show a pay button", () => {
	assert.equal(isTerminalForPayment(STATES.PAID), true);
	assert.equal(isTerminalForPayment(STATES.PROVISIONING), true);
	assert.equal(isTerminalForPayment(STATES.PROVISIONING_DELAYED), true);
	assert.equal(isTerminalForPayment(STATES.FAILED_TERMINAL), true);
	assert.equal(isTerminalForPayment(STATES.RECONNECT), true);
	assert.equal(isTerminalForPayment(STATES.UNKNOWN), false);
	assert.equal(isTerminalForPayment(STATES.FAILED_RETRYABLE), false);
});

// ---------------------------------------------------------------------------
// rehydration: server truth wins over anything the page remembers
// ---------------------------------------------------------------------------
test("REHYDRATE takes the summary from the server context, never from a prefill", () => {
	const s = reduce(
		initialState(),
		CONTRACT({
			code: CODES.PAYMENT_CONFIRMATION_PENDING,
			data: {
				attempt_id: "att_9",
				generation: 1,
				email: "real@customer.com",
				company: "Acme",
				plan: { name: "pro", label: "Pro" },
				amount_inr: 12000,
				due_today_inr: 12000,
			},
			context: { email: "real@customer.com", company: "Acme", plan_label: "Pro" },
		})
	);
	assert.equal(s.summary.email, "real@customer.com");
	assert.equal(s.summary.company, "Acme");
	assert.equal(s.summary.planLabel, "Pro");
	assert.equal(s.summary.dueTodayInr, 12000);
	assert.equal(s.attemptId, "att_9");
});

test("'last checked' is read from data, never from the persisted context", () => {
	// Zero-write polling: in steady state the persisted context deliberately
	// keeps a STALE payment_last_checked_at (a write per poll would clear the
	// document cache for every request on the site). The fresh stamp rides in
	// `data`, so a page that rendered the context's copy would tell the customer
	// their payment was last checked minutes before it actually was.
	const s = reduce(
		initialState(),
		CONTRACT({
			code: CODES.PAYMENT_CONFIRMATION_PENDING,
			data: {
				attempt_id: "att_1",
				generation: 1,
				payment_last_checked_at: "2026-08-02 10:30:00.000000",
			},
			context: { payment_last_checked_at: "2026-08-02 09:00:00.000000" },
		})
	);
	assert.equal(s.lastCheckedAt, "2026-08-02 10:30:00.000000");
});

test("an envelope with no fresh stamp keeps the previous one rather than inventing one", () => {
	let s = reduce(
		initialState(),
		CONTRACT({
			code: CODES.PAYMENT_CONFIRMATION_PENDING,
			data: {
				attempt_id: "att_1",
				generation: 1,
				payment_last_checked_at: "2026-08-02 10:30:00",
			},
			context: {},
		})
	);
	s = reduce(
		s,
		CONTRACT({
			code: CODES.PAYMENT_CONFIRMATION_PENDING,
			data: { attempt_id: "att_1", generation: 1 },
			context: { payment_last_checked_at: "2026-08-02 09:00:00" },
		})
	);
	assert.equal(s.lastCheckedAt, "2026-08-02 10:30:00");
});

test("trial disclosure survives into the summary", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			trial_days: 14,
			effective_trial_days: 14,
			due_today_inr: 0,
			amount_inr: 999,
			razorpay_subscription_id: "sub_1",
		})
	);
	assert.equal(s.summary.trialDays, 14);
	assert.equal(s.summary.dueTodayInr, 0);
	assert.equal(s.isMandate, true);
});

// ===========================================================================
// Round-2 hardening: fail-closed checkout, attempt fence, callback identity,
// the safe checkout-return exit, and the server-truth-gated restart.
// ===========================================================================

// ---- P1-1: canOpenCheckout fails closed on the source state ---------------
test("P1-1: a retained handle cannot open a sheet from a settled recovery state", () => {
	// Each of these carries a perfectly openable handle, but the STATE forbids a
	// blind Pay: the authorization already exists, the account must reconnect, or
	// the signup is terminal. The old predicate looked only at paid + handle and
	// returned true for all three.
	const handle = { razorpay_order_id: "o", razorpay_key_id: "k", generation: 1 };
	for (const code of [
		CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM,
		CODES.ACCOUNT_RECONNECT_REQUIRED,
		CODES.SIGNUP_TERMINAL,
	]) {
		const s = reduce(initialState(), at(code, handle));
		assert.ok(s.handles.razorpay_order_id, `${code} kept the handle`);
		assert.equal(canOpenCheckout(s), false, `${code} must not open a retained handle`);
		// ...and the reducer's own CHECKOUT_OPENED guard agrees (no drift).
		const out = reduce(s, { type: EVENTS.CHECKOUT_OPENED });
		assert.notEqual(out.value, STATES.CHECKOUT_OPEN, `${code} open is refused`);
	}
});

test("P1-1: verification never opens a retained handle either", () => {
	const s = reduce(
		initialState(),
		at(CODES.SIGNUP_VERIFICATION_REQUIRED, {
			pending_verification: true,
			razorpay_order_id: "o",
			razorpay_key_id: "k",
		})
	);
	assert.equal(canOpenCheckout(s), false);
});

// ---- P1-4: the attempt fence sits ahead of the generation compare ---------
test("P1-4: a different attempt at the SAME generation does not merge handles", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			attempt_id: "att_1",
			generation: 3,
			razorpay_order_id: "order_OLD",
			razorpay_key_id: "k",
		})
	);
	s = reduce(
		s,
		CONTRACT({
			code: CODES.PAYMENT_CONFIRMATION_PENDING,
			data: {
				attempt_id: "att_2",
				generation: 3,
				razorpay_order_id: "order_NEW",
				razorpay_key_id: "k",
			},
		})
	);
	assert.equal(s.attemptId, "att_2");
	assert.equal(s.handles.razorpay_order_id, "order_NEW", "the new attempt replaces");
	assert.equal(s.generation, 3);
});

test("P1-4: a new attempt at a LOWER generation is a replacement, not a stale reject", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			attempt_id: "att_1",
			generation: 5,
			razorpay_order_id: "order_OLD",
			razorpay_key_id: "k",
		})
	);
	s = reduce(
		s,
		CONTRACT({
			code: CODES.PAYMENT_CONFIRMATION_PENDING,
			data: {
				attempt_id: "att_2",
				generation: 2, // lower than the old attempt's 5
				razorpay_order_id: "order_NEW",
				razorpay_key_id: "k",
			},
		})
	);
	// The generation fence must NOT swallow this - it is a different intent.
	assert.equal(s.attemptId, "att_2");
	assert.equal(s.generation, 2);
	assert.equal(s.handles.razorpay_order_id, "order_NEW");
});

test("P1-4: a LOSING generation of the SAME attempt is still rejected outright", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			attempt_id: "att_1",
			generation: 5,
			razorpay_order_id: "order_LIVE",
			razorpay_key_id: "k",
		})
	);
	const before = s;
	s = reduce(
		s,
		CONTRACT({
			code: CODES.PAYMENT_DECLINED,
			data: { attempt_id: "att_1", generation: 3, razorpay_order_id: "order_STALE" },
		})
	);
	assert.equal(s, before, "a same-attempt losing generation is a no-op");
});

// ---- P1-5: gateway-event source + callback-identity guards ----------------
test("P1-5: a stale-attempt callback is a no-op, not a mutation", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			attempt_id: "att_NEW",
			generation: 2,
			razorpay_order_id: "o",
			razorpay_key_id: "k",
		})
	);
	s = reduce(s, { type: EVENTS.CHECKOUT_OPENED });
	assert.equal(s.value, STATES.CHECKOUT_OPEN);
	// A callback from an OLD sheet (att_OLD) must not drive this newer intent.
	const out = reduce(s, { type: EVENTS.GATEWAY_CALLBACK, attemptId: "att_OLD", generation: 1 });
	assert.equal(out.value, STATES.CHECKOUT_OPEN, "stale callback did not confirm the new intent");
});

test("P1-5: gateway events cannot move a settled recovery state", () => {
	const terminal = reduce(initialState(), at(CODES.SIGNUP_TERMINAL));
	assert.equal(terminal.value, STATES.FAILED_TERMINAL);
	// A late dismiss/callback from an old sheet must not unseat terminal/reconnect.
	assert.equal(
		reduce(terminal, { type: EVENTS.CHECKOUT_DISMISSED }).value,
		STATES.FAILED_TERMINAL
	);
	assert.equal(
		reduce(terminal, { type: EVENTS.GATEWAY_CALLBACK }).value,
		STATES.FAILED_TERMINAL
	);
	const reconnect = reduce(initialState(), at(CODES.ACCOUNT_RECONNECT_REQUIRED));
	assert.equal(reduce(reconnect, { type: EVENTS.CHECKOUT_FAILED }).value, STATES.RECONNECT);
});

test("P1-5: CONFIRM_SUCCEEDED is legal only from confirming", () => {
	const s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o", razorpay_key_id: "k" })
	);
	// From UNKNOWN (not confirming): a no-op in production, a throw in strict.
	assert.equal(reduce(s, { type: EVENTS.CONFIRM_SUCCEEDED, data: {} }).value, STATES.UNKNOWN);
	assert.throws(() => reduce(s, { type: EVENTS.CONFIRM_SUCCEEDED, data: {} }, { strict: true }));
});

// ---- P0-2: the safe checkout-return exit ----------------------------------
test("P0-2: RETURNED_FROM_CHECKOUT leaves checkout_open for a checkable unknown", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o", razorpay_key_id: "k" })
	);
	s = reduce(s, { type: EVENTS.CHECKOUT_OPENED });
	assert.equal(s.value, STATES.CHECKOUT_OPEN);
	s = reduce(s, { type: EVENTS.RETURNED_FROM_CHECKOUT });
	assert.equal(s.value, STATES.UNKNOWN, "the sheet is gone; do not assume dismissal");
	assert.equal(s.checkRequired, true, "server truth must be reconciled");
});

test("P0-2: RETURNED_FROM_CHECKOUT never assumes anything from a non-checkout state", () => {
	const paid = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	assert.equal(reduce(paid, { type: EVENTS.RETURNED_FROM_CHECKOUT }).value, STATES.PAID);
	const review = initialState();
	assert.equal(reduce(review, { type: EVENTS.RETURNED_FROM_CHECKOUT }).value, STATES.REVIEW);
});

// ---- P1-3: the server-truth-gated restart ---------------------------------
test("P1-3: RESTART resets a day-one / account-exists state to a fresh review", () => {
	const exists = reduce(
		initialState(),
		CONTRACT({ code: CODES.ACCOUNT_ALREADY_EXISTS, ok: false, data: {} })
	);
	const reset = reduce(exists, { type: EVENTS.RESTART });
	assert.equal(reset.value, STATES.REVIEW);
	assert.equal(reset.code, "");
	assert.equal(reset.handles.razorpay_order_id, undefined);
});

test("P1-3: RESTART preserves a state where a payment may be recoverable", () => {
	// Authorized-pending-confirm: an authorization exists at the gateway. A blind
	// reset would orphan it, so the machine is preserved.
	const authd = reduce(
		initialState(),
		at(CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM, { attempt_id: "att_1", generation: 1 })
	);
	assert.equal(reduce(authd, { type: EVENTS.RESTART }).value, STATES.CONFIRM_REQUIRED);
	// Parked money is preserved too.
	const parked = reduce(
		initialState(),
		CONTRACT({ code: CODES.BENCH_AWAITING_RECONCILIATION, ok: false, data: {} })
	);
	assert.equal(reduce(parked, { type: EVENTS.RESTART }).awaitingReconciliation, true);
	// Paid is never abandoned.
	const paid = reduce(initialState(), at(CODES.PAYMENT_ALREADY_ACTIVE));
	assert.equal(reduce(paid, { type: EVENTS.RESTART }).value, STATES.PAID);
});

// ---------------------------------------------------------------------------
// X1 / B2-1: an open-timeout may not re-arm initiate over a live sheet
// ---------------------------------------------------------------------------
function openSheet() {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	s = reduce(s, { type: EVENTS.CHECKOUT_OPENED });
	assert.equal(s.value, STATES.CHECKOUT_OPEN);
	return s;
}

test("X1: an open-timeout vetoes initiate and offers only a check", () => {
	let s = openSheet();
	s = reduce(s, { type: EVENTS.CHECKOUT_OPEN_TIMED_OUT });
	assert.equal(s.value, STATES.UNKNOWN);
	assert.equal(s.checkoutMayBeOpen, true);
	assert.equal(s.checkRequired, true);
	// The mandatory reconcile lands a pending answer whose can_initiate is TRUE -
	// and the veto must survive it: the page cannot re-arm "start a new payment"
	// while the sheet may still be open and payable.
	s = reduce(
		s,
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			can_initiate_payment: true,
			can_check_status: true,
		})
	);
	assert.equal(s.checkoutMayBeOpen, true, "a check must not clear the veto");
	assert.equal(s.canInitiate, false, "initiate stays vetoed while the sheet may be open");
	assert.equal(s.canCheck, true, "check is the only forward action");
});

test("X1: the veto lifts only when the sheet actually closes and a check runs", () => {
	let s = openSheet();
	s = reduce(s, { type: EVENTS.CHECKOUT_OPEN_TIMED_OUT });
	// The timed-out sheet finally closed with no success.
	s = reduce(s, { type: EVENTS.CHECKOUT_SHEET_CLOSED });
	assert.equal(s.checkoutMayBeOpen, false);
	assert.equal(s.checkRequired, true);
	// Now a check re-arms initiate.
	s = reduce(
		s,
		at(CODES.PAYMENT_CONFIRMATION_PENDING, {
			can_initiate_payment: true,
			can_check_status: true,
		})
	);
	assert.equal(s.canInitiate, true, "initiate re-armed after the sheet closed + a check ran");
});

test("X1: a late gateway callback (a real post-deadline payment) clears the veto", () => {
	let s = openSheet();
	s = reduce(s, { type: EVENTS.CHECKOUT_OPEN_TIMED_OUT });
	assert.equal(s.checkoutMayBeOpen, true);
	// The sheet the customer never closed finally paid: the late callback lands.
	s = reduce(s, { type: EVENTS.GATEWAY_CALLBACK });
	assert.equal(s.value, STATES.CONFIRMING);
	assert.equal(s.checkoutMayBeOpen, false);
});

test("X1: a fresh open clears the veto", () => {
	let s = openSheet();
	s = reduce(s, { type: EVENTS.CHECKOUT_OPEN_TIMED_OUT });
	// A new intent arrives (advanced generation) and its sheet opens.
	s = reduce(
		s,
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { generation: 2, razorpay_order_id: "o2" })
	);
	assert.equal(s.checkoutMayBeOpen, false, "a replacement intent drops the stale veto");
	s = reduce(s, { type: EVENTS.CHECKOUT_OPENED });
	assert.equal(s.checkoutMayBeOpen, false);
});

// ---------------------------------------------------------------------------
// X2: an internal deadline label can never reach the customer-facing note
// ---------------------------------------------------------------------------
test("X2: sanitizeCheckoutNote drops the internal timeout signature, keeps SDK strings", () => {
	assert.equal(sanitizeCheckoutNote("payment request timed out: open"), "");
	assert.equal(sanitizeCheckoutNote("  Payment request timed out: confirm "), "");
	assert.equal(
		sanitizeCheckoutNote("An ad blocker stopped the checkout."),
		"An ad blocker stopped the checkout."
	);
	assert.equal(sanitizeCheckoutNote(""), "");
	assert.equal(sanitizeCheckoutNote(null), "");
});

test("X2: a CHECKOUT_FAILED carrying the internal timeout string stores no note", () => {
	let s = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	s = reduce(s, { type: EVENTS.CHECKOUT_FAILED, message: "payment request timed out: open" });
	assert.equal(s.value, STATES.FAILED_RETRYABLE);
	assert.equal(s.checkoutNote, "", "the internal leak string is filtered at the source");
	// A genuine SDK reason still survives.
	let g = reduce(
		initialState(),
		at(CODES.PAYMENT_CONFIRMATION_PENDING, { razorpay_order_id: "o" })
	);
	g = reduce(g, {
		type: EVENTS.CHECKOUT_FAILED,
		message: "An ad blocker stopped the checkout.",
	});
	assert.equal(g.checkoutNote, "An ad blocker stopped the checkout.");
});
