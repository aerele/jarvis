/**
 * The payment orchestrator: everything the reducer is forbidden to do.
 *
 * The reducer (paymentMachine) is a pure transition table. This is the place
 * where API calls happen, where the top-level navigation to the admin-hosted pay
 * page fires, and where the two long poll loops run - so all of that can be
 * cancelled, generation-fenced and browser-tested apart from the render.
 *
 * plan-09 WS7 (the admin-hosted checkout cutover). The bench opens NO gateway SDK
 * on its own origin. A payable answer carries a pay-page token plus the bench's
 * OWN attested origin, and the customer is TOP-LEVEL NAVIGATED to
 * `{origin}/jarvis-checkout#t=<token>` (navigateToPay). There is no in-page sheet
 * to dismiss, no client-side confirm, and NO FALLBACK: a token with no attested
 * origin, or a pre-cutover admin's raw handles with no token, fails the step
 * closed with honest copy (the reducer renders it) - nothing opens.
 *
 * The rules this file exists to hold, none of which fit in a pure reducer:
 *
 *   - **Reconcile after a return.** Coming back from the pay page (bfcache /
 *     history.back / refocus) is never assumed to be a verdict: the machine takes
 *     the explicit RETURNED_FROM_CHECKOUT exit and this file re-reads server truth.
 *   - **One in-flight generation.** A `cancelInFlight()` bumps a token; any answer
 *     that belongs to a superseded token is dropped. Plus a NEW token per action
 *     (P1-2), so a late answer from a finished-then-abandoned action is fenced even
 *     without an explicit cancel.
 *   - **One navigation, ever, per intent state.** navigateToPay applies
 *     NAVIGATED_TO_PAY (→ CHECKOUT_OPEN) BEFORE it navigates; CHECKOUT_OPEN is not a
 *     navigable source state, so a double-click cannot double-navigate. The action
 *     lock guards the round trip that precedes it.
 *   - **The SPA never sends an idempotency key.** The bench owns that receipt; so
 *     `initiate` posts no key, ever.
 *   - **Raw responses, decoded here.** Frappe-ui's `call()` throws on a 4xx and
 *     discards the body, but the whole contract lives in that body. The api layer
 *     injected here returns `{status, body, networkError}` untouched.
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
	canNavigateToPay,
	canSafelyRestart,
	initialState,
	payPageUrl,
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

const PROVISIONING_ATTEMPTS = 45; // ~45 × 2s ≈ 90s (was proceedAfterPay)
const PROVISIONING_INTERVAL_MS = 2000;

/**
 * @param {object} deps
 * @param {object} deps.api  raw-response endpoints (see api.js onboardingRaw.*)
 * @param {Function} deps.navigate  top-level navigation to the pay page. Called
 *   with `{url, attemptId}`; the DOM impl records the external-nav marker
 *   (attempt-stamped) BEFORE `window.location.assign(url)`. Injected so the flow
 *   is browser-testable without leaving the jsdom page.
 * @param {Function} [deps.sleep]
 * @param {Function} [deps.now]  ms clock (injected so cooldowns are testable)
 * @param {object} [deps.storage]  {get,set} for the non-secret attempt hint
 * @param {boolean} [deps.strict]  reducer strict mode (dev/test)
 */
