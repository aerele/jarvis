/**
 * The onboarding wizard's checkout dispatcher, built ON the shipped billing
 * layer (C02-2) rather than beside it.
 *
 * `billingCheckout.classifyHandles` already knows every sheet the Account page
 * opens, and `useRazorpay` / `useCashfree` already open them - with a dismissal
 * signal, a one-shot latch, and the exact confirm payloads admin verifies. This
 * module reuses all of it and adds the ONE shape the billing page deliberately
 * refuses: a Cashfree autopay MANDATE.
 *
 * Why the wizard owns that one and the billing page does not: a Cashfree mandate
 * is a full-page `subscriptionsCheckout` redirect whose `return_url` admin
 * points at THIS wizard, and which `confirm_payment` can only confirm on the way
 * back. From a settings page that redirect would strand the customer, so
 * billingCheckout classifies `subscription_session_id` / `cashfree_subscription_id`
 * as UNSUPPORTED. Here it is a first-class journey - the wizard has always
 * handled it - so we branch before delegating and never inherit the refusal.
 */

import {
	classifyHandles,
	CHECKOUT_RAZORPAY,
	CHECKOUT_CASHFREE_ORDER,
	CHECKOUT_NONE,
} from "@/lib/billingCheckout";
import { openCheckout as openRazorpaySheet, CHECKOUT_DISMISSED } from "@/lib/useRazorpay";
import { openCashfreeCheckout as openCashfreeSheet } from "@/lib/useCashfree";

/** The wizard's own branch: a Cashfree autopay mandate (redirect journey). */
export const CHECKOUT_CASHFREE_MANDATE = "cashfree_mandate";

/**
 * Which checkout `handles` calls for, from the onboarding point of view.
 *
 * The mandate check runs FIRST, for the same reason billingCheckout tests it
 * first: a mandate carries no order id and would otherwise fall through to a
 * shape it is not. Everything the shipped classifier already resolves is
 * delegated verbatim, so the two cannot drift.
 */
export function classifyOnboardingHandles(handles) {
	const h = handles || {};
	if (h.subscription_session_id || h.cashfree_subscription_id) {
		return CHECKOUT_CASHFREE_MANDATE;
	}
	return classifyHandles(h);
}

/** Thrown for handles this build cannot render. Its message is customer-facing. */
export class UnrenderableCheckoutError extends Error {
	constructor(message) {
		super(message || "This payment method is not available here yet. Please contact support.");
		this.name = "UnrenderableCheckoutError";
	}
}

/**
 * Open the right sheet for `handles`, delegating to the shipped openers.
 *
 * Side effects are injected so this is unit-testable with the SDKs stubbed:
 *
 * @returns {Promise<{status: string, payload?: object, leavesPage?: boolean}>}
 *   For Razorpay/Cashfree orders, exactly what the shipped opener returns
 *   ({status: success|dismissed, payload}). For a Cashfree mandate,
 *   `{status, leavesPage: true}` - the browser is leaving for the gateway and
 *   the wizard resumes through its normal return path, so there is no in-page
 *   confirm to await.
 */
export async function openOnboardingCheckout(handles, opts = {}) {
	const {
		description = "Jarvis subscription",
		openRazorpay = openRazorpaySheet,
		openCashfree = openCashfreeSheet,
		openMandate = defaultOpenMandate,
	} = opts;

	const kind = classifyOnboardingHandles(handles);
	switch (kind) {
		case CHECKOUT_RAZORPAY: {
			const out = await openRazorpay(handles, description);
			return out;
		}
		case CHECKOUT_CASHFREE_ORDER: {
			const out = await openCashfree(handles);
			// Cashfree gives no dismissal signal, so its order flow always resolves
			// as a success that the server-side confirm then adjudicates - the flow
			// treats "never confirmed" as its own outcome.
			return { ...out, pollConfirm: true };
		}
		case CHECKOUT_CASHFREE_MANDATE: {
			const out = await openMandate(handles);
			return { ...out, leavesPage: true };
		}
		case CHECKOUT_NONE:
		default:
			// classifyHandles returns UNSUPPORTED or NONE for anything else. Both
			// mean "there is no sheet this build can open" - and reporting that as
			// success is exactly the silent free-upgrade bug billingCheckout exists
			// to prevent, so it is a loud refusal here too.
			throw new UnrenderableCheckoutError();
	}
}

// The mandate opener the wizard used inline (openCashfreeMandate): a
// subscriptionsCheckout redirect. Kept injectable so the spec never touches the
// real SDK. The real DOM-mounted implementation lives in the view, which owns
// the Cashfree SDK handle; this default exists only so a caller that does not
// pass one fails loudly rather than silently no-oping.
async function defaultOpenMandate() {
	throw new UnrenderableCheckoutError(
		"Auto-pay authorisation could not be started. Please contact support."
	);
}

export { CHECKOUT_DISMISSED };
