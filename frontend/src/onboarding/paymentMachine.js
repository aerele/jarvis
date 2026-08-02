/**
 * The pay page's transition table, as a pure reducer.
 *
 * No Vue, no network, no timers. The orchestrator (usePaymentFlow) owns every
 * side effect - API calls, the SDK launch, the poll loops - so this file can
 * hold the ONE invariant the whole plan exists for, and be exhaustively tested
 * for it without a browser: **nothing but an authoritative paid answer leaves
 * the Pay page.** A failed, dismissed, slow, timed-out, callback-lost or
 * unknown-status payment stays put, with the two named recovery actions the
 * backend says are safe.
 *
 * Two rules that show up all over the reducer and are worth stating once:
 *
 *   - **Code, never status.** Every payment verdict comes from `decoded.code`
 *     (admin's contract vocabulary, or a BENCH_* code the facade added). The
 *     HTTP status is a transport signal the codec already consumed; it never
 *     reaches a branch here.
 *   - **Paid is a floor.** Once `paid`/`provisioning` is reached, no later
 *     answer - a stale pending poll, a late decline, a dismissal that fired
 *     after the success handler - moves it back. Late gateway events are
 *     idempotent no-ops.
 *
 * The provisioning states are in this table so the page can RENDER them, but
 * they are not driven by the three payment endpoints: the D1/WS-D readiness gate
 * owns the transition into and out of them (see provisioningOwner). This reducer
 * only accepts an explicit PROVISIONING_STARTED/_DELAYED from that surface and
 * refuses to derive it from a payment answer.
 */

import { CODES } from "./paymentCodes.js";
import { emptyCounter, recordCheck, shouldOfferSupport } from "./supportHandoff.js";

export const STATES = {
	REVIEW: "review",
	STARTING_SIGNUP: "starting_signup",
	VERIFICATION_REQUIRED: "verification_required",
	CHECKOUT_OPEN: "checkout_open",
	CONFIRMING: "confirming",
	UNKNOWN: "unknown",
	FAILED_RETRYABLE: "failed_retryable",
	FAILED_TERMINAL: "failed_terminal",
	CONFIRM_REQUIRED: "confirm_required",
	RECONNECT: "reconnect",
	PAID: "paid",
	PROVISIONING: "provisioning",
	PROVISIONING_DELAYED: "provisioning_delayed",
};

export const EVENTS = {
	SUBMIT_REVIEW: "SUBMIT_REVIEW",
	CONTRACT_STATE: "CONTRACT_STATE",
	CHECKOUT_OPENED: "CHECKOUT_OPENED",
	CHECKOUT_DISMISSED: "CHECKOUT_DISMISSED",
	CHECKOUT_FAILED: "CHECKOUT_FAILED",
	GATEWAY_CALLBACK: "GATEWAY_CALLBACK",
	CONFIRM_SUCCEEDED: "CONFIRM_SUCCEEDED",
	CONFIRM_FAILED: "CONFIRM_FAILED",
	RATE_LIMITED: "RATE_LIMITED",
	COOLDOWN_ELAPSED: "COOLDOWN_ELAPSED",
	PROVISIONING_STARTED: "PROVISIONING_STARTED",
	PROVISIONING_DELAYED: "PROVISIONING_DELAYED",
};

const DEFAULT_COOLDOWN_MS = 30_000;

// States the page must never show a payment action in, because payment is
// either done or cannot be driven forward from here.
const TERMINAL_FOR_PAYMENT = new Set([
	STATES.PAID,
	STATES.PROVISIONING,
	STATES.PROVISIONING_DELAYED,
	STATES.FAILED_TERMINAL,
	STATES.RECONNECT,
]);

// Once here, a payment answer is a no-op: paid is a floor and provisioning sits
// above it. Late polls, late declines and late callbacks cannot move it.
const PAID_FLOOR = new Set([STATES.PAID, STATES.PROVISIONING, STATES.PROVISIONING_DELAYED]);

