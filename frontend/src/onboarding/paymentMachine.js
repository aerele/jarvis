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
	// The gateway open did not settle inside the (generous) open deadline. Unlike
	// CHECKOUT_FAILED this is NOT "the sheet could not open" - an in-page modal
	// (Razorpay netbanking / UPI-collect) can sit open and PAYABLE long past the
	// deadline, so we must not re-arm "start a new payment" over a live sheet
	// (B2-1). It leaves the busy screen for a checkable UNKNOWN and VETOES initiate
	// (checkoutMayBeOpen) until the sheet actually closes and a check has run.
	CHECKOUT_OPEN_TIMED_OUT: "CHECKOUT_OPEN_TIMED_OUT",
	// A previously timed-out sheet finally closed WITHOUT a success (a late
	// dismiss, or a programmatic teardown). Clears the checkoutMayBeOpen veto and
	// requires a check; never a verdict of its own.
	CHECKOUT_SHEET_CLOSED: "CHECKOUT_SHEET_CLOSED",
	// The customer came back to a checkout the page had opened - a bfcache
	// restore, a top-level redirect return, or a tab regaining focus after a
	// full-page mandate. NOT a dismissal: it asserts nothing about the money, only
	// that the sheet is no longer in front of us, so it leaves the busy screen for
	// a checkable UNKNOWN and the orchestrator reconciles server truth (P0-2).
	RETURNED_FROM_CHECKOUT: "RETURNED_FROM_CHECKOUT",
	GATEWAY_CALLBACK: "GATEWAY_CALLBACK",
	CONFIRM_SUCCEEDED: "CONFIRM_SUCCEEDED",
	CONFIRM_FAILED: "CONFIRM_FAILED",
	RATE_LIMITED: "RATE_LIMITED",
	COOLDOWN_ELAPSED: "COOLDOWN_ELAPSED",
	// "Start again" - a server-truth-gated reset. The reducer resets to a fresh
	// review ONLY for codes where no recoverable payment can exist; otherwise it
	// preserves the attempt and its status/reconnect/support affordances (P1-3).
	RESTART: "RESTART",
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

// The ONLY states a gateway sheet may be opened from (P1-1). A live, unsettled
// intent that still has a real handle - never a settled recovery state. Opening
// from confirm_required (the authorization already exists), reconnect, terminal,
// verification (nothing to pay yet), or the paid floor is exactly the "blind Pay"
// the plan forbids: those states can still hold a retained handle from an earlier
// same-generation answer, and the old predicate would have opened on it.
const CHECKOUT_OPENABLE_STATES = new Set([STATES.UNKNOWN, STATES.FAILED_RETRYABLE]);

// The states a stray gateway event (dismiss/fail/callback) may legally act on: a
// live, unsettled intent or a sheet that is actually open/confirming. It must
// NEVER move a settled recovery state (terminal/reconnect/authorized-pending), a
// verification wait, or the paid floor - a late event from a superseded sheet
// would otherwise drag one of those backwards (P1-5). Dismiss is tighter still
// (a sheet has to have been open to be dismissed), and CONFIRM_SUCCEEDED - the
// one event that mints PAID - is tightest of all (only from confirming).
const GATEWAY_EVENT_SOURCES = new Set([
	STATES.CHECKOUT_OPEN,
	STATES.UNKNOWN,
	STATES.FAILED_RETRYABLE,
]);
const DISMISS_SOURCES = new Set([STATES.CHECKOUT_OPEN]);
const RETURN_SOURCES = new Set([STATES.CHECKOUT_OPEN, STATES.CONFIRMING]);

// Codes for which "Start again" may safely wipe the machine: no recoverable
// payment can be sitting behind them, so a fresh review is honest. Any other code
// - and any parked-money flag - preserves the attempt instead (P1-3).
const RESTART_SAFE_CODES = new Set([
	CODES.BENCH_NO_SIGNUP_CONTEXT,
	CODES.NO_CURRENT_INTENT,
	CODES.ACCOUNT_ALREADY_EXISTS,
]);

