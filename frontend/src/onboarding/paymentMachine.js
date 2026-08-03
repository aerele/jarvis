/**
 * The pay step's transition table, as a pure reducer.
 *
 * No Vue, no network, no timers. The orchestrator (usePaymentFlow) owns every
 * side effect - API calls, the top-level navigation to the admin-hosted pay
 * page, the poll loops - so this file can hold the ONE invariant the whole plan
 * exists for, and be exhaustively tested for it without a browser: **nothing but
 * an authoritative paid answer leaves the pay step.** A failed, slow, timed-out,
 * unknown-status payment stays put, with the named recovery actions the backend
 * says are safe.
 *
 * plan-09 WS7 (the admin-hosted checkout cutover). The bench no longer opens any
 * gateway SDK on its own origin. A payable intent comes back as a pay-page TOKEN
 * plus the bench's OWN attested origin (onboarding_contract.augment_pay_page), and
 * the customer is TOP-LEVEL NAVIGATED to `{origin}/jarvis-checkout#t=<token>`.
 * `CHECKOUT_OPEN` therefore means "we navigated away to the pay page", not "a sheet
 * is open in this tab"; on return the machine takes the explicit, safe
 * RETURNED_FROM_CHECKOUT exit and the orchestrator hydrates server truth. There is
 * NO FALLBACK (owner decision 4): a token with no attested origin, or a pre-cutover
 * admin's raw handles with no token, fails the step CLOSED with honest copy - never
 * a sheet, never a guess.
 *
 * Two rules that show up all over the reducer and are worth stating once:
 *
 *   - **Code, never status.** Every payment verdict comes from `decoded.code`
 *     (admin's contract vocabulary, or a BENCH_* code the facade added). The
 *     HTTP status is a transport signal the codec already consumed; it never
 *     reaches a branch here.
 *   - **Paid is a floor.** Once `paid`/`provisioning` is reached, no later
 *     answer - a stale pending poll, a late decline - moves it back.
 *
 * The provisioning states are in this table so the page can RENDER them, but
 * they are not driven by the payment endpoints: the D1/WS-D readiness gate owns
 * the transition into and out of them (see provisioningOwner). This reducer only
 * accepts an explicit PROVISIONING_STARTED/_DELAYED from that surface and refuses
 * to derive it from a payment answer.
 */

import { CODES } from "./paymentCodes.js";
import { emptyCounter, recordCheck, shouldOfferSupport } from "./supportHandoff.js";