const HANDLE_KEYS = [
	"razorpay_order_id",
	"razorpay_subscription_id",
	"razorpay_key_id",
	"payment_session_id",
	"cashfree_order_id",
	"cashfree_subscription_id",
	"subscription_session_id",
	"cashfree_app_id",
	"cashfree_env",
	"amount_inr",
];

export function isTerminalForPayment(value) {
	return TERMINAL_FOR_PAYMENT.has(value);
}

/**
 * Who drives a given state: the readiness/connect gate (WS-D) or the payment
 * endpoints (this train). Names the boundary the brief asked for - the reducer
 * renders provisioning but never mints it from a payment answer.
 */
export function provisioningOwner(value) {
	if (value === STATES.PROVISIONING || value === STATES.PROVISIONING_DELAYED) return "readiness";
	return "payment";
}

export function initialState() {
	return {
		value: STATES.REVIEW,
		code: "",
		message: "",
		recovery: "",
		busy: null, // "starting" | "initiating" | "checking" | "confirming" | null
		attemptId: null,
		generation: null,
		provider: null,
		handles: {},
		summary: null,
		lastCheckedAt: null,
		verificationExpiresAt: null,
		isMandate: false,
		canInitiate: false,
		canCheck: false,
		canReconnect: false,
		awaitingReconciliation: false,
		checkRequired: false,
		notStarted: false,
		transportError: false,
		checkCooldownUntil: 0,
		supportChecks: emptyCounter(),
		supportOffered: false,
		illegalTransitions: 0,
	};
}

// ---- helpers ---------------------------------------------------------------

function pickHandles(data) {
	const out = {};
	for (const k of HANDLE_KEYS) if (data[k] != null) out[k] = data[k];
	return out;
}

function buildSummary(prev, data, context) {
	// Server truth over anything the wizard prefilled (C02-3): a resumed page
	// renders admin's identity or an honest blank, never the site admin's email.
	// `plan` is {name,label} on the state/resume surfaces, a bare string on the
	// bench's own signup call - both fold to planLabel.
	const planLabel =
		(data.plan && typeof data.plan === "object" && (data.plan.label || data.plan.name)) ||
		context.plan_label ||
		(typeof data.plan === "string" ? data.plan : "") ||
		(prev && prev.planLabel) ||
		"";
	const num = (v, fallback) => (v === null || v === undefined || v === "" ? fallback : Number(v));
	return {
		email: data.email || context.email || (prev && prev.email) || "",
		company: data.company || context.company || (prev && prev.company) || "",
		planLabel,
		amountInr: num(data.amount_inr, prev ? prev.amountInr : null),
		dueTodayInr: num(data.due_today_inr, prev ? prev.dueTodayInr : null),
		signupFeeInr: num(data.signup_fee_inr, prev ? prev.signupFeeInr : null),
		trialDays: num(data.effective_trial_days ?? data.trial_days, prev ? prev.trialDays : 0) || 0,
	};
}

function isMandateShape(handles) {
	return !!(
		handles.razorpay_subscription_id ||
		handles.subscription_session_id ||
		handles.cashfree_subscription_id
	);
}

// The state a payment CODE maps to, before the floor/fence guards. `null` means
// "this code does not itself set a payment state" (a transport/local code that
// only annotates).
function stateForCode(code, data) {
	switch (code) {
		case CODES.SIGNUP_VERIFICATION_REQUIRED:
			return STATES.VERIFICATION_REQUIRED;
		case CODES.PAYMENT_ALREADY_ACTIVE:
			return STATES.PAID;
		case CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM:
			return STATES.CONFIRM_REQUIRED;
		case CODES.ACCOUNT_RECONNECT_REQUIRED:
			return STATES.RECONNECT;
		case CODES.SIGNUP_TERMINAL:
			return STATES.FAILED_TERMINAL;
		case CODES.PAYMENT_DECLINED:
		case CODES.INTENT_HANDLE_UNAVAILABLE:
		case CODES.NO_CURRENT_INTENT:
			return STATES.FAILED_RETRYABLE;
		case CODES.PAYMENT_CONFIRMATION_PENDING:
			return STATES.UNKNOWN;
		case CODES.INVALID_REQUEST:
			return STATES.FAILED_RETRYABLE;
		default:
			return null;
	}
}

