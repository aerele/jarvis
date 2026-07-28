/**
 * The pay-then-apply half of every billing action (upgrade, downgrade, renew).
 *
 * Preview and confirm copy differ per action and live in the page. Everything
 * from "the server handed us gateway handles" onwards is identical, so it lives
 * here once - and because every side effect is injected, it is unit-testable
 * with the Razorpay global stubbed.
 */

import { CHECKOUT_DISMISSED } from "@/lib/useRazorpay";

/** The plan now reflects the change. */
export const PAY_APPLIED = "applied";
/** The customer closed the sheet. The page must look exactly as it did. */
export const PAY_DISMISSED = "dismissed";
/** Money moved, but the plan had not flipped before we stopped waiting. */
export const PAY_PENDING = "pending";
/** The server settled it without a payment (an annual downgrade schedules). */
export const PAY_NOTHING_TO_PAY = "nothing_to_pay";

const POLL_INTERVAL_MS = 3000;
const POLL_TIMEOUT_MS = 30000;

/**
 * The fields whose change means "the billing action landed".
 *
 * Wider than the desk page's plan/end/status triple, because that triple never
 * moves for two flows the SPA now hosts: re-arming autopay only flips
 * `autorenew`/`has_mandate`, and a scheduled monthly downgrade only sets
 * `scheduled_plan`. Polling on the narrow triple would have timed those out
 * into a false "pending".
 */
export function accountSnapshot(a) {
	const acc = a || {};
	return JSON.stringify({
		plan: (acc.plan || {}).name || "",
		end: acc.current_period_end || "",
		status: acc.subscription_status || "",
		autorenew: !!acc.autorenew,
		mandate: !!acc.has_mandate,
		cancelling: !!acc.cancel_at_period_end,
		scheduled: acc.scheduled_plan || "",
	});
}

/**
 * Peel the {ok, data} envelope off a billing response, if there is one.
 *
 * admin_client._do_post already unwraps "data" before the value reaches the
 * bench, so these endpoints return flat dicts and this is normally a no-op.
 * The retired desk page unwrapped defensively at all six of its call sites
 * anyway; keeping that here costs one type check and means a re-wrapped
 * response from the admin plane cannot silently produce empty handles.
 */
export function unwrapData(res) {
	if (res && typeof res === "object" && res.data && typeof res.data === "object") {
		return res.data;
	}
	return res || {};
}

/** True when the server expects us to open a payment sheet at all. */
export function needsCheckout(handles) {
	const h = handles || {};
	return !!(h.razorpay_order_id || h.razorpay_subscription_id);
}

/**
 * Open Checkout for `handles`, verify, then wait for the plan to reflect it.
 *
 * @returns {Promise<{status: string, account?: object, verified?: boolean}>}
 *   `verified` is false when the signature POST failed but the customer was
 *   still charged - a real state, distinct from a dismissal, and one the
 *   gateway webhook usually resolves on its own within the poll window.
 */
export async function payAndApply({
	handles,
	description,
	before,
	openCheckout,
	finishPayment,
	getAccount,
	onPhase = () => {},
	sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
	intervalMs = POLL_INTERVAL_MS,
	timeoutMs = POLL_TIMEOUT_MS,
}) {
	if (!needsCheckout(handles)) {
		// Nothing was charged, but the server did change something (it scheduled
		// the switch), so the caller still has to re-read rather than assume.
		return { status: PAY_NOTHING_TO_PAY };
	}

	onPhase("sheet");
	const out = await openCheckout(handles, description);
	if (out.status === CHECKOUT_DISMISSED) {
		// Deliberately the earliest possible return: no verify, no poll, no
		// toast. Backing out of a payment is a decision, not an error.
		return { status: PAY_DISMISSED };
	}

	onPhase("verifying");
	let verified = true;
	try {
		await finishPayment(out.payload);
	} catch (e) {
		// Do NOT surface this as the outcome. The payment itself succeeded at the
		// gateway; only our confirm round-trip failed, and the gateway webhook
		// applies the same change server-side. Fall through to the poll, which is
		// what turns "probably fine" into an observed fact.
		verified = false;
	}

	onPhase("applying");
	const account = await pollUntilChanged({
		before,
		getAccount,
		sleep,
		intervalMs,
		timeoutMs,
	});
	return account
		? { status: PAY_APPLIED, account, verified }
		: { status: PAY_PENDING, verified };
}

/**
 * Re-read the account until its snapshot differs from `before`.
 * Resolves with the changed account, or null once `timeoutMs` has elapsed.
 */
export async function pollUntilChanged({
	before,
	getAccount,
	sleep,
	intervalMs = POLL_INTERVAL_MS,
	timeoutMs = POLL_TIMEOUT_MS,
}) {
	let waited = 0;
	// Read first, sleep second: a confirmed payment has usually already landed
	// by the time finish_payment returns, and a leading sleep would make the
	// common case feel three seconds slower than it is.
	for (;;) {
		try {
			const a = await getAccount();
			if (accountSnapshot(a) !== before) return a;
		} catch (e) {
			// A transient read failure is not a verdict. Keep waiting; the
			// timeout below is the only thing that ends this loop unhappily.
		}
		if (waited >= timeoutMs) return null;
		await sleep(intervalMs);
		waited += intervalMs;
	}
}
