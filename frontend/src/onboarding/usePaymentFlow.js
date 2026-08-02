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
import { counterKey, shouldOfferSupport } from "./supportHandoff.js";

// The support-counter persistence key prefix (P2-5). Stores only a non-secret
// (attempt, generation) key and its integer check count under the injected
// storage - never a gateway id, payload, or token.
const SUPPORT_KEY_PREFIX = "jvpay:sc:";

function elapsedBucket(ms) {
	if (!(ms >= 0)) return "unknown";
	if (ms < 1000) return "<1s";
	if (ms < 5000) return "1-5s";
	if (ms < 30_000) return "5-30s";
	return ">30s";
}

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
		// PII-free transition sink (P2-3). Called with {event, from, to, code,
		// provider, generation, elapsed_bucket, source} on every state change, and
		// {event:"payment_illegal_transition", ...} when the reducer counts one. It
		// carries NO email/company/address/handle/payment-id/token - only the shape
		// of the move. Default is a no-op so node --test never needs a sink.
		telemetry = () => {},
		// Wall-clock deadlines (P0-3). Every payment fetch is raced against
		// `fetchDeadlineMs`; the provider-open await (an SDK that may never settle)
		// against the far more generous `openDeadlineMs`. A deadline is a CLIENT
		// abort, never a server verdict - every timeout reconciles server truth.
		fetchDeadlineMs = 30_000,
		openDeadlineMs = 20 * 60_000,
	} = deps;

	const state = ref(initialState());
	// The generation token: every mutating action captures it, and anything that
	// resumes after `cancelInFlight` compares against it and bails. NOT the
	// server's payment generation - this fences the CLIENT's in-flight work
	// (a superseded loop, a stale slow response), which is a separate concern
	// from the server's intent generation the reducer fences on. A NEW token is
	// minted per action (P1-2), so a late answer from a finished-then-abandoned
	// action is fenced even without an explicit cancel.
	let token = 0;
	// The one incompatible-action lock (P1-2). submitReview / initiatePayment /
	// checkStatus / verifyAndContinue all take it before their first API call, so
	// a burst of clicks - or a check racing an initiate - produces exactly one
	// live action and one gateway open. `lockToken` is the token that holds it, so
	// a release only ever frees the lock it took (never a later action's).
	let actionLock = false;
	let lockToken = 0;
	// Guards the 90s provisioning poll against re-entry (see waitForProvisioning).
	let provisioningInFlight = false;
	// For the elapsed-time bucket on transition telemetry.
	let lastTransitionAt = now();

	function apply(event) {
		const before = state.value;
		const next = reduce(before, event, { strict, nowMs: now() });
		state.value = next;
		emitTransition(before, next, event);
		return next;
	}

	function emitTransition(before, next, event) {
		try {
			if (before.value !== next.value) {
				const t = now();
				const elapsed = t - lastTransitionAt;
				lastTransitionAt = t;
				telemetry({
					event: "payment_transition",
					from: before.value,
					to: next.value,
					code: next.code || "",
					provider: next.provider || "",
					generation: next.generation == null ? null : Number(next.generation),
					elapsed_bucket: elapsedBucket(elapsed),
					source: event.type,
				});
			}
			if (next.illegalTransitions > before.illegalTransitions) {
				telemetry({
					event: "payment_illegal_transition",
					from: before.value,
					attempted: event.type,
					code: next.code || "",
					generation: next.generation == null ? null : Number(next.generation),
				});
			}
		} catch (e) {
			// Telemetry must never break the machine.
		}
	}

	// Take the one action lock, mint this action's token, and (optionally) show a
	// busy flag. Returns the fresh token, or 0 when an incompatible action already
	// holds the lock - the caller then does nothing (the burst guard).
	function beginAction(displayBusy) {
		if (actionLock) return 0;
		actionLock = true;
		token += 1;
		lockToken = token;
		if (displayBusy) state.value = { ...state.value, busy: displayBusy };
		return token;
	}

	// Release the lock IF this token still holds it, and clear any busy flag it
	// left. Fenced on `lockToken === my` so a cancelled action's late release
	// cannot free a newer action's lock.
	function endAction(my) {
		if (lockToken !== my) return;
		actionLock = false;
		if (state.value.busy !== null) state.value = { ...state.value, busy: null };
	}

	function cancelInFlight() {
		token += 1;
		// Abandon whatever is in flight: free the lock so a fresh action may start,
		// and drop the flag those calls were holding. Every release path is fenced
		// on `my === token`, so the bump that invalidates the in-flight work also
		// invalidates its own release: a cancel mid-round-trip left `busy` set
		// forever, which is a dead Verify button (or two dead recovery actions) on a
		// page whose whole job is to offer the customer a way forward. Once nothing
		// is in flight, the lock and busy belong to nobody.
		actionLock = false;
		if (state.value.busy !== null) state.value = { ...state.value, busy: null };
	}

	// Race a payment fetch/SDK-open against a wall-clock deadline (P0-3). On
	// timeout it aborts the browser wait (via AbortSignal where the api honours
	// one) and rejects with `isTimeout`; it NEVER resolves to a fabricated answer,
	// because a client abort says nothing about what the server did - the caller
	// reconciles server truth after every timeout.
	async function deadlined(fn, ms, label) {
		const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
		let timer = null;
		try {
			return await new Promise((resolve, reject) => {
				timer = setTimeout(() => {
					if (controller) {
						try {
							controller.abort();
						} catch (e) {
							/* jsdom without abort support */
						}
					}
					const err = new Error(`payment request timed out: ${label}`);
					err.isTimeout = true;
					reject(err);
				}, ms);
				Promise.resolve()
					.then(() => fn(controller ? controller.signal : undefined))
					.then(resolve, reject);
			});
		} finally {
			if (timer) clearTimeout(timer);
		}
	}

	// A decoded "the network never answered" verdict, routed through the reducer's
	// existing transport path so a timeout/abort becomes truth-UNKNOWN (keeping any
	// known payment state, offering the read-only check) - never a decline.
	function offlineDecoded() {
		return ingest({ status: 0, body: null, networkError: true });
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
		let decoded;
		try {
			decoded = ingest(
				await deadlined(
					(signal) => api.getOnboardingState({ signal }),
					fetchDeadlineMs,
					"state"
				)
			);
		} catch (e) {
			// The mount read timed out or the network died. Truth is UNKNOWN. Do NOT
			// unseat a live sheet (state guard below still applies), and never claim
			// not-paid from a client abort.
			if (my !== token)
				return { paid: null, truthKnown: false, notStarted: false, superseded: true };
			const liveNow = state.value.value;
			if (liveNow === STATES.CHECKOUT_OPEN || liveNow === STATES.CONFIRMING) {
				return { paid: null, truthKnown: false, notStarted: false, superseded: true };
			}
			absorb(offlineDecoded());
			return { paid: null, truthKnown: false, notStarted: false };
		}
		if (my !== token)
			return { paid: null, truthKnown: false, notStarted: false, superseded: true };
		// ...and the fence that the token alone cannot draw: a mount read and the
		// checkout it races share one token (nothing bumps between them), so the
		// STATE is the discriminator. hydrate() is the PASSIVE read; it must never
		// unseat a gateway interaction that is already in front of the customer. The
		// ONLY safe exit from checkout_open is the explicit returnFromCheckout()
		// path (P0-2), never this passive read.
		const live = state.value.value;
		if (live === STATES.CHECKOUT_OPEN || live === STATES.CONFIRMING) {
			return { paid: null, truthKnown: false, notStarted: false, superseded: true };
		}
		if (decoded.code === CLIENT_OFFLINE || decoded.code === CODES.BENCH_ADMIN_UNREACHABLE) {
			absorb(decoded);
			return { paid: null, truthKnown: false, notStarted: false };
		}
		const code = absorb(decoded);
		// Server truth now names the (attempt, generation): restore any persisted
		// support count for it, so a refresh between checks does not reset the offer.
		restoreSupport();
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
		const my = beginAction("verifying");
		if (!my) return; // an incompatible action already holds the lock (P1-2)
		let opened = false;
		try {
			const decoded = ingest(
				await deadlined(
					(signal) => api.getOnboardingState({ signal }),
					fetchDeadlineMs,
					"state"
				)
			);
			if (my !== token) return;
			const code = absorb(decoded);
			if (!decoded.ok) return;
			if (code === CODES.SIGNUP_VERIFICATION_REQUIRED) return; // still unclicked
			// Verified and payable: open the checkout this same click. runCheckout
			// drives its own state from here, so the lock is released first - leaving
			// it held would disable the recovery card the sheet returns to.
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
				endAction(my);
				await runCheckout();
			}
		} finally {
			// Released on every exit, including a thrown/timed-out round trip: a stuck
			// lock is the same class of trap as a cooldown that never lifts. (When we
			// opened, the lock was already released above.)
			if (!opened) endAction(my);
		}
	}

	// ---- submit review: start the signup exactly once ----------------------
	async function submitReview({ email, company, plan, provider } = {}) {
		const my = beginAction(null); // SUBMIT_REVIEW sets the busy flag itself
		if (!my) return; // a burst of clicks produces exactly one start (P1-2)
		try {
			apply({ type: EVENTS.SUBMIT_REVIEW });
			let decoded;
			try {
				decoded = ingest(
					await deadlined(
						(signal) =>
							api.startSignup({ email, company, plan, provider }, { signal }),
						fetchDeadlineMs,
						"start"
					)
				);
			} catch (e) {
				if (my !== token) return;
				// The start timed out/died: truth UNKNOWN, then a bounded reconcile so
				// the customer lands on a checkable recovery, never a frozen spinner.
				absorb(offlineDecoded());
				await reconcileAfterFailure();
				return;
			}
			if (my !== token) return;
			const code = absorb(decoded);
			if (!decoded.ok) return; // parked-money / duplicate / terminal - the reducer rendered it
			if (code === CODES.SIGNUP_VERIFICATION_REQUIRED) return; // wait for the magic link
			await runCheckout(provider);
		} finally {
			endAction(my);
		}
	}

	// ---- initiate (retry): authenticated, no idempotency key from the SPA ---
	async function initiatePayment({ plan, provider } = {}) {
		const my = beginAction("initiating");
		if (!my) return; // one initiate per burst; a check-vs-initiate race yields one (P1-2)
		try {
			let decoded;
			try {
				// No idempotency_key: the bench mints/reuses its own receipt. Passing
				// one would let the browser replay a dead intent.
				decoded = ingest(
					await deadlined(
						(signal) => api.initiateSignupPayment({ plan, provider }, { signal }),
						fetchDeadlineMs,
						"initiate"
					)
				);
			} catch (e) {
				if (my !== token) return;
				absorb(offlineDecoded());
				await reconcileAfterFailure();
				return;
			}
			if (my !== token) return;
			const code = absorb(decoded);
			if (!decoded.ok) return;
			if (code === CODES.SIGNUP_VERIFICATION_REQUIRED) return;
			await runCheckout(provider);
		} finally {
			endAction(my);
		}
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
		// The identity of the intent this sheet belongs to, captured at open time
		// and threaded onto every subsequent gateway event (P1-5). A callback that
		// arrives against a different attempt/older generation is a reducer-level
		// no-op - a late sheet cannot mutate a newer intent by event name alone.
		const identity = {
			attemptId: state.value.attemptId,
			generation: state.value.generation,
		};
		let out;
		try {
			apply({ type: EVENTS.CHECKOUT_OPENED });
			// The SDK/provider open is bounded too (P0-3): a sheet that never opens
			// (SDK promise that never settles) times out into a retryable failure
			// with a mandatory reconcile, rather than a permanent Opening… spinner.
			out = await deadlined(
				() => openCheckout(handles, { description: "Jarvis subscription" }),
				openDeadlineMs,
				"open"
			);
		} catch (e) {
			// The sheet could not be opened at all (or the open timed out). Stay on
			// Pay, retryable, and - mandatory - go and ASK the gateway rather than
			// guess.
			if (my !== token) return;
			apply({ type: EVENTS.CHECKOUT_FAILED, message: e && e.message, ...identity });
			await reconcileAfterFailure();
			return;
		}
		if (my !== token) return;

		if (out.leavesPage) return; // Cashfree mandate: the browser is redirecting away

		if (out.status === "dismissed") {
			apply({ type: EVENTS.CHECKOUT_DISMISSED, ...identity });
			await reconcileAfterFailure(); // check-on-failure
			return;
		}

		// A success from the sheet: confirm it. Cashfree orders have no client
		// signature and settle only server-side, so they poll the confirm; a
		// Razorpay success confirms once.
		if (out.pollConfirm) {
			await confirmCashfreeLoop(out.payload, identity);
		} else {
			await confirmOnce(out.payload, identity);
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
	async function confirmOnce(payload, identity = {}) {
		apply({ type: EVENTS.GATEWAY_CALLBACK, ...identity });
		const my = token;
		let decoded;
		try {
			decoded = ingest(
				await deadlined(
					(signal) => api.confirmSignupPayment(payload, { signal }),
					fetchDeadlineMs,
					"confirm"
				)
			);
		} catch (e) {
			// The confirm round-trip timed out/died. NOT a failure and NOT a paid:
			// fall to unknown and reconcile - the money may have landed past a
			// dropped response.
			if (my !== token) return;
			apply({
				type: EVENTS.CONFIRM_FAILED,
				decoded: { ok: false, code: "", message: "" },
				...identity,
			});
			await reconcileAfterFailure();
			return;
		}
		if (my !== token) return;
		// A 200 is NOT a payment. Read what the body actually says: only admin's
		// connection payload (allocated, or the allocation-failure shape that still
		// records the money) means confirmed. A bare 200 with an empty or unrelated
		// body used to fire CONFIRM_SUCCEEDED and land the customer on "paid" with
		// nothing behind it.
		if (decoded.ok && effectiveCode(decoded) === CODES.PAYMENT_ALREADY_ACTIVE) {
			apply({ type: EVENTS.CONFIRM_SUCCEEDED, data: decoded.data, ...identity });
			return;
		}
		// A DECIDED answer (a coded decline, terminal, reconnect, authorized-pending,
		// parked money) is already authoritative: render it and stop. Reconciling on
		// top would overwrite a definite verdict with a fresh "pending" read (P0-1's
		// mirror image - the mandatory check exists for AMBIGUOUS outcomes, not to
		// downgrade a verdict the gateway just gave us).
		apply({ type: EVENTS.CONFIRM_FAILED, decoded, ...identity });
		if (DECIDED_CONFIRM_CODES.has(decoded.code)) return;
		// Ambiguous (a bare failure, an unreadable body): the payment may have landed
		// past a dropped confirm round-trip, so ask provider truth.
		await reconcileAfterFailure();
	}

	// The old confirmCashfree loop, fenced. 12 × 3s: Cashfree confirms server-
	// side (admin fetches the real order status), so this polls until PAID or
	// the ceiling, and STOPS the moment its generation is superseded.
	async function confirmCashfreeLoop(payload, identity = {}) {
		apply({ type: EVENTS.GATEWAY_CALLBACK, ...identity });
		const my = token;
		for (let i = 0; i < CASHFREE_CONFIRM_ATTEMPTS; i++) {
			if (my !== token) return;
			let decoded;
			try {
				decoded = ingest(
					await deadlined(
						(signal) => api.confirmSignupPayment(payload, { signal }),
						fetchDeadlineMs,
						"confirm"
					)
				);
			} catch (e) {
				// A single poll timed out - "not settled yet", not a verdict. Sleep
				// and try again; the loop's own attempt ceiling still bounds it.
				if (my !== token) return;
				await sleep(CASHFREE_CONFIRM_INTERVAL_MS);
				continue;
			}
			if (my !== token) return;
			if (decoded.ok && effectiveCode(decoded) === CODES.PAYMENT_ALREADY_ACTIVE) {
				apply({ type: EVENTS.CONFIRM_SUCCEEDED, data: decoded.data, ...identity });
				return;
			}
			// A DECIDED answer ends the loop. Only "not settled yet" is worth another
			// pass: re-polling a gateway that has already said DECLINED eleven more
			// times spends eleven live Cashfree calls, delays the verdict by half a
			// minute, and then throws the coded decline away in favour of a generic
			// "we could not determine" - the one thing the customer already knew.
			if (DECIDED_CONFIRM_CODES.has(decoded.code)) {
				// Authoritative verdict - render it and stop, no reconcile (it would
				// only downgrade a decided decline to a generic pending; P0-1).
				apply({ type: EVENTS.CONFIRM_FAILED, decoded, ...identity });
				return;
			}
			await sleep(CASHFREE_CONFIRM_INTERVAL_MS);
		}
		if (my !== token) return;
		// Never confirmed inside the window. Do NOT claim a failure - fall to
		// unknown and let a status check (or the webhook) settle it.
		apply({
			type: EVENTS.CONFIRM_FAILED,
			decoded: { ok: false, code: "", message: "" },
			...identity,
		});
		await reconcileAfterFailure();
	}

	// ---- check status: the provider-truth read -----------------------------
	// The user-driven "Check payment status" button: apply whatever the gateway
	// says (pending stays pending, a decline becomes a decline, paid advances),
	// and count it toward the client-local support ceiling.
	async function checkStatus() {
		// The one action lock (P1-2). Without it the Check button stayed live
		// through its own round trip - an impatient customer fired N concurrent
		// provider-truth calls, each spending a gateway call against the per-account
		// cap, and a Check racing an Initiate produced two live actions. The lock
		// makes a check-vs-initiate burst resolve to exactly one action.
		const my = beginAction("checking");
		if (!my) return;
		try {
			let decoded;
			try {
				decoded = ingest(
					await deadlined(
						(signal) => api.checkSignupPaymentStatus({ signal }),
						fetchDeadlineMs,
						"check"
					)
				);
			} catch (e) {
				// A timed-out check says nothing about the money: route the offline
				// answer through the reducer (keeps any known state, re-enables Check).
				// It is NOT a check the customer got an answer to, so it does not count
				// toward the support ceiling.
				if (my !== token) return;
				absorb(offlineDecoded());
				return;
			}
			if (my !== token) return;
			absorb(decoded);
			// A rate limit asserts nothing about the money and is not a "check" that
			// counts toward the support ceiling - the customer never got an answer.
			if (decoded.code !== CODES.PAYMENT_CHECK_RATE_LIMITED) {
				state.value = noteStatusCheck(state.value);
				persistSupport(state.value);
			}
		} finally {
			endAction(my);
		}
	}

	// Check-on-failure: mandatory after a dead/dismissed checkout, a failed confirm,
	// a timed-out mutation, or a return from an external checkout. It routes EVERY
	// authoritative answer through the reducer (P0-1) - not just paid/rate-limit/
	// parked-money. The old code discarded everything else and kept the stale
	// failure framing over it, so an answer of "authorized; do not pay again"
	// (PAYMENT_AUTHORIZED_PENDING_CONFIRM), a reconnect, a terminal, or a pending
	// whose can_initiate flipped to false still rendered "Initiate payment again".
	// The reducer already knows how to fold each of those safely - a transport
	// failure becomes truth-unknown while KEEPING any known payment state; paid is
	// a floor; a decline renders as itself - so the one correct thing to do is give
	// it the answer. A "the checkout could not open" note survives as presentation
	// metadata (state.checkoutNote), never as a preserved code/state/capability.
	async function reconcileAfterFailure() {
		const my = token;
		let decoded;
		try {
			decoded = ingest(
				await deadlined(
					(signal) => api.checkSignupPaymentStatus({ signal }),
					fetchDeadlineMs,
					"check"
				)
			);
		} catch (e) {
			// The mandatory check itself timed out/died. Truth is UNKNOWN, never a
			// verdict: route the offline answer through the reducer (which keeps a
			// known payment state and offers the read-only check) and stop - no
			// recursion, no fabricated failure.
			if (my !== token) return;
			absorb(offlineDecoded());
			return;
		}
		if (my !== token) return;
		absorb(decoded);
	}

	// ---- return from an external checkout (P0-2) ---------------------------
	// The explicit, SAFE exit from checkout_open/confirming: a bfcache restore, a
	// top-level redirect return, or a tab regaining focus after a full-page
	// mandate. hydrate() deliberately refuses to leave checkout_open (a passive
	// read must never unseat a live sheet); this is the ONE path that does, and it
	// never assumes dismissal - it leaves the busy screen for a checkable UNKNOWN
	// and reconciles server truth.
	async function returnFromCheckout() {
		const v = state.value.value;
		if (v !== STATES.CHECKOUT_OPEN && v !== STATES.CONFIRMING) return { returned: false };
		cancelInFlight(); // the sheet is gone; abandon any client work it left
		apply({ type: EVENTS.RETURNED_FROM_CHECKOUT });
		await reconcileAfterFailure();
		return { returned: true };
	}

	// ---- "Start again" (P1-3): a server-truth-gated reset -------------------
	// Resets the machine to a fresh review ONLY when no recoverable payment can be
	// behind the current code (the reducer's canSafelyRestart decides); otherwise
	// it preserves the attempt and its status/reconnect/support affordances. The
	// caller uses `reset` to decide whether editing details is safe or the customer
	// should stay on their recovery card.
	function restart() {
		cancelInFlight();
		const after = apply({ type: EVENTS.RESTART });
		return { reset: after.value === STATES.REVIEW };
	}

	// ---- support-counter persistence (P2-5) --------------------------------
	function persistSupport(s) {
		if (s.attemptId == null && s.generation == null) return;
		try {
			const key = SUPPORT_KEY_PREFIX + counterKey(s);
			storage.set(key, String((s.supportChecks && s.supportChecks.checks) || 0));
		} catch (e) {
			/* storage may be unavailable/full - the counter simply falls back to memory */
		}
	}

	function restoreSupport() {
		const s = state.value;
		if (s.attemptId == null && s.generation == null) return;
		let raw = null;
		try {
			raw = storage.get(SUPPORT_KEY_PREFIX + counterKey(s));
		} catch (e) {
			return;
		}
		const n = Number(raw);
		const have = (s.supportChecks && s.supportChecks.checks) || 0;
		if (Number.isFinite(n) && n > have) {
			const counter = { key: counterKey(s), checks: n };
			state.value = {
				...s,
				supportChecks: counter,
				supportOffered: shouldOfferSupport(counter),
			};
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
				// Bounded like every other payment wait (P0-3): a sync_connection that
				// never settles times out into "not ready yet" and the loop moves on,
				// rather than hanging one iteration forever.
				r = await deadlined(() => api.syncConnection(), fetchDeadlineMs, "sync");
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
		returnFromCheckout,
		restart,
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