export const HANDLE_KEYS = [
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

// Which gateway each of those handles belongs to, partitioned once. Derived
// from what the openers actually read: useRazorpay (razorpay_key_id, and either
// razorpay_subscription_id or razorpay_order_id + amount_inr), useCashfree
// (cashfree_env, payment_session_id, cashfree_order_id), and the wizard's own
// autopay-mandate arm in OnboardingView (cashfree_env, subscription_session_id).
//
// It exists because handles ACCUMULATE: a same-generation answer merges rather
// than replaces (see applyContract), and a same-generation answer is a designed
// event - the bench reuses its idempotency key so retries converge on one
// gateway object. classifyOnboardingHandles then sniffs the mandate SHAPE before
// it ever consults payment_provider, so an accumulated set is classified by
// whichever shape it matches first: a stale Cashfree session sitting beside a
// live Razorpay order classifies as a mandate, and the customer is sent to a
// full-page Cashfree redirect for a Razorpay order. Narrowing to the named
// gateway's own keys is what keeps the shape honest.
const RAZORPAY_HANDLE_KEYS = new Set([
	"razorpay_order_id",
	"razorpay_subscription_id",
	"razorpay_key_id",
]);
const CASHFREE_HANDLE_KEYS = new Set([
	// Cashfree's, despite the gateway-neutral name - it is what the Cashfree SDK
	// is handed as `paymentSessionId`, and what classifyHandles reads as a
	// Cashfree order.
	"payment_session_id",
	"cashfree_order_id",
	"cashfree_subscription_id",
	"subscription_session_id",
	"cashfree_app_id",
	"cashfree_env",
]);
// Not a gateway handle at all: the price the sheet displays. It belongs to
// whichever sheet opens.
const NEUTRAL_HANDLE_KEYS = new Set(["amount_inr"]);
const PROVIDER_HANDLE_KEYS = {
	razorpay: RAZORPAY_HANDLE_KEYS,
	cashfree: CASHFREE_HANDLE_KEYS,
};
// Every family, for the case where the NAMED one turns out to hold nothing that
// can open. Only one of them can hold the openable handles at that point, so
// there is a right answer here and no guess involved.
const FAMILIES = [RAZORPAY_HANDLE_KEYS, CASHFREE_HANDLE_KEYS];

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
		// Presentation-only detail from the thing that failed to OPEN a sheet (an
		// ad-blocker eating the SDK, a gateway that would not launch). It is never a
		// payment verdict, so it rides alongside whatever authoritative code the
		// mandatory reconcile then discovers - it must not preserve a stale code,
		// state, or capability over that answer (P0-1). Cleared on a fresh
		// open/attempt.
		checkoutNote: "",
		// Set when an open times out while the sheet MAY still be open and payable
		// (B2-1). While true, initiate is vetoed at the machine (not just in copy):
		// the page cannot offer "start a new payment" over a live gateway sheet. It
		// lifts only when the sheet actually closes (CHECKOUT_SHEET_CLOSED /
		// GATEWAY_CALLBACK / a fresh open) and a status check has run after closure.
		checkoutMayBeOpen: false,
		checkCooldownUntil: 0,
		supportChecks: emptyCounter(),
		supportOffered: false,
		illegalTransitions: 0,
		// admin's own sentence for a paid-but-not-provisioned workspace.
		provisioningNote: "",
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
	// The plan NAME (not its label): what a retry must initiate on. Rendering uses
	// planLabel; initiate uses this, and getting them confused is a wrong charge.
	const planName =
		(data.plan && typeof data.plan === "object" && data.plan.name) ||
		(typeof data.plan === "string" ? data.plan : "") ||
		context.plan ||
		(prev && prev.plan) ||
		"";
	const num = (v, fallback) =>
		v === null || v === undefined || v === "" ? fallback : Number(v);
	return {
		plan: planName,
		email: data.email || context.email || (prev && prev.email) || "",
		company: data.company || context.company || (prev && prev.company) || "",
		planLabel,
		amountInr: num(data.amount_inr, prev ? prev.amountInr : null),
		dueTodayInr: num(data.due_today_inr, prev ? prev.dueTodayInr : null),
		signupFeeInr: num(data.signup_fee_inr, prev ? prev.signupFeeInr : null),
		trialDays:
			num(data.effective_trial_days ?? data.trial_days, prev ? prev.trialDays : 0) || 0,
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
	const cooling =
		next.checkCooldownUntil > 0 && (nowMs == null || nowMs < next.checkCooldownUntil);
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
	// ONE clock resolution for every case that reads time. Callers pass it two
	// ways - the tests put it on the event, usePaymentFlow.apply() puts it in
	// opts - and reading only one of them is what left the rate-limit cooldown
	// permanently stuck: tickCooldown() sent {type} with the clock in opts, the
	// guard saw `event.nowMs || 0` = 0, returned the state unchanged forever, and
	// the Check button stayed dead while its label (read from the view's own
	// clock) recovered. That inverted this module's status-first rule - the safe
	// action was disabled and the charging action was the only one left alive.
	const nowMs = event.nowMs != null ? event.nowMs : opts.nowMs != null ? opts.nowMs : 0;
	switch (event.type) {
		case EVENTS.SUBMIT_REVIEW:
			return {
				...state,
				value: STATES.STARTING_SIGNUP,
				busy: "starting",
				transportError: false,
			};

		case EVENTS.CONTRACT_STATE:
			return applyContract(state, event.decoded || {}, opts);

		case EVENTS.CHECKOUT_OPENED: {
			// Paid is a floor here too. Every other late gateway event is already a
			// no-op above it; an open is the one that would have taken a settled
			// signup back to a live sheet (a second tab, a click that landed after
			// the confirm), which is the same class of harm the floor exists for.
			if (PAID_FLOOR.has(state.value)) return state;
			if (!canOpenCheckout(state)) return illegal(state, strict, "checkout without handles");
			// A fresh sheet clears any earlier "the checkout could not open" note and
			// the "a previous sheet may still be open" veto - this IS the new sheet.
			return {
				...state,
				value: STATES.CHECKOUT_OPEN,
				busy: null,
				checkoutNote: "",
				checkoutMayBeOpen: false,
			};
		}

		case EVENTS.CHECKOUT_OPEN_TIMED_OUT: {
			if (PAID_FLOOR.has(state.value)) return state;
			if (callbackStale(state, event)) return state;
			if (state.value !== STATES.CHECKOUT_OPEN) {
				return illegal(state, strict, "open-timeout outside an open checkout");
			}
			// The sheet did not settle in time and MAY still be open & payable. Leave
			// the busy screen for a checkable UNKNOWN, but VETO initiate so the page
			// cannot re-arm "start a new payment" over a live sheet (B2-1). The veto
			// lifts on CHECKOUT_SHEET_CLOSED / GATEWAY_CALLBACK, never on a check.
			return {
				...state,
				value: STATES.UNKNOWN,
				busy: null,
				checkRequired: true,
				checkoutMayBeOpen: true,
			};
		}

		case EVENTS.CHECKOUT_SHEET_CLOSED: {
			// A timed-out sheet finally closed with no success (late dismiss / SDK
			// teardown). It asserts nothing about the money - only that the sheet is
			// gone - so it clears the veto and requires a check. Legal from any
			// non-floor state; a stray one on the paid floor is a no-op.
			if (PAID_FLOOR.has(state.value)) return state;
			if (callbackStale(state, event)) return state;
			return { ...state, busy: null, checkRequired: true, checkoutMayBeOpen: false };
		}

		case EVENTS.CHECKOUT_DISMISSED: {
			if (PAID_FLOOR.has(state.value)) return state; // paid wins over a late dismiss
			if (callbackStale(state, event)) return state; // a superseded sheet's dismiss
			if (!DISMISS_SOURCES.has(state.value)) {
				return illegal(state, strict, "dismiss from a state with no open sheet");
			}
			return {
				...state,
				value: STATES.UNKNOWN,
				busy: null,
				checkRequired: true, // check-on-failure is mandatory
			};
		}

		case EVENTS.CHECKOUT_FAILED: {
			if (PAID_FLOOR.has(state.value)) return state;
			if (callbackStale(state, event)) return state;
			if (!GATEWAY_EVENT_SOURCES.has(state.value)) {
				return illegal(state, strict, "checkout-failed from a settled state");
			}
			return {
				...state,
				value: STATES.FAILED_RETRYABLE,
				busy: null,
				// The SDK/gateway reason is PRESENTATION metadata, not a verdict: it
				// rides in its own field so the mandatory reconcile that follows can
				// overwrite message/code/state without losing it (P0-1). It is
				// SANITIZED first (X2): a deadline abort's internal label ("payment
				// request timed out: <label>") is a client signal, never a customer
				// message, so it is dropped here - the one chokepoint that feeds the
				// note render path - while genuine SDK/customer-authored strings pass.
				checkoutNote: sanitizeCheckoutNote(event.message) || state.checkoutNote || "",
				checkRequired: true,
			};
		}

		case EVENTS.GATEWAY_CALLBACK: {
			if (PAID_FLOOR.has(state.value)) return state;
			if (callbackStale(state, event)) return state;
			if (!GATEWAY_EVENT_SOURCES.has(state.value)) {
				return illegal(state, strict, "gateway callback from a settled state");
			}
			// A callback arriving means the sheet closed - clear any "may still be
			// open" veto (X1: a late post-deadline payment lands here).
			return {
				...state,
				value: STATES.CONFIRMING,
				busy: "confirming",
				checkoutMayBeOpen: false,
			};
		}

		case EVENTS.RETURNED_FROM_CHECKOUT: {
			// The sheet is gone (bfcache restore, redirect return, tab refocus). Do
			// NOT assume dismissal or paid: leave the actionless busy screen for a
			// checkable UNKNOWN and let the orchestrator reconcile server truth. Only
			// legal while a sheet was actually open/confirming; a stray pageshow in
			// any other state is a no-op (never a regression).
			if (PAID_FLOOR.has(state.value)) return state;
			if (!RETURN_SOURCES.has(state.value)) return state;
			return {
				...state,
				value: STATES.UNKNOWN,
				busy: null,
				checkRequired: true,
				checkoutMayBeOpen: false,
			};
		}

		case EVENTS.CONFIRM_SUCCEEDED: {
			// The one event that mints PAID, so it is the tightest-guarded: only a
			// real confirm-in-progress can complete, and only for the live attempt.
			if (state.value !== STATES.CONFIRMING) {
				if (PAID_FLOOR.has(state.value)) return state;
				return illegal(state, strict, "confirm-succeeded outside a confirm");
			}
			if (callbackStale(state, event)) return state;
			// Keep what the confirm actually said. admin's allocation-failure branch
			// answers ok:True with a real customer sentence in
			// `chat_readiness_reason` and NO container - discarding it left the
			// customer watching a 90-second spinner instead of reading the one line
			// that explained it.
			const d = event.data || {};
			return {
				...state,
				value: STATES.PAID,
				busy: null,
				checkRequired: false,
				transportError: false,
				checkoutMayBeOpen: false,
				provisioningNote: d.chat_readiness_reason || state.provisioningNote || "",
				handles: state.handles,
			};
		}

		case EVENTS.CONFIRM_FAILED: {
			if (PAID_FLOOR.has(state.value)) return state;
			if (callbackStale(state, event)) return state;
			if (state.value !== STATES.CONFIRMING) {
				return illegal(state, strict, "confirm-failed outside a confirm");
			}
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
			const secs = Number(event.retryAfterSeconds) || 0;
			const until = nowMs + (secs > 0 ? secs * 1000 : DEFAULT_COOLDOWN_MS);
			// A 429 can be the FIRST thing a cold page hears, and its envelope
			// carries no capability flags (a failure body has no `data`). Default
			// the read-only capability to true so the page is not left with the
			// charging action as its only live control once the window passes.
			const next = {
				...state,
				checkCooldownUntil: until,
				_backendCanCheck: state._backendCanCheck == null ? true : state._backendCanCheck,
			};
			next.canCheck = recomputeCanCheck(next, nowMs);
			return next;
		}

		case EVENTS.COOLDOWN_ELAPSED: {
			if (state.checkCooldownUntil && nowMs < state.checkCooldownUntil) return state;
			const next = { ...state, checkCooldownUntil: 0 };
			next.canCheck = recomputeCanCheck(next, nowMs);
			return next;
		}

		case EVENTS.RESTART: {
			// "Start again" is only a real reset when no recoverable payment can be
			// behind the current code (P1-3). Money in flight - the paid floor, a
			// parked-reconciliation flag, an authorization pending confirm, a live
			// pending intent - preserves the attempt and its recovery affordances
			// instead, so a blind local reset can never orphan a payment.
			if (!canSafelyRestart(state)) return state;
			return { ...initialState(), value: STATES.REVIEW };
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
				return illegal(
					state,
					strict,
					"provisioning_delayed from a non-provisioning state"
				);
			}
			return { ...state, value: STATES.PROVISIONING_DELAYED };
		}

		default:
			return state;
	}
}

