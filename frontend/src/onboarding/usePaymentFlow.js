/**
 * The payment orchestrator: everything the reducer is forbidden to do.
 *
 * The reducer (paymentMachine) is a pure transition table. This is the place
 * where API calls happen, where the checkout SDK is launched, and where the two
 * long poll loops run - so all of that can be cancelled, generation-fenced and
 * browser-tested apart from the render.
 *
 * The rules this file exists to hold, none of which fit in a pure reducer:
 *
 *   - **Check-on-failure is mandatory.** Every dead or dismissed checkout, and
 *     every failed confirm, is followed by a provider-truth check before the
 *     page renders a verdict - because the idempotency-key reuse the bench does
 *     is only safe if the SPA reconciles rather than assumes.
 *   - **One in-flight generation.** A `cancelInFlight()` bumps a token; any
 *     answer, or any poll iteration, that belongs to a superseded token is
 *     dropped. This is what fences confirmCashfree (12×3s) and proceedAfterPay
 *     (45×2s) - the two loops the plan named - plus a superseded initiate.
 *   - **The SPA never sends an idempotency key.** The bench owns that receipt
 *     (onboarding_contract.next_idempotency_key); handing one to the browser
 *     invites it to replay a dead intent. So `initiate` posts no key, ever.
 *   - **Raw responses, decoded here.** Frappe-ui's `call()` throws on a 4xx and
 *     discards the body, but the whole contract lives in that body (the stamped
 *     `error`, the `{ok,data,context}` envelope). The api layer injected here
 *     returns `{status, body, networkError}` untouched, and `decode` reads it.
 *
 * Vue is imported lazily-ish (only `ref`) so the module still parses under
 * `node --test`; the spec drives it under vitest with a plain state holder.
 */

import { ref } from "vue";

import { decode, effectiveCode, CLIENT_OFFLINE } from "./paymentCodec.js";
import { CODES } from "./paymentCodes.js";
import {
	EVENTS,
	STATES,
	initialState,
	reduce,
	noteStatusCheck,
	isTerminalForPayment,
} from "./paymentMachine.js";

const PROVISIONING_ATTEMPTS = 45; // ~45 × 2s ≈ 90s (was proceedAfterPay)
const PROVISIONING_INTERVAL_MS = 2000;
const CASHFREE_CONFIRM_ATTEMPTS = 12; // 12 × 3s (was confirmCashfree)
const CASHFREE_CONFIRM_INTERVAL_MS = 3000;

/**
 * @param {object} deps
 * @param {object} deps.api  raw-response endpoints (see api.js onboardingRaw.*)
 * @param {Function} deps.openCheckout  opens the right sheet (onboardingCheckout)
 * @param {Function} [deps.sleep]
 * @param {Function} [deps.now]  ms clock (injected so cooldowns are testable)
 * @param {object} [deps.storage]  {get,set} for the non-secret attempt hint
 * @param {boolean} [deps.strict]  reducer strict mode (dev/test)
 */