export function createPaymentFlow(deps) {
	const {
		api,
		navigate = ({ url }) => {
			if (url && typeof window !== "undefined" && window.location)
				window.location.assign(url);
		},
		sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
		now = () => Date.now(),
		storage = memoryStorage(),
		strict = false,
		// PII-free transition sink (P2-3). Carries only the SHAPE of the move -
		// never email/company/address/token/payment-id. Default is a no-op.
		telemetry = () => {},
		// Wall-clock deadlines (P0-3). Every payment fetch is raced against
		// `fetchDeadlineMs`. A deadline is a CLIENT abort, never a server verdict -
		// every timeout reconciles server truth.
		fetchDeadlineMs = 30_000,
	} = deps;

	const state = ref(initialState());
	// The generation token: every mutating action captures it, and anything that
	// resumes after `cancelInFlight` compares against it and bails. A NEW token is
	// minted per action (P1-2), so a late answer from a finished-then-abandoned
	// action is fenced even without an explicit cancel.
	let token = 0;
	// The one incompatible-action lock (P1-2). submitReview / initiatePayment /
	// checkStatus / verifyAndContinue all take it before their first API call, so a
	// burst of clicks - or a check racing an initiate - produces exactly one live
	// action and, at most, one navigation. `lockToken` is the token that holds it.
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
		// and drop the flag those calls were holding. Every release path is fenced on
		// `my === token`, so the bump that invalidates the in-flight work also
		// invalidates its own release.
		actionLock = false;
		if (state.value.busy !== null) state.value = { ...state.value, busy: null };
	}

	// Race a payment fetch against a wall-clock deadline (P0-3). On timeout it
	// aborts the browser wait (via AbortSignal where the api honours one) and
	// rejects with `isTimeout`; it NEVER resolves to a fabricated answer, because a
	// client abort says nothing about what the server did - the caller reconciles
	// server truth after every timeout.
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
		// Every failure - rate limits included - goes through the ONE contract path.
		apply({ type: EVENTS.CONTRACT_STATE, decoded });
		return decoded.code;
	}

	// ---- top-level navigation to the admin-hosted pay page (WS7) ------------
	// The ONE place the customer leaves for checkout, and the one place that decides
	// whether they may: `canNavigateToPay` is the reducer's own NAVIGATED_TO_PAY
	// guard, so a refusal here and a refusal there cannot drift apart. If the machine
	// is not navigable (no attested token), nothing happens and the caller's state
	// renders as it stands (an honest hold, never a fallback sheet).
	function navigateToPay() {
		if (!canNavigateToPay(state.value)) {
			// Not navigable: release any busy flag the caller took so the honest-hold
			// card the customer is left on is usable.
			if (state.value.busy !== null) state.value = { ...state.value, busy: null };
			return false;
		}
		// Build the URL from the bench's OWN attested origin + the frozen path
		// constant + the token in the fragment - NEVER from a URL admin supplied.
		const url = payPageUrl(state.value);
		const attemptId = state.value.attemptId;
		// Move to CHECKOUT_OPEN FIRST: CHECKOUT_OPEN is not a navigable source, so a
		// second click (or a re-entrant call) cannot navigate again (P1-2, extended to
		// the navigation action). Only then hand off to the DOM navigation, which
		// stamps the external-nav marker before window.location.assign.
		apply({ type: EVENTS.NAVIGATED_TO_PAY });
		navigate({ url, attemptId });
		return true;
	}

	// ---- mount: server truth wins ------------------------------------------
	/**
	 * Reconcile on mount. Returns {paid, truthKnown, notStarted}:
	 *   paid === true   - an authoritative Active/paid answer
	 *   paid === false  - a definite not-paid answer (incl. day-one)
	 *   paid === null   - the control plane could not be reached; paid is UNKNOWN.
	 */
	async function hydrate() {
		// A FROZEN checkout_open - we navigated away to the pay page and the customer
		// is back (a bfcache restore, or a torn-down/remounted instance) - must not
		// trap them on "Taking you to the secure payment page…" forever. Take the
		// explicit, safe RETURNED_FROM_CHECKOUT exit so the read below reconciles
		// truth. (A fresh mount is never in checkout_open; only a return is.)
		if (state.value.value === STATES.CHECKOUT_OPEN) {
			apply({ type: EVENTS.RETURNED_FROM_CHECKOUT }); // now UNKNOWN; the read reconciles
		}
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
			// The mount read timed out or the network died. Truth is UNKNOWN. Never
			// claim not-paid from a client abort.
			if (my !== token)
				return { paid: null, truthKnown: false, notStarted: false, superseded: true };
			absorb(offlineDecoded());
			return { paid: null, truthKnown: false, notStarted: false };
		}
		if (my !== token)
			return { paid: null, truthKnown: false, notStarted: false, superseded: true };
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
	// One round trip: re-read, and if the signup is now payable (a navigable token),
	// go straight to the pay page on the same click.
	async function verifyAndContinue() {
		const my = beginAction("verifying");
		if (!my) return; // an incompatible action already holds the lock (P1-2)
		let navigated = false;
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
			// Payable is asked of the MACHINE, not of the answer: an answer the reducer
			// refused (no generation to attribute it to, a losing generation, an
			// unattested origin) is not navigable, and navigating off it would send the
			// customer to a dead token.
			if (canNavigateToPay(state.value)) {
				navigated = true;
				endAction(my); // navigation leaves the page; free the lock first
				navigateToPay();
			}
		} finally {
			if (!navigated) endAction(my);
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
				absorb(offlineDecoded());
				await reconcileAfterFailure();
				return;
			}
			if (my !== token) return;
			const code = absorb(decoded);
			if (!decoded.ok) return; // parked-money / duplicate / terminal - reducer rendered it
			if (code === CODES.SIGNUP_VERIFICATION_REQUIRED) return; // wait for the magic link
			navigateToPay(); // navigable answer → pay page; honest hold otherwise (no fallback)
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
				// No idempotency_key: the bench mints/reuses its own receipt.
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
			navigateToPay();
		} finally {
			endAction(my);
		}
	}

	// ---- check status: the provider-truth read -----------------------------
	async function checkStatus() {
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

	// Check-on-failure: mandatory after a timed-out mutation or a return from the
	// pay page. It routes EVERY authoritative answer through the reducer (P0-1) - a
	// transport failure becomes truth-unknown while KEEPING any known payment state;
	// paid is a floor; a decline renders as itself.
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
			// verdict: route the offline answer through the reducer and stop.
			if (my !== token) return;
			absorb(offlineDecoded());
			return;
		}
		if (my !== token) return;
		absorb(decoded);
	}

	// ---- return from the pay page (P0-2) -----------------------------------
	// The explicit, SAFE exit from checkout_open: a bfcache restore, a top-level
	// history.back(), or a tab regaining focus after the pay-page journey. hydrate()
	// takes the same exit for a frozen mount; this is the event-driven path the
	// view's pageshow/visibility handlers call. It never assumes dismissal - it
	// leaves the busy screen for a checkable UNKNOWN and reconciles server truth.
	async function returnFromCheckout() {
		if (state.value.value !== STATES.CHECKOUT_OPEN) return { returned: false };
		cancelInFlight(); // abandon any client work still in flight
		apply({ type: EVENTS.RETURNED_FROM_CHECKOUT });
		await reconcileAfterFailure();
		return { returned: true };
	}

	// ---- "Start again" (P1-3): a server-truth-gated reset -------------------
	function restart() {
		const willReset = canSafelyRestart(state.value);
		if (willReset) cancelInFlight();
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
			/* storage may be unavailable/full - the counter falls back to memory */
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
		// plan 02 names.
		if (state.value.value === STATES.PAID) {
			apply({ type: EVENTS.PROVISIONING_STARTED });
		}
		for (let i = 0; i < PROVISIONING_ATTEMPTS; i++) {
			if (my !== token) return { status: "superseded" };
			let r;
			try {
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

	// A wall-clock tick from the view lifts an expired cooldown so the Check button
	// re-enables itself without another request.
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
		navigateToPay,
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