/**
 * Can a gateway sheet be opened from THIS state?
 *
 * The single predicate both the reducer's CHECKOUT_OPENED guard and the
 * orchestrator's decide-to-open read, so the SPA can never open a sheet the
 * machine would refuse. The orchestrator used to answer the same question from
 * the ANSWER it had just received, which is a different question: three of
 * applyContract's early returns (the generation fence, an unattributable answer,
 * the paid floor) deliberately keep an answer's handles OUT of the state, and a
 * handle the reducer refused is a dead order to send a customer to.
 */
export function canOpenCheckout(state) {
	const s = state || {};
	// Fail closed on the SOURCE STATE first (P1-1). Only a live, unsettled intent
	// may raise a sheet: confirm_required (the authorization already exists),
	// reconnect, terminal, verification, the paid floor and provisioning all keep
	// their retained handles OUT of reach here, so a stale handle can never open a
	// blind Pay from a state whose whole point is that paying again is unsafe.
	if (!CHECKOUT_OPENABLE_STATES.has(s.value)) return false;
	return hasOpenableHandle(s.handles);
}

// The four handles that are, on their own, enough to raise a sheet.
function hasOpenableHandle(handles) {
	const h = handles || {};
	return !!(
		h.razorpay_order_id ||
		h.razorpay_subscription_id ||
		h.payment_session_id ||
		h.subscription_session_id
	);
}