export const STATES = {
	REVIEW: "review",
	STARTING_SIGNUP: "starting_signup",
	VERIFICATION_REQUIRED: "verification_required",
	// "We top-level-navigated to the admin-hosted pay page." The tab is leaving
	// (or has left) for `{origin}/jarvis-checkout`; on return the explicit
	// RETURNED_FROM_CHECKOUT exit + a fresh hydrate reconcile server truth.
	CHECKOUT_OPEN: "checkout_open",
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
	// The orchestrator has begun the top-level navigation to the pay page. Legal
	// ONLY from a navigable source state carrying a live, attested token
	// (canNavigateToPay). Moves to CHECKOUT_OPEN so a second click cannot navigate
	// again (the source-state gate is the double-navigate guard).
	NAVIGATED_TO_PAY: "NAVIGATED_TO_PAY",
	// The customer came back from the pay page - a bfcache restore, a top-level
	// history.back(), or a tab regaining focus. NOT a verdict about the money: it
	// leaves the actionless busy screen for a checkable UNKNOWN and the orchestrator
	// reconciles server truth (P0-2).
	RETURNED_FROM_CHECKOUT: "RETURNED_FROM_CHECKOUT",
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
// above it. Late polls and late declines cannot move it.
const PAID_FLOOR = new Set([STATES.PAID, STATES.PROVISIONING, STATES.PROVISIONING_DELAYED]);

// The ONLY states the customer may be navigated to the pay page from (P1-1, now
// the navigate-to-pay eligibility rule). A live, unsettled intent that still
// carries a token - never a settled recovery state. Navigating from
// confirm_required (the authorization already exists), reconnect, terminal,
// verification (verify first) or the paid floor is exactly the "blind pay" the
// plan forbids.
const NAVIGABLE_SOURCE_STATES = new Set([STATES.UNKNOWN, STATES.FAILED_RETRYABLE]);

// The one state a RETURNED_FROM_CHECKOUT may act on: we actually navigated away.
// A stray pageshow/visibility in any other state is a no-op (never a regression).
const RETURN_SOURCES = new Set([STATES.CHECKOUT_OPEN]);

// Codes for which "Start again" may safely wipe the machine: no recoverable
// payment can be sitting behind them, so a fresh review is honest. Any other code
// - and any parked-money flag - preserves the attempt instead (P1-3).
const RESTART_SAFE_CODES = new Set([
	CODES.BENCH_NO_SIGNUP_CONTEXT,
	CODES.NO_CURRENT_INTENT,
	CODES.ACCOUNT_ALREADY_EXISTS,
]);

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
		busy: null, // "starting" | "initiating" | "checking" | "verifying" | null
		attemptId: null,
		generation: null,
		provider: null,
		// plan-09 WS7: the navigate-to-pay capability. A live pay-page token, the
		// bench's OWN configured origin, and admin's attestation that the two agree
		// (onboarding_contract.augment_pay_page). All three are required to navigate;
		// any missing piece fails the step closed (no fallback). Refreshed from
		// server truth on every answer, so a stale token can never linger.
		payPageToken: "",
		payOrigin: "",
		payOriginAttested: false,
		summary: null,
		lastCheckedAt: null,
		verificationExpiresAt: null,
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
		// admin's own sentence for a paid-but-not-provisioned workspace.
		provisioningNote: "",
	};
}