export function createPaymentFlow(deps) {
	const {
		api,
		openCheckout,
		sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
		now = () => Date.now(),
		storage = memoryStorage(),
		strict = false,
	} = deps;

	const state = ref(initialState());
	// The generation token: every mutating action captures it, and anything that
	// resumes after `cancelInFlight` compares against it and bails. NOT the
	// server's payment generation - this fences the CLIENT's in-flight work
	// (a superseded loop, a stale slow response), which is a separate concern
	// from the server's intent generation the reducer fences on.
	let token = 0;

	function apply(event) {
		state.value = reduce(state.value, event, { strict, nowMs: now() });
		return state.value;
	}

	function cancelInFlight() {
		token += 1;
	}

	// ---- the read/decode seam ----------------------------------------------
	function ingest(res) {
		return decode(res);
	}

	// Fold a decoded answer into the machine. A success is normalised so a FLAT
	// legacy body (no code) still lands on the right state via effectiveCode.
	function absorb(decoded) {
		if (decoded.ok) {
			const code = effectiveCode(decoded);
			apply({ type: EVENTS.CONTRACT_STATE, decoded: { ...decoded, code } });
			return code;
		}
		// A rate limit is the one failure with a parsed backoff to hand the
		// reducer as its own event (so the cooldown clock is set from `now`).
		if (decoded.code === CODES.PAYMENT_CHECK_RATE_LIMITED) {
			apply({ type: EVENTS.RATE_LIMITED, retryAfterSeconds: decoded.retryAfterSeconds, nowMs: now() });
			return decoded.code;
		}
		apply({ type: EVENTS.CONTRACT_STATE, decoded });
		return decoded.code;
	}

	// ---- mount: server truth wins ------------------------------------------
	/**
	 * Reconcile on mount. Returns {paid, truthKnown, notStarted}:
	 *   paid === true   - an authoritative Active/paid answer
	 *   paid === false  - a definite not-paid answer (incl. day-one)
	 *   paid === null   - the control plane could not be reached; paid is UNKNOWN,
	 *                     never assumed false (the C02-1 correction: "has
	 *                     credentials" is not "has paid", and neither is "the
	 *                     check failed").
	 * The caller (the router/gate + the view) decides routing from this - it must
	 * NOT read "has llm_credentials" as "has paid".
	 */
	async function hydrate() {
		const decoded = ingest(await api.getOnboardingState());
		if (decoded.code === CLIENT_OFFLINE || decoded.code === CODES.BENCH_ADMIN_UNREACHABLE) {
			absorb(decoded);
			return { paid: null, truthKnown: false, notStarted: false };
		}
		const code = absorb(decoded);
		if (decoded.notStarted || code === CODES.BENCH_NO_SIGNUP_CONTEXT) {
			return { paid: false, truthKnown: true, notStarted: true };
		}
		const paid = isPaidState(state.value.value);
		return { paid, truthKnown: true, notStarted: false };
	}

	// ---- submit review: start the signup exactly once ----------------------
	async function submitReview({ email, company, plan, provider } = {}) {
		apply({ type: EVENTS.SUBMIT_REVIEW });
		const my = token;
		const decoded = ingest(await api.startSignup({ email, company, plan, provider }));
		if (my !== token) return;
		const code = absorb(decoded);
		if (!decoded.ok) return; // parked-money / duplicate / terminal - the reducer rendered it
		if (code === CODES.SIGNUP_VERIFICATION_REQUIRED) return; // wait for the magic link
		await runCheckout(decoded, provider);
	}

	// ---- initiate (retry): authenticated, no idempotency key from the SPA ---
	async function initiatePayment({ plan, provider } = {}) {
		state.value = { ...state.value, busy: "initiating" };
		const my = token;
		// No idempotency_key: the bench mints/reuses its own receipt. Passing one
		// would let the browser replay a dead intent.
		const decoded = ingest(await api.initiateSignupPayment({ plan, provider }));
		if (my !== token) return;
		const code = absorb(decoded);
		if (!decoded.ok) return;
		if (code === CODES.SIGNUP_VERIFICATION_REQUIRED) return;
		await runCheckout(decoded, provider);
	}

	// ---- the shared checkout tail ------------------------------------------
	async function runCheckout(decoded, provider) {
		const handles = { ...decoded.data };
		if (provider && !handles.payment_provider) handles.payment_provider = provider;
		const my = token;
		let out;
		try {
			apply({ type: EVENTS.CHECKOUT_OPENED });
			out = await openCheckout(handles, { description: "Jarvis subscription" });
		} catch (e) {
			// The sheet could not be opened at all. Stay on Pay, retryable, and -
			// mandatory - go and ASK the gateway rather than guess.
			if (my !== token) return;
			apply({ type: EVENTS.CHECKOUT_FAILED, message: e && e.message });
			await reconcileAfterFailure();
			return;
		}
		if (my !== token) return;

		if (out.leavesPage) return; // Cashfree mandate: the browser is redirecting away

		if (out.status === "dismissed") {
			apply({ type: EVENTS.CHECKOUT_DISMISSED });
			await reconcileAfterFailure(); // check-on-failure
			return;
		}

		// A success from the sheet: confirm it. Cashfree orders have no client
		// signature and settle only server-side, so they poll the confirm; a
		// Razorpay success confirms once.
		if (out.pollConfirm) {
			await confirmCashfreeLoop(out.payload);
		} else {
			await confirmOnce(out.payload);
		}
	}

	// The confirm_required action: a mandate the gateway has already AUTHORIZED
	// (PAYMENT_AUTHORIZED_PENDING_CONFIRM). The recovery is to CONFIRM it - a
	// second intent would authorize a second mandate - through the same OLD-shape
	// finish_payment endpoint a callback would have used. The handles the state
	// carries are the confirm payload; provider rides so admin branches right.
	async function confirmAuthorized() {
		const h = state.value.handles || {};
		const payload = state.value.provider === "cashfree"
			? { provider: "cashfree", cashfree_order_id: h.cashfree_order_id, cashfree_subscription_id: h.cashfree_subscription_id }
			: {
					razorpay_subscription_id: h.razorpay_subscription_id,
					razorpay_order_id: h.razorpay_order_id,
			  };
		await confirmOnce(payload);
	}

	async function confirmOnce(payload) {
		apply({ type: EVENTS.GATEWAY_CALLBACK });
		const my = token;
		const res = await api.confirmSignupPayment(payload);
		if (my !== token) return;
		const decoded = ingest(res);
		if (decoded.ok) {
			apply({ type: EVENTS.CONFIRM_SUCCEEDED, data: decoded.data });
			return;
		}
		// A coded decline renders as itself; anything else falls to unknown AND
		// checks provider truth - the payment may have succeeded past a dropped
		// confirm round-trip.
		apply({ type: EVENTS.CONFIRM_FAILED, decoded });
		await reconcileAfterFailure();
	}

	// The old confirmCashfree loop, fenced. 12 × 3s: Cashfree confirms server-
	// side (admin fetches the real order status), so this polls until PAID or
	// the ceiling, and STOPS the moment its generation is superseded.
	async function confirmCashfreeLoop(payload) {
		apply({ type: EVENTS.GATEWAY_CALLBACK });
		const my = token;
		for (let i = 0; i < CASHFREE_CONFIRM_ATTEMPTS; i++) {
			if (my !== token) return;
			const decoded = ingest(await api.confirmSignupPayment(payload));
			if (my !== token) return;
			if (decoded.ok) {
				apply({ type: EVENTS.CONFIRM_SUCCEEDED, data: decoded.data });
				return;
			}
			await sleep(CASHFREE_CONFIRM_INTERVAL_MS);
		}
		if (my !== token) return;
		// Never confirmed inside the window. Do NOT claim a failure - fall to
		// unknown and let a status check (or the webhook) settle it.
		apply({ type: EVENTS.CONFIRM_FAILED, decoded: { ok: false, code: "", message: "" } });
		await reconcileAfterFailure();
	}

	// ---- check status: the provider-truth read -----------------------------
	// The user-driven "Check payment status" button: apply whatever the gateway
	// says (pending stays pending, a decline becomes a decline, paid advances),
	// and count it toward the client-local support ceiling.
	async function checkStatus() {
		const my = token;
		const res = await api.checkSignupPaymentStatus();
		if (my !== token) return;
		const decoded = ingest(res);
		absorb(decoded);
		// A rate limit asserts nothing about the money and is not a "check" that
		// counts toward the support ceiling - the customer never got an answer.
		if (decoded.code !== CODES.PAYMENT_CHECK_RATE_LIMITED) {
			state.value = noteStatusCheck(state.value);
		}
	}

	// Check-on-failure: mandatory after a dead/dismissed checkout or a failed
	// confirm. Its ONE job is to catch a payment that actually landed - so only a
	// PAID discovery moves the page, and a non-paid answer leaves the failure
	// framing (and its "try again" copy) exactly where it was rather than
	// downgrading it to a generic pending. It is not a user check, so it does not
	// count toward the support ceiling. A rate limit still sets the cooldown.
	async function reconcileAfterFailure() {
		const my = token;
		const res = await api.checkSignupPaymentStatus();
		if (my !== token) return;
		const decoded = ingest(res);
		if (decoded.ok && effectiveCode(decoded) === CODES.PAYMENT_ALREADY_ACTIVE) {
			absorb(decoded);
			return;
		}
		if (decoded.code === CODES.PAYMENT_CHECK_RATE_LIMITED) {
			absorb(decoded);
		}
	}

	// ---- provisioning: the old proceedAfterPay loop, fenced ----------------
	async function waitForProvisioning() {
		const my = token;
		// The transition into provisioning is legal only from paid - the readiness
		// gate owns this surface, and `unknown -> provisioning` is the illegal move
		// plan 02 names. Enter it only when we are actually paid; otherwise just
		// run the poll (the caller drives the state).
		if (state.value.value === STATES.PAID) {
			apply({ type: EVENTS.PROVISIONING_STARTED });
		}
		for (let i = 0; i < PROVISIONING_ATTEMPTS; i++) {
			if (my !== token) return { status: "superseded" };
			let r;
			try {
				r = await api.syncConnection();
			} catch (e) {
				r = null;
			}
			if (my !== token) return { status: "superseded" };
			if (r && (r.synced || r.tenant_status === "running")) {
				return { status: "ready" };
			}
			await sleep(PROVISIONING_INTERVAL_MS);
		}
		if (my !== token) return { status: "superseded" };
		if (state.value.value === STATES.PROVISIONING) {
			apply({ type: EVENTS.PROVISIONING_DELAYED });
		}
		return { status: "delayed" };
	}

	// A wall-clock tick from the view lifts an expired cooldown so the Check
	// button re-enables itself without another request.
	function tickCooldown() {
		apply({ type: EVENTS.COOLDOWN_ELAPSED });
	}

	return {
		state,
		hydrate,
		submitReview,
		initiatePayment,
		confirmAuthorized,
		checkStatus,
		waitForProvisioning,
		cancelInFlight,
		tickCooldown,
	};
}

function isPaidState(value) {
	return (
		value === STATES.PAID ||
		value === STATES.PROVISIONING ||
		value === STATES.PROVISIONING_DELAYED
	);
}

function memoryStorage() {
	const m = new Map();
	return { get: (k) => m.get(k) || null, set: (k, v) => m.set(k, v) };
}

export { isTerminalForPayment };