/**
 * The handles a sheet may be built from for ONE named gateway.
 *
 * Handles accumulate across same-generation answers, and the checkout
 * dispatcher classifies by SHAPE before it looks at any provider field - so the
 * set handed to an SDK must first be narrowed to the gateway the machine names,
 * plus the provider-neutral keys (the price). Nothing else changes: the reducer
 * still keeps everything it was told, and this is applied where the sheet is
 * built.
 *
 * The provider LABEL is sticky, so it can name a gateway whose keys in the set
 * are all stale. That is not the accumulation this exists for: once the named
 * family holds nothing openable, only the OTHER family can, so the sheet is
 * built from that one. Handing over the whole set instead left the named
 * family's leftovers in it - and a leftover that opens nothing can still decide
 * the classification, which is exactly how a dead Cashfree subscription id
 * captured a live Razorpay order.
 *
 * The one deliberate non-narrowing, and it is "we have no basis, so do not
 * guess": an absent or unrecognised provider has no key family, so the full set
 * is returned unchanged - an unfamiliar discriminator is not a licence to drop
 * handles the answer actually carried. (Nothing openable anywhere is the same
 * answer for the same reason; the caller has already refused to open it.)
 */
export function handlesForProvider(handles, provider) {
	const full = { ...(handles || {}) };
	const named =
		PROVIDER_HANDLE_KEYS[
			String(provider || "")
				.trim()
				.toLowerCase()
		];
	if (!named) return full;
	const family = hasOpenableHandle(narrowToFamily(full, named))
		? named
		: FAMILIES.find((f) => hasOpenableHandle(narrowToFamily(full, f)));
	return family ? narrowToFamily(full, family) : full;
}