const PAYMENT_STATE_CODES = new Set([
	CODES.SIGNUP_VERIFICATION_REQUIRED,
	CODES.PAYMENT_CONFIRMATION_PENDING,
	CODES.PAYMENT_DECLINED,
	CODES.PAYMENT_ALREADY_ACTIVE,
	CODES.NO_CURRENT_INTENT,
	CODES.ACCOUNT_RECONNECT_REQUIRED,
	CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM,
	CODES.INTENT_HANDLE_UNAVAILABLE,
	CODES.SIGNUP_TERMINAL,
]);

function recomputeCanCheck(next, nowMs) {
	// The check button is live when the backend permits it AND we are not in a
	// rate-limit cooldown. A missing `nowMs` (most reducer calls) leaves an
	// existing cooldown in force - only an explicit clock tick lifts it.
	const cooling = next.checkCooldownUntil > 0 && (nowMs == null || nowMs < next.checkCooldownUntil);
	return next._backendCanCheck && !cooling;
}

// ---- the reducer -----------------------------------------------------------

/**
 * @param {object} state  previous state (never mutated)
 * @param {object} event  {type, ...}
 * @param {{strict?: boolean}} [opts]  strict throws on an illegal transition
 *   (dev/test); production ignores it and counts it.
 */
export function reduce(state, event, opts = {}) {
	const strict = !!opts.strict;
	switch (event.type) {
		case EVENTS.SUBMIT_REVIEW:
			return { ...state, value: STATES.STARTING_SIGNUP, busy: "starting", transportError: false };

		case EVENTS.CONTRACT_STATE:
			return applyContract(state, event.decoded || {}, opts);

		case EVENTS.CHECKOUT_OPENED: {
			if (!hasOpenableHandles(state)) return illegal(state, strict, "checkout without handles");
			return { ...state, value: STATES.CHECKOUT_OPEN, busy: null };
		}

		case EVENTS.CHECKOUT_DISMISSED: {
			if (PAID_FLOOR.has(state.value)) return state; // paid wins over a late dismiss
			return {
				...state,
				value: STATES.UNKNOWN,
				busy: null,
				checkRequired: true, // check-on-failure is mandatory
			};
		}

		case EVENTS.CHECKOUT_FAILED: {
			if (PAID_FLOOR.has(state.value)) return state;
			return {
				...state,
				value: STATES.FAILED_RETRYABLE,
				busy: null,
				message: event.message || state.message,
				checkRequired: true,
			};
		}

		case EVENTS.GATEWAY_CALLBACK: {
			if (PAID_FLOOR.has(state.value)) return state;
			return { ...state, value: STATES.CONFIRMING, busy: "confirming" };
		}

		case EVENTS.CONFIRM_SUCCEEDED:
			return { ...state, value: STATES.PAID, busy: null, checkRequired: false, transportError: false };

		case EVENTS.CONFIRM_FAILED: {
			if (PAID_FLOOR.has(state.value)) return state;
			const decoded = event.decoded || {};
			// A confirm that returned a coded decline renders that decline; a bare
			// timeout/transport failure falls back to unknown and forces a check -
			// never to paid, never to a made-up failure.
			if (decoded.code && PAYMENT_STATE_CODES.has(decoded.code)) {
				return applyContract(state, decoded, opts);
			}
			return { ...state, value: STATES.UNKNOWN, busy: null, checkRequired: true };
		}

		case EVENTS.RATE_LIMITED: {
			const nowMs = event.nowMs || 0;
			const secs = Number(event.retryAfterSeconds) || 0;
			const until = nowMs + (secs > 0 ? secs * 1000 : DEFAULT_COOLDOWN_MS);
			const next = { ...state, checkCooldownUntil: until };
			next.canCheck = recomputeCanCheck(next, nowMs);
			return next;
		}

		case EVENTS.COOLDOWN_ELAPSED: {
			const nowMs = event.nowMs || 0;
			if (state.checkCooldownUntil && nowMs < state.checkCooldownUntil) return state;
			const next = { ...state, checkCooldownUntil: 0 };
			next.canCheck = recomputeCanCheck(next, nowMs);
			return next;
		}

		case EVENTS.PROVISIONING_STARTED: {
			// Legal ONLY from paid - provisioning is the readiness gate's, and
			// `unknown -> provisioning` is the illegal transition plan 02 names.
			if (state.value !== STATES.PAID && state.value !== STATES.PROVISIONING) {
				return illegal(state, strict, "provisioning from a non-paid state");
			}
			return { ...state, value: STATES.PROVISIONING, busy: null };
		}

		case EVENTS.PROVISIONING_DELAYED: {
			if (state.value !== STATES.PROVISIONING) {
				return illegal(state, strict, "provisioning_delayed from a non-provisioning state");
			}
			return { ...state, value: STATES.PROVISIONING_DELAYED };
		}

		default:
			return state;
	}
}