// ---- helpers ---------------------------------------------------------------

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
		// plan-09 WS7 honest holds: a raw-handle pre-cutover answer, or a token we
		// cannot navigate with. Neither offers a payment action (no fallback).
		case CODES.CLIENT_UPGRADE_REQUIRED:
		case CODES.BENCH_PAY_ORIGIN_UNCONFIGURED:
			return STATES.FAILED_TERMINAL;
		// plan-09 WS7: a navigable pay-page token. Lands on UNKNOWN (a live, openable
		// intent, exactly as a payable answer always has) so canNavigateToPay accepts
		// it; the orchestrator then top-level-navigates.
		case CODES.PAYMENT_PAGE_REDIRECT:
			return STATES.UNKNOWN;
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
	CODES.PAYMENT_PAGE_REDIRECT,
	CODES.CLIENT_UPGRADE_REQUIRED,
	CODES.BENCH_PAY_ORIGIN_UNCONFIGURED,
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
	// permanently stuck.
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

		case EVENTS.NAVIGATED_TO_PAY: {
			// Paid is a floor here too: a late navigate (a second tab, a click that
			// landed after the confirm) must never take a settled signup back to the
			// pay page. And navigate ONLY from a state that canNavigateToPay accepts -
			// which is also the double-navigate guard: once here we are CHECKOUT_OPEN,
			// and CHECKOUT_OPEN is not a navigable source, so a second click no-ops.
			if (PAID_FLOOR.has(state.value)) return state;
			if (!canNavigateToPay(state)) {
				return illegal(state, strict, "navigate to pay without an attested token");
			}
			return { ...state, value: STATES.CHECKOUT_OPEN, busy: null };
		}

		case EVENTS.RETURNED_FROM_CHECKOUT: {
			// Back from the pay page (bfcache restore, history.back(), tab refocus). Do
			// NOT assume dismissal or paid: leave the actionless busy screen for a
			// checkable UNKNOWN and let the orchestrator reconcile server truth. Only
			// legal while we actually navigated away; a stray pageshow elsewhere no-ops.
			if (PAID_FLOOR.has(state.value)) return state;
			if (!RETURN_SOURCES.has(state.value)) return state;
			return {
				...state,
				value: STATES.UNKNOWN,
				busy: null,
				checkRequired: true,
			};
		}

		case EVENTS.RATE_LIMITED: {
			const secs = Number(event.retryAfterSeconds) || 0;
			const until = nowMs + (secs > 0 ? secs * 1000 : DEFAULT_COOLDOWN_MS);
			// A 429 can be the FIRST thing a cold page hears, and its envelope carries
			// no capability flags. Default the read-only capability to true so the page
			// is not left with the charging action as its only live control.
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
 * May the customer be navigated to the pay page from THIS state?
 *
 * The single predicate both the reducer's NAVIGATED_TO_PAY guard and the
 * orchestrator's decide-to-navigate read, so the SPA can never navigate from a
 * state the machine would refuse. Fail closed on the SOURCE STATE first (P1-1):
 * only a live, unsettled intent (UNKNOWN / FAILED_RETRYABLE) may navigate -
 * confirm_required, reconnect, terminal, verification, the paid floor and
 * provisioning all refuse, so a stale token can never drive a blind pay from a
 * state whose whole point is that paying again is unsafe. Then it requires the
 * FULL navigate-to-pay capability: a live token, the bench's own origin, AND
 * admin's attestation that the two agree (no fallback, §R P0-3).
 */
export function canNavigateToPay(state) {
	const s = state || {};
	if (!NAVIGABLE_SOURCE_STATES.has(s.value)) return false;
	return !!(s.payPageToken && s.payOrigin && s.payOriginAttested);
}

/**
 * The full top-level URL to navigate to for the current state, or "" when the
 * state is not navigable. The base path is a code-owned constant; the token rides
 * ONLY in the fragment (§3.1). Built from the bench's OWN configured, attested
 * origin - NEVER from a URL admin supplied.
 */
export const CHECKOUT_NAMESPACE = "jarvis-checkout";
export function payPageUrl(state) {
	if (!canNavigateToPay(state)) return "";
	return `${state.payOrigin}/${CHECKOUT_NAMESPACE}#t=${state.payPageToken}`;
}

function illegal(state, strict, why) {
	if (strict) throw new Error(`illegal transition: ${why} (from ${state.value})`);
	return { ...state, illegalTransitions: state.illegalTransitions + 1 };
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
	// attempt id is a different intent, so it is judged on its own terms and is
	// NEVER rejected by the generation counter (a legitimate replacement attempt
	// may carry a lower generation - a fresh intent starting its own count). The
	// generation fence below therefore governs only answers of the SAME attempt
	// (or an unattributable one).
	const incomingGen = data.generation;
	const incomingAttempt = data.attempt_id;
	const attemptChanged =
		incomingAttempt != null && state.attemptId != null && incomingAttempt !== state.attemptId;

	// ---- the generation fence: a stale answer from a superseded intent of the
	// SAME attempt is ignored OUTRIGHT (a two-tab race cannot even repaint). A
	// missing generation is NOT zero - a legacy admin sends none, and the known
	// generation must survive such an answer rather than being reset by it.
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
			// A parked-money refusal is not a navigate answer: drop any stale token so
			// nothing can navigate off it.
			payPageToken: "",
			payOrigin: "",
			payOriginAttested: false,
		};
		next.canCheck = recomputeCanCheck(next, null);
		return next;
	}

	// ---- rate limit arriving as a decoded failure (belt-and-braces).
	if (code === CODES.PAYMENT_CHECK_RATE_LIMITED) {
		if (PAID_FLOOR.has(state.value)) return state;
		const next = reduce(state, {
			type: EVENTS.RATE_LIMITED,
			retryAfterSeconds: decoded.retryAfterSeconds || 0,
			nowMs: opts.nowMs || 0,
		});
		// A 429 over a KNOWN payment state must not overwrite it. But on a COLD page
		// this is the only thing we have been told, and leaving code empty rendered
		// the alarming catch-all instead of this code's own truthful row.
		if (PAYMENT_STATE_CODES.has(state.code)) return next;
		return { ...next, code };
	}

	// ---- a transport / non-payment failure says NOTHING about the money.
	const isFailure = decoded.ok === false;
	const mapped = stateForCode(code, data);
	if (isFailure && !PAYMENT_STATE_CODES.has(code)) {
		// A page that already knows a payment state keeps it, and only flags that
		// the check itself failed. A page that knows nothing renders unknown. Either
		// way the read-only capability defaults TRUE when nothing is known (checking
		// is a read; it creates no intent and charges nothing). `initiate` is never
		// defaulted this way.
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
			// A transport failure carries no token: never leave a stale one navigable.
			payPageToken: "",
			payOrigin: "",
			payOriginAttested: false,
		});
	}

	// ---- a real payment state.
	if (PAID_FLOOR.has(state.value)) {
		// Paid is a floor: absorb the reconciliation flag and capability tweaks if
		// any, but never move the state back.
		return state;
	}

	const supersededGen =
		!attemptChanged &&
		incomingGen != null &&
		state.generation != null &&
		Number(incomingGen) > Number(state.generation);
	// A new attempt (P1-4) or an advanced generation is a fresh intent: it resets
	// the client-local support counter with its live checkout.
	const freshIntent = attemptChanged || supersededGen;

	// plan-09 WS7: the pay-page token and the bench's OWN attested origin, refreshed
	// from THIS answer (the read envelope re-serves the live token). Reset to
	// empty/false whenever the answer does not carry them, so a stale token can
	// never linger and drive a later navigate - fail closed.
	const payPageToken = data.pay_page_token || "";
	const payOrigin = payPageToken ? data.pay_origin || "" : "";
	const payOriginAttested = payPageToken ? !!data.pay_origin_attested : false;

	const next = {
		...state,
		code: code || state.code,
		message: decoded.message || "",
		recovery: decoded.recovery || data.recovery || "",
		busy: null,
		transportError: false,
		attemptId: incomingAttempt || state.attemptId,
		// A new attempt with no generation of its own resets the counter to unknown
		// rather than inheriting the replaced intent's.
		generation:
			incomingGen != null ? Number(incomingGen) : attemptChanged ? null : state.generation,
		provider:
			(data.payment_provider || state.provider || null) &&
			(data.payment_provider || state.provider),
		payPageToken,
		payOrigin,
		payOriginAttested,
		lastCheckedAt: data.payment_last_checked_at || state.lastCheckedAt,
		verificationExpiresAt: data.verification_expires_at || state.verificationExpiresAt,
		awaitingReconciliation: !!data.awaiting_manual_reconciliation,
		canReconnect: !!data.can_reconnect || state.canReconnect,
		notStarted: false,
	};

	if (mapped) next.value = mapped;

	// plan-09 WS7: a pay-page token that is NOT navigable (origin unset/invalid, or
	// its digest did not match admin's attestation) is an honest config hold, NOT a
	// navigable state and NEVER a fallback. Rewrite the code + state so the page
	// says so with a support hint instead of flashing the redirect copy forever.
	if (
		code === CODES.PAYMENT_PAGE_REDIRECT &&
		!(payPageToken && payOrigin && payOriginAttested)
	) {
		next.code = CODES.BENCH_PAY_ORIGIN_UNCONFIGURED;
		next.value = STATES.FAILED_TERMINAL;
	}

	// A fresh intent puts the previous intent's "you have checked many times" offer
	// away. Keyed the same way supportHandoff keys it - (attempt, generation) - so
	// this and recordCheck cannot disagree.
	if (freshIntent) {
		next.supportChecks = emptyCounter();
		next.supportOffered = false;
	}

	next.summary = buildSummary(state.summary, data, context);

	// Capability flags: honour the backend, then let the reconciliation flag veto
	// initiate. A code that is definitionally paid/terminal never offers initiate
	// regardless of what a flag says.
	const backendCanInitiate =
		"can_initiate_payment" in data ? !!data.can_initiate_payment : next.canInitiate;
	next._backendCanCheck =
		"can_check_status" in data
			? !!data.can_check_status
			: state._backendCanCheck == null
			? true
			: state._backendCanCheck;
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