function narrowToFamily(handles, family) {
	const out = {};
	for (const k of Object.keys(handles)) {
		if (family.has(k) || NEUTRAL_HANDLE_KEYS.has(k)) out[k] = handles[k];
	}
	return out;
}

// The internal signature a deadline abort stamps on its Error message
// (usePaymentFlow.deadlined: "payment request timed out: <label>"). It is a
// CLIENT signal - it says nothing about the gateway - so it must never reach the
// customer-facing note. Everything else (genuine SDK / customer-authored strings
// like "An ad blocker stopped the checkout.") passes through unchanged.
const INTERNAL_TIMEOUT_NOTE_PREFIX = /^\s*payment request timed out/i;

/**
 * Sanitize a would-be checkout note before it is stored (X2). The stored note is
 * the single source the render path (OnboardingView.payDetail) reads, so filtering
 * an untagged internal string here guarantees it can never be shown. A prefix
 * filter (not an allowlist) is used deliberately: SDK/customer strings are open-
 * ended and legitimate, while the one internal leak has a fixed, known prefix.
 */
export function sanitizeCheckoutNote(msg) {
	const s = (msg == null ? "" : String(msg)).trim();
	if (!s) return "";
	if (INTERNAL_TIMEOUT_NOTE_PREFIX.test(s)) return "";
	return s;
}