function hasOpenableHandles(state) {
	const h = state.handles || {};
	return !!(
		h.razorpay_order_id ||
		h.razorpay_subscription_id ||
		h.payment_session_id ||
		h.subscription_session_id
	);
}

function illegal(state, strict, why) {
	if (strict) throw new Error(`illegal transition: ${why} (from ${state.value})`);
	return { ...state, illegalTransitions: state.illegalTransitions + 1 };
}

// ---- CONTRACT_STATE: the big one -------------------------------------------

function applyContract(state, decoded, opts) {
	const code = decoded.code || "";
	const data = (decoded.data && typeof decoded.data === "object" && decoded.data) || {};
	const context = (decoded.context && typeof decoded.context === "object" && decoded.context) || {};

	// ---- the generation fence: a stale answer from a superseded intent is
	// ignored OUTRIGHT (same object back, so a two-tab race cannot even repaint).
	// A missing generation is NOT zero - a legacy admin sends none, and the known
	// generation must survive such an answer rather than being reset by it.
	const incomingGen = data.generation;
	if (
		incomingGen != null &&
		state.generation != null &&
		Number(incomingGen) < Number(state.generation)
	) {
		return state;
	}

	// ---- the day-one guard: no signup here yet. A fresh start, never support.
	if (code === CODES.BENCH_NO_SIGNUP_CONTEXT) {
		if (PAID_FLOOR.has(state.value)) return state;
		return { ...state, value: STATES.REVIEW, notStarted: true, busy: null, code };
	}

	// ---- the money-parked refusal (bench-local, before or instead of a state).
	if (code === CODES.BENCH_AWAITING_RECONCILIATION) {
		if (PAID_FLOOR.has(state.value)) return state;
		const next = {
			...state,
			value: state.value === STATES.REVIEW || state.value === STATES.STARTING_SIGNUP
				? STATES.UNKNOWN
				: state.value,
			busy: null,
			awaitingReconciliation: true,
			canInitiate: false,
			_backendCanCheck: true,
			recovery: decoded.recovery || "check_status",
			message: decoded.message || state.message,
			code,
		};
		next.canCheck = recomputeCanCheck(next, null);
		return next;
	}

	// ---- rate limit arriving as a decoded failure (belt-and-braces; the flow
	// also raises the RATE_LIMITED event with the parsed retry_after).
	if (code === CODES.PAYMENT_CHECK_RATE_LIMITED) {
		if (PAID_FLOOR.has(state.value)) return state;
		return reduce(state, {
			type: EVENTS.RATE_LIMITED,
			retryAfterSeconds: decoded.retryAfterSeconds || 0,
			nowMs: opts.nowMs || 0,
		});
	}

	// ---- a transport / non-payment failure says NOTHING about the money.
	const isFailure = decoded.ok === false;
	const mapped = stateForCode(code, data);
	if (isFailure && !PAYMENT_STATE_CODES.has(code)) {
		// A page that already knows a payment state keeps it, and only flags that
		// the check itself failed. A page that knows nothing renders unknown.
		if (state.code && PAYMENT_STATE_CODES.has(state.code)) {
			return { ...state, transportError: true, busy: null, message: decoded.message || state.message };
		}
		if (PAID_FLOOR.has(state.value)) return state;
		return { ...state, value: STATES.UNKNOWN, transportError: true, busy: null, code };
	}

	// ---- a real payment state.
	if (PAID_FLOOR.has(state.value)) {
		// Paid is a floor: absorb the reconciliation flag and capability tweaks if
		// any, but never move the state back.
		return state;
	}

	const handles = pickHandles(data);
	const supersededGen =
		incomingGen != null && state.generation != null && Number(incomingGen) > Number(state.generation);

	const next = {
		...state,
		code: code || state.code,
		message: decoded.message || "",
		recovery: decoded.recovery || data.recovery || "",
		busy: null,
		transportError: false,
		attemptId: data.attempt_id || state.attemptId,
		generation: incomingGen != null ? Number(incomingGen) : state.generation,
		provider: (data.payment_provider || state.provider || null) && (data.payment_provider || state.provider),
		// A newer generation REPLACES the previous intent's handles wholesale (the
		// old order is dead); same/again keeps and merges.
		handles: supersededGen ? handles : { ...state.handles, ...handles },
		lastCheckedAt: data.payment_last_checked_at || state.lastCheckedAt,
		verificationExpiresAt: data.verification_expires_at || state.verificationExpiresAt,
		awaitingReconciliation: !!data.awaiting_manual_reconciliation,
		canReconnect: !!data.can_reconnect || state.canReconnect,
		notStarted: false,
	};

	if (mapped) next.value = mapped;

	// A new intent (the generation advanced) is a fresh start for the client-
	// local support counter: the customer has a live checkout in front of them
	// again, so the "you have checked many times" offer from the previous intent
	// is put away. Keyed the same way supportHandoff keys it - (attempt,
	// generation) - so this and recordCheck cannot disagree.
	if (supersededGen) {
		next.supportChecks = emptyCounter();
		next.supportOffered = false;
	}

	next.summary = buildSummary(state.summary, data, context);
	next.isMandate = isMandateShape(next.handles);

	// Capability flags: honour the backend, then let the reconciliation flag veto
	// initiate. A code that is definitionally paid/terminal never offers initiate
	// regardless of what a flag says.
	const backendCanInitiate = "can_initiate_payment" in data ? !!data.can_initiate_payment : next.canInitiate;
	next._backendCanCheck = "can_check_status" in data ? !!data.can_check_status : (state._backendCanCheck || false);
	next.canInitiate =
		backendCanInitiate && !next.awaitingReconciliation && !isTerminalForPayment(next.value);
	next.canCheck = recomputeCanCheck(next, opts.nowMs);

	return next;
}

/** Seconds left on the check cooldown, rounded UP, never negative. */
export function remainingCooldownSeconds(state, nowMs) {
	const left = (state.checkCooldownUntil || 0) - nowMs;
	if (left <= 0) return 0;
	return Math.ceil(left / 1000);
}

/**
 * Record a status check against the current intent and surface the support
 * moment once the client-local ceiling is reached. Kept here so the flow has one
 * call for "the customer checked again".
 */
export function noteStatusCheck(state) {
	const counter = recordCheck(state.supportChecks, {
		attemptId: state.attemptId,
		generation: state.generation,
	});
	return { ...state, supportChecks: counter, supportOffered: shouldOfferSupport(counter) };
}
