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
	canOpenCheckout,
	handlesForProvider,
	initialState,
	reduce,
	noteStatusCheck,
	isTerminalForPayment,
} from "./paymentMachine.js";

// Codes that END a confirm poll: the gateway has decided, and asking again just
// spends another live call on an answer that will not change.
const DECIDED_CONFIRM_CODES = new Set([
	CODES.PAYMENT_DECLINED,
	CODES.PAYMENT_ALREADY_ACTIVE,
	CODES.SIGNUP_TERMINAL,
	CODES.ACCOUNT_RECONNECT_REQUIRED,
	CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM,
	CODES.BENCH_AWAITING_RECONCILIATION,
]);

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
	// Guards the 90s provisioning poll against re-entry (see waitForProvisioning).
	let provisioningInFlight = false;

	function apply(event) {
		state.value = reduce(state.value, event, { strict, nowMs: now() });
		return state.value;
	}

	function cancelInFlight() {
		token += 1;
		// ...and the flag those calls were holding. Every release path is fenced on
		// `my === token`, so the bump that invalidates the in-flight work also
		// invalidates its own release: a cancel mid-round-trip left `busy` set
		// forever, which is a dead Verify button (or two dead recovery actions) on a
		// page whose whole job is to offer the customer a way forward. Once nothing
		// is in flight, busy belongs to nobody.
		if (state.value.busy !== null) state.value = { ...state.value, busy: null };
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
		// Every failure - rate limits included - goes through the ONE contract
		// path. The rate limit used to be special-cased into a bare RATE_LIMITED
		// event here, which set the cooldown but never the code, so a cold page
		// whose first answer was a 429 rendered the alarming catch-all instead of
		// this code's own row. applyContract sets the cooldown AND the code (and
		// still refuses to overwrite a known payment state with it).
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
		// The 14th fence point. A mount read is slow (it is the first request of
		// the page) and can land AFTER the customer has already opened a checkout
		// sheet from a second tab or a fast click - at which point absorbing it
		// reset a LIVE gateway session back to `review`, with the sheet still open
		// over the top of it.
		const my = token;
		const decoded = ingest(await api.getOnboardingState());
		if (my !== token) return { paid: null, truthKnown: false, notStarted: false, superseded: true };
		// ...and the fence that the token alone cannot draw: a mount read and the
		// checkout it races share one token (nothing bumps between them), so the
		// STATE is the discriminator. hydrate() is the PASSIVE read; it must never
		// unseat a gateway interaction that is already in front of the customer.
		const live = state.value.value;
		if (live === STATES.CHECKOUT_OPEN || live === STATES.CONFIRMING) {
			return { paid: null, truthKnown: false, notStarted: false, superseded: true };
		}
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

	// ---- "I've verified my email" ------------------------------------------
	// One round trip, like the flow this replaced. The retired onVerifyCheck
	// polled and, if the answer carried checkout handles, opened the gateway on
	// the SAME click. Routing this action at a bare hydrate() cost the customer an
	// extra click and an extra round trip, and - worse - showed them
	// failure-framed recovery copy at the exact moment their verification had
	// just succeeded. So: re-read, and if the signup is now payable, go straight
	// to the sheet.
	async function verifyAndContinue() {
		// The same in-flight guard its siblings hold, and it matters MORE here than
		// on either of them: this path opens the checkout on success, so an
		// un-guarded triple-click did not merely spend three provider-truth calls
		// against the hourly cap - it stacked three gateway sheets on top of each
		// other. (Not a double charge: one order id, payable once at the gateway,
		// and the PAID floor absorbs the late dismissals. A broken money screen all
		// the same.)
		if (state.value.busy === "verifying") return;
		const my = token;
		state.value = { ...state.value, busy: "verifying" };
		let opened = false;
		try {
			const decoded = ingest(await api.getOnboardingState());
			if (my !== token) return;
			const code = absorb(decoded);
			if (!decoded.ok) return;
			if (code === CODES.SIGNUP_VERIFICATION_REQUIRED) return; // still unclicked
			// Verified and payable: open the checkout this same click. runCheckout
			// drives its own state from here, so the guard is released first -
			// leaving it set would disable the recovery card the sheet returns to.
			//
			// Payable is asked of the MACHINE, not of the answer. Reading it off
			// `decoded.data` asked a different question, and the two could disagree:
			// an answer the reducer refused (no generation to attribute it to, a
			// losing generation) still looked payable here, so a sheet opened while
			// the machine stayed on the card behind it - Verify or Initiate live
			// under an opening gateway, and in the fenced case the sheet was raised
			// on the DEAD order the reducer had just thrown away.
			if (canOpenCheckout(state.value)) {
				opened = true;
				releaseVerifyGuard(my);
				await runCheckout();
			}
		} finally {
			// Cleared on every exit, including a thrown round trip: a stuck busy flag
			// is the same class of trap as a cooldown that never lifts.
			if (!opened) releaseVerifyGuard(my);
		}
	}

	function releaseVerifyGuard(my) {
		if (my === token && state.value.busy === "verifying") {
			state.value = { ...state.value, busy: null };
		}
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
		await runCheckout(provider);
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
		await runCheckout(provider);
	}

	// ---- the shared checkout tail ------------------------------------------
	// The ONE place a sheet is opened, and the one place that decides whether it
	// may be: `canOpenCheckout` is the reducer's own CHECKOUT_OPENED guard, so a
	// refusal here and a refusal there cannot drift apart. If the machine did not
	// take the answer, nothing opens and the caller's state renders as it stands.
	async function runCheckout(provider) {
		if (!canOpenCheckout(state.value)) {
			// Nothing opened, so nothing is in flight: release the flag the caller
			// took. The reducer clears `busy` on every answer it accepts, but the
			// answers this branch exists for are the ones it REFUSES (it returns the
			// previous state untouched, flag and all) - and the card the customer is
			// left on is the one whose buttons that flag disables.
			if (state.value.busy !== null) state.value = { ...state.value, busy: null };
			return;
		}
		// Opened on what the MACHINE kept, never on the raw answer: the reducer is
		// the thing that knows which handles belong to the live intent. And kept
		// handles ACCUMULATE - a same-generation answer merges into them - so they
		// are narrowed to the gateway the machine names before anything classifies
		// them, or a stale session from the other gateway captures the shape and
		// opens the wrong sheet on a live order.
		const prov = state.value.provider || provider;
		const handles = handlesForProvider(state.value.handles, prov);
		if (prov && !handles.payment_provider) handles.payment_provider = prov;
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

	// NOTE: there is deliberately no client-driven "confirm an authorized mandate"
	// action. PAYMENT_AUTHORIZED_PENDING_CONFIRM is a WAIT state: admin's
	// confirm_payment signature-verifies before any branch and needs a
	// gateway-issued payment id + signature that only a live Checkout callback
	// produces - and admin emits that code precisely BECAUSE it could not resolve
	// the authorization payment id itself. Every client-built payload 402s, so a
	// Confirm button returned a byte-identical screen forever with no other
	// affordance (can_initiate_payment is false by design there). The real
	// resolver is the gateway webhook; the support handoff after N checks is that
	// state's exit. confirmOnce below is reached only from a REAL callback.
	async function confirmOnce(payload) {
		apply({ type: EVENTS.GATEWAY_CALLBACK });
		const my = token;
		const res = await api.confirmSignupPayment(payload);
		if (my !== token) return;
		const decoded = ingest(res);
		// A 200 is NOT a payment. Read what the body actually says: only admin's
		// connection payload (allocated, or the allocation-failure shape that still
		// records the money) means confirmed. A bare 200 with an empty or unrelated
		// body used to fire CONFIRM_SUCCEEDED and land the customer on "paid" with
		// nothing behind it.
		if (decoded.ok && effectiveCode(decoded) === CODES.PAYMENT_ALREADY_ACTIVE) {
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
			if (decoded.ok && effectiveCode(decoded) === CODES.PAYMENT_ALREADY_ACTIVE) {
				apply({ type: EVENTS.CONFIRM_SUCCEEDED, data: decoded.data });
				return;
			}
			// A DECIDED answer ends the loop. Only "not settled yet" is worth another
			// pass: re-polling a gateway that has already said DECLINED eleven more
			// times spends eleven live Cashfree calls, delays the verdict by half a
			// minute, and then throws the coded decline away in favour of a generic
			// "we could not determine" - the one thing the customer already knew.
			if (DECIDED_CONFIRM_CODES.has(decoded.code)) {
				apply({ type: EVENTS.CONFIRM_FAILED, decoded });
				await reconcileAfterFailure();
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
		// In-flight guard. Without it the Check button stayed live through its own
		// round trip, so an impatient customer fired N concurrent provider-truth
		// calls - each one spending a gateway call against the per-account cap,
		// burning the client-local support counter in seconds, and walking straight
		// into the rate limit that this page then has to explain.
		if (state.value.busy === "checking") return;
		const my = token;
		state.value = { ...state.value, busy: "checking" };
		try {
			const res = await api.checkSignupPaymentStatus();
			if (my !== token) return;
			const decoded = ingest(res);
			absorb(decoded);
			// A rate limit asserts nothing about the money and is not a "check" that
			// counts toward the support ceiling - the customer never got an answer.
			if (decoded.code !== CODES.PAYMENT_CHECK_RATE_LIMITED) {
				state.value = noteStatusCheck(state.value);
			}
		} finally {
			if (my === token && state.value.busy === "checking") {
				state.value = { ...state.value, busy: null };
			}
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
			return;
		}
		// Money the gateway is holding that an operator has to place. This is the
		// single most important thing a mandatory check can learn, and dropping it
		// meant the page went straight back to offering "Initiate payment again" on
		// a signup that has ALREADY been paid once. The facade also refuses the
		// click server-side, so this is the second lock rather than the only one -
		// but a customer must never be invited to pay twice in the first place.
		if (decoded.ok && (decoded.data || {}).awaiting_manual_reconciliation) {
			absorb(decoded);
		}
	}

	// ---- provisioning: the old proceedAfterPay loop, fenced ----------------
	async function waitForProvisioning() {
		// Re-entrancy guard. "Check setup status" is a 90-second loop behind a
		// button that never disabled itself, and the state does not leave
		// PROVISIONING_DELAYED while it runs - so every impatient click spawned
		// ANOTHER concurrent 45x2s loop, all sharing one token that nothing bumps,
		// all polling sync_connection forever.
		if (provisioningInFlight) return { status: "already_running" };
		provisioningInFlight = true;
		try {
			return await runProvisioningPoll();
		} finally {
			provisioningInFlight = false;
		}
	}

	async function runProvisioningPoll() {
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
		verifyAndContinue,
		submitReview,
		initiatePayment,
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
