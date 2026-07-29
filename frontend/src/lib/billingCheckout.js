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
/**
 * The gateway never confirmed, and we cannot tell paid from abandoned.
 *
 * Cashfree only: its sheet gives no dismissal signal, so a customer who closed
 * it and a customer whose payment is still settling look identical from the
 * browser. Distinct from PAY_PENDING, which means "you definitely paid" - the
 * page must not tell someone who backed out that their payment was received.
 */
export const PAY_UNCONFIRMED = "unconfirmed";

const POLL_INTERVAL_MS = 3000;
const POLL_TIMEOUT_MS = 30000;

// ---- which gateway, in which shape -----------------------------------------
// The bench never learns a tenant's gateway up front: get_account_summary
// carries no provider field and admin picks per subscription, so the
// discriminator only ever arrives in the start_* / renew response. This is the
// single point where the SPA decides what it is looking at.

/** Razorpay Checkout: a one-shot order or a mandate authorisation. */
export const CHECKOUT_RAZORPAY = "razorpay";
/** Cashfree Checkout: a one-shot order, confirmed server-side by order id. */
export const CHECKOUT_CASHFREE_ORDER = "cashfree_order";
/** No gateway object at all - the server settled it (an annual switch). */
export const CHECKOUT_NONE = "none";
/** Real gateway handles this build cannot render. Never "nothing to pay". */
export const CHECKOUT_UNSUPPORTED = "unsupported";

/** Thrown for CHECKOUT_UNSUPPORTED. Its message is customer-facing copy. */
export class UnsupportedCheckoutError extends Error {
	constructor(message) {
		super(message);
		this.name = "UnsupportedCheckoutError";
	}
}

// Deliberately echoes admin's own refusal wording for the sibling flows it
// already blocks on Cashfree (CashfreeReauthorizeUnsupported and friends), so a
// customer who meets this limit twice reads the same sentence both times.
export const UNSUPPORTED_CHECKOUT_MESSAGE =
	"This change is not available on Cashfree subscriptions yet. " +
	"Contact support and we will make the change for you.";

/**
 * Which checkout, if any, `handles` calls for.
 *
 * The order of these checks is load-bearing. A Cashfree autopay mandate
 * (cashfree_subscription_id / subscription_session_id) is tested BEFORE the
 * order shape because it carries no order id and needs a different SDK call
 * entirely: subscriptionsCheckout, a full-page redirect whose return_url admin
 * points at the ONBOARDING wizard, and which admin's confirm_payment has no
 * route for. Rendering it here would take an authorisation charge and then
 * apply nothing, so it is refused rather than attempted.
 */
export function classifyHandles(handles) {
	const h = handles || {};
	if (h.razorpay_order_id || h.razorpay_subscription_id) return CHECKOUT_RAZORPAY;
	if (h.cashfree_subscription_id || h.subscription_session_id) return CHECKOUT_UNSUPPORTED;
	if (h.cashfree_order_id || h.payment_session_id) return CHECKOUT_CASHFREE_ORDER;
	// A discriminator naming a gateway whose handles we did not recognise. The
	// server minted SOMETHING; calling that "nothing to pay" is exactly how a
	// Cashfree customer used to be shown a silent success for an upgrade that
	// never happened. Anything non-Razorpay with no usable handle fails here.
	const provider = String(h.payment_provider || h.provider || "")
		.trim()
		.toLowerCase();
	if (provider && provider !== "razorpay") return CHECKOUT_UNSUPPORTED;
	return CHECKOUT_NONE;
}

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

/**
 * True when the server expects us to open a payment sheet at all.
 *
 * Derived from classifyHandles rather than re-listing field names, because the
 * two drifting apart IS the bug this replaced: this predicate used to test the
 * razorpay fields alone, so a Cashfree order answered "no sheet needed",
 * payAndApply returned "nothing to pay", and the page reloaded reporting
 * SUCCESS for a renewal that had taken no money and changed nothing.
 *
 * UNSUPPORTED deliberately answers true. There is a real gateway object; we
 * simply cannot render it, and that has to surface as a refusal rather than as
 * a free upgrade.
 */
export function needsCheckout(handles) {
	return classifyHandles(handles) !== CHECKOUT_NONE;
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
	const kind = classifyHandles(handles);
	if (kind === CHECKOUT_NONE) {
		// Nothing was charged, but the server did change something (it scheduled
		// the switch), so the caller still has to re-read rather than assume.
		return { status: PAY_NOTHING_TO_PAY };
	}
	if (kind === CHECKOUT_UNSUPPORTED) {
		// A real gateway object we cannot render. Refusing loudly is the whole
		// point: the previous behaviour reported success and took no money.
		throw new UnsupportedCheckoutError(UNSUPPORTED_CHECKOUT_MESSAGE);
	}

	onPhase("sheet");
	// `kind` is passed so the caller opens the RIGHT sheet. It is not inferred
	// inside the opener, so a new gateway cannot be silently mishandled by an
	// opener that only knows one.
	const out = await openCheckout(handles, description, kind);
	if (out.status === CHECKOUT_DISMISSED) {
		// Deliberately the earliest possible return: no verify, no poll, no
		// toast. Backing out of a payment is a decision, not an error.
		//
		// Only Razorpay can reach this. Cashfree gives no trustworthy dismissal
		// signal (see useCashfree), so its "customer closed the sheet" case is
		// indistinguishable from "paid but we have not seen it yet" and resolves
		// through PAY_UNCONFIRMED below instead of being guessed at here.
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
	if (account) return { status: PAY_APPLIED, account, verified };
	// Nothing landed inside the window. What that MEANS depends on the gateway,
	// and conflating the two would put words in the customer's mouth about their
	// own money.
	//
	// Razorpay reached its success handler, so a payment definitely happened and
	// only the apply is lagging: PAY_PENDING ("you paid, it is on its way").
	//
	// Cashfree settles its sheet the same way whether the customer paid, closed
	// it, or hit a card error, and the server-side confirm is the only verdict.
	// If that confirm did not succeed AND nothing changed, we genuinely do not
	// know whether money moved, so it reports PAY_UNCONFIRMED rather than
	// claiming a payment we cannot evidence.
	if (kind === CHECKOUT_CASHFREE_ORDER && !verified) {
		return { status: PAY_UNCONFIRMED, verified };
	}
	return { status: PAY_PENDING, verified };
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