function illegal(state, strict, why) {
	if (strict) throw new Error(`illegal transition: ${why} (from ${state.value})`);
	return { ...state, illegalTransitions: state.illegalTransitions + 1 };
}

/**
 * A gateway event (dismiss/fail/callback/confirm) carrying an attempt or
 * generation identity that no longer matches the live intent is from a
 * SUPERSEDED sheet - a second tab, a late callback from an old order. The
 * orchestrator's token fences most of these within one component instance; this
 * is the reducer-level backstop the plan asked for (P1-5): a stale-identity
 * callback is a no-op here (the orchestrator reconciles server truth), never a
 * mutation by event name alone. Absent identity (the existing internal events
 * carry none) is not stale - only a present, mismatched one is.
 */
function callbackStale(state, event) {
	if (
		event.attemptId != null &&
		state.attemptId != null &&
		event.attemptId !== state.attemptId
	) {
		return true;
	}
	if (
		event.generation != null &&
		state.generation != null &&
		Number(event.generation) < Number(state.generation)
	) {
		return true;
	}
	return false;
}

/**
 * May "Start again" wipe the machine? Only when no recoverable payment can be
 * behind the current code (P1-3). The paid floor and a parked-reconciliation
 * flag are always preserved; otherwise the code must be one that definitionally
 * carries no money on this signup, or a page that never left review.
 */
export function canSafelyRestart(state) {
	const s = state || {};
	if (PAID_FLOOR.has(s.value)) return false;
	if (s.awaitingReconciliation) return false;
	if (s.value === STATES.REVIEW) return true;
	return RESTART_SAFE_CODES.has(s.code);
}

// ---- CONTRACT_STATE: the big one -------------------------------------------

function applyContract(state, decoded, opts) {
	const code = decoded.code || "";
	const data = (decoded.data && typeof decoded.data === "object" && decoded.data) || {};
	const context =
		(decoded.context && typeof decoded.context === "object" && decoded.context) || {};

	// ---- the ATTEMPT fence, ahead of the generation compare (P1-4). A different
	// attempt id is a different intent, so it is judged on its own terms: its
	// handles never merge into the previous attempt's, and it is NEVER rejected by
	// the generation counter (a legitimate replacement attempt may even carry a
	// lower generation - a fresh intent starting its own count). The generation
	// fence below therefore governs only answers of the SAME attempt (or an
	// unattributable one), which is the only place a "stale poll" comparison means
	// anything.
	const incomingGen = data.generation;
	const incomingAttempt = data.attempt_id;
	const attemptChanged =
		incomingAttempt != null && state.attemptId != null && incomingAttempt !== state.attemptId;

	// ---- the generation fence: a stale answer from a superseded intent of the
	// SAME attempt is ignored OUTRIGHT (same object back, so a two-tab race cannot
	// even repaint). A missing generation is NOT zero - a legacy admin sends none,
	// and the known generation must survive such an answer rather than being reset
	// by it.
	if (
		!attemptChanged &&
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
			value:
				state.value === STATES.REVIEW || state.value === STATES.STARTING_SIGNUP
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
		const next = reduce(state, {
			type: EVENTS.RATE_LIMITED,
			retryAfterSeconds: decoded.retryAfterSeconds || 0,
			nowMs: opts.nowMs || 0,
		});
		// A 429 over a KNOWN payment state must not overwrite it - the money is
		// still wherever it was, and "you asked too often" is not a payment
		// verdict. But on a COLD page this is the only thing we have been told,
		// and leaving code empty rendered the alarming catch-all ("We could not
		// determine the payment status") instead of this code's own truthful
		// row, whose copy was otherwise unreachable.
		if (PAYMENT_STATE_CODES.has(state.code)) return next;
		return { ...next, code };
	}

	// ---- a transport / non-payment failure says NOTHING about the money.
	const isFailure = decoded.ok === false;
	const mapped = stateForCode(code, data);
	if (isFailure && !PAYMENT_STATE_CODES.has(code)) {
		// A page that already knows a payment state keeps it, and only flags that
		// the check itself failed. A page that knows nothing renders unknown.
		//
		// Either way the read-only capability defaults TRUE when nothing is known.
		// A failure envelope (onboarding_contract.failure) carries no `data` at
		// all, so no capability flag can ever arrive on one - and without this a
		// cold mount that first hears a transport failure rendered a screen whose
		// only offered action was disabled. Checking is a read: it creates no
		// intent and charges nothing, so offering it is always safe. `initiate` is
		// deliberately NOT defaulted - that one can take money.
		const withCheck = (next) => {
			const out = {
				...next,
				_backendCanCheck: next._backendCanCheck == null ? true : next._backendCanCheck,
			};
			out.canCheck = recomputeCanCheck(out, opts.nowMs);
			return out;
		};
		if (state.code && PAYMENT_STATE_CODES.has(state.code)) {
			return withCheck({
				...state,
				transportError: true,
				busy: null,
				message: decoded.message || state.message,
			});
		}
		if (PAID_FLOOR.has(state.value)) return state;
		return withCheck({
			...state,
			value: STATES.UNKNOWN,
			transportError: true,
			busy: null,
			code,
		});
	}

	// ---- a real payment state.
	if (PAID_FLOOR.has(state.value)) {
		// Paid is a floor: absorb the reconciliation flag and capability tweaks if
		// any, but never move the state back.
		return state;
	}

	const handles = pickHandles(data);
	const supersededGen =
		!attemptChanged &&
		incomingGen != null &&
		state.generation != null &&
		Number(incomingGen) > Number(state.generation);
	// An answer carrying NO generation cannot be attributed to the current
	// intent, and the generation fence above cannot judge it. Its CODE is still
	// honoured - an older control plane is entitled to report a decline - but its
	// HANDLES are not merged over a live intent's, which is how a dead order id
	// came back to sit beside a live one after a gen-less failure landed. A
	// changed attempt is not "unattributable" - it is attributable to a DIFFERENT
	// intent, which is handled by the replace path below.
	const unattributable = !attemptChanged && incomingGen == null && state.generation != null;
	// A new attempt (P1-4) or an advanced generation is a fresh intent: it
	// REPLACES the previous handles wholesale rather than merging over them, and
	// resets the client-local support counter with its live checkout.
	const replaceHandles = attemptChanged || supersededGen;

	const next = {
		...state,
		code: code || state.code,
		message: decoded.message || "",
		recovery: decoded.recovery || data.recovery || "",
		busy: null,
		transportError: false,
		// A fresh intent (new attempt or advanced generation) also drops the stale
		// "the checkout could not open" note AND the "a previous sheet may still be
		// open" veto from the intent it replaces. A same-intent answer (a check while
		// a timed-out sheet is still open) keeps the veto - only the sheet actually
		// closing lifts it.
		checkoutNote: replaceHandles ? "" : state.checkoutNote,
		checkoutMayBeOpen: replaceHandles ? false : state.checkoutMayBeOpen,
		attemptId: incomingAttempt || state.attemptId,
		// A new attempt with no generation of its own resets the counter to unknown
		// rather than inheriting the replaced intent's.
		generation:
			incomingGen != null ? Number(incomingGen) : attemptChanged ? null : state.generation,
		provider:
			(data.payment_provider || state.provider || null) &&
			(data.payment_provider || state.provider),
		// A newer generation or a new attempt REPLACES the previous intent's
		// handles wholesale (the old order is dead); same attempt/generation keeps
		// and merges; an unattributable answer contributes none.
		handles: replaceHandles
			? handles
			: unattributable
			? state.handles
			: { ...state.handles, ...handles },
		lastCheckedAt: data.payment_last_checked_at || state.lastCheckedAt,
		verificationExpiresAt: data.verification_expires_at || state.verificationExpiresAt,
		awaitingReconciliation: !!data.awaiting_manual_reconciliation,
		canReconnect: !!data.can_reconnect || state.canReconnect,
		notStarted: false,
	};

	if (mapped) next.value = mapped;

	// A fresh intent (a new attempt, or the generation advanced) is a fresh start
	// for the client-local support counter: the customer has a live checkout in
	// front of them again, so the "you have checked many times" offer from the
	// previous intent is put away. Keyed the same way supportHandoff keys it -
	// (attempt, generation) - so this and recordCheck cannot disagree.
	if (replaceHandles) {
		next.supportChecks = emptyCounter();
		next.supportOffered = false;
	}

	next.summary = buildSummary(state.summary, data, context);
	next.isMandate = isMandateShape(next.handles);

	// Capability flags: honour the backend, then let the reconciliation flag veto
	// initiate. A code that is definitionally paid/terminal never offers initiate
	// regardless of what a flag says.
	const backendCanInitiate =
		"can_initiate_payment" in data ? !!data.can_initiate_payment : next.canInitiate;
	// Same read-only default as the transport branch, and for the same reason: a
	// coded FAILURE (already-paid, terminal, parked money, an invalid key) rides
	// onboarding_contract.failure, which carries no `data` and therefore no
	// capability flag - so a cold page that first hears one of those had no way
	// to learn that checking was allowed, and rendered its buttons all disabled.
	// Checking creates no intent and charges nothing; `initiate` is never
	// defaulted this way, because that one can take money.
	next._backendCanCheck =
		"can_check_status" in data
			? !!data.can_check_status
			: state._backendCanCheck == null
			? true
			: state._backendCanCheck;
	next.canInitiate =
		backendCanInitiate &&
		!next.awaitingReconciliation &&
		!isTerminalForPayment(next.value) &&
		// The B2-1 veto: never offer a new payment while a prior sheet may still be
		// open and payable. It survives the mandatory reconcile (a check does not
		// clear it), so the page cannot re-arm initiate under a live gateway sheet.
		!next.checkoutMayBeOpen;
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
