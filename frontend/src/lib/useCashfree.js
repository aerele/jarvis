/**
 * Cashfree Checkout, as a promise, with the same contract as useRazorpay.
 *
 * Ports the onboarding wizard's openCashfreeCheckout / confirmCashfree pair
 * (views/OnboardingView.vue) so the billing page charges a Cashfree customer
 * exactly the way the wizard already does. Nothing here is new gateway
 * behaviour; it is the wizard's flow with the wizard's state machine removed.
 *
 * Two things differ from Razorpay and both are forced by the gateway:
 *
 *   - There is NO client-side signature. The browser only learns an order id,
 *     and confirmation is a server-side fetch of the real order status from
 *     Cashfree (admin's _confirm_cashfree). A forged success cannot activate.
 *   - There is NO trustworthy dismissal signal. cf.checkout() settles the same
 *     way whether the customer paid, closed the sheet, or hit a card error, and
 *     a payment can still land after the modal goes away. So this module never
 *     reports a dismissal; the server-side confirm is the only verdict, and
 *     billingCheckout treats "never confirmed" as its own outcome rather than
 *     claiming money was received.
 */

import { CHECKOUT_SUCCESS } from "@/lib/useRazorpay";

const CHECKOUT_SRC = "https://sdk.cashfree.com/js/v3/cashfree.js";
const SCRIPT_ID = "jv-cashfree-checkout";

// Module-level for the same reason as useRazorpay's: two concurrent callers
// share one in-flight load instead of racing two tags into <head>.
let loadPromise = null;

/**
 * Inject the v3 SDK on first use and resolve with the `Cashfree` factory.
 * Later calls resolve from cache without touching the DOM.
 */
export function loadCashfree() {
	// Checked before loadPromise: another surface (the onboarding wizard in a
	// previous route, a gateway return page) may already have injected it, and
	// then there is no "load" event left to catch.
	if (typeof window !== "undefined" && window.Cashfree) {
		return Promise.resolve(window.Cashfree);
	}
	if (loadPromise) return loadPromise;

	loadPromise = new Promise((resolve, reject) => {
		let el = document.getElementById(SCRIPT_ID);
		const fresh = !el;
		if (fresh) {
			el = document.createElement("script");
			el.id = SCRIPT_ID;
			el.src = CHECKOUT_SRC;
			el.async = true;
		}
		el.addEventListener("load", () => {
			if (window.Cashfree) resolve(window.Cashfree);
			else reject(new Error("Payment form loaded but did not initialise."));
		});
		el.addEventListener("error", () => {
			// Drop the cached rejection so a retry gets a real second attempt.
			loadPromise = null;
			el.remove();
			reject(new Error("Could not load the payment form. Check your connection."));
		});
		if (fresh) document.head.appendChild(el);
	});
	return loadPromise;
}

/**
 * Open Cashfree Checkout for one one-shot order.
 *
 * Resolves with `{ status: CHECKOUT_SUCCESS, payload }` where `payload` is
 * exactly what jarvis.onboarding.finish_payment forwards to admin's
 * confirm_payment for this gateway. Rejects only when the sheet could not be
 * opened at all.
 *
 * ONE-SHOT ORDERS ONLY. A Cashfree autopay mandate carries
 * subscription_session_id and needs subscriptionsCheckout(), which is a
 * full-page redirect whose return_url admin points at the ONBOARDING wizard,
 * and which has no confirm route through confirm_payment at all. Handing a
 * mandate to this function would open the wrong SDK call, so billingCheckout
 * classifies that shape as unsupported and never gets here.
 *
 * @param {object} handles  start_upgrade / renew response for a Cashfree sub
 */
export async function openCashfreeCheckout(handles) {
	const Cashfree = await loadCashfree();
	// The env decides which Cashfree backend the sheet talks to. Default to
	// sandbox on anything unrecognised: a sandbox sheet against production
	// handles simply fails, while the reverse would take real money on a
	// mis-typed value.
	const cf = Cashfree({
		mode: handles.cashfree_env === "production" ? "production" : "sandbox",
	});

	try {
		// _modal, not a redirect: the SPA stays mounted, so the page can confirm
		// and reconcile in place the way the Razorpay flow does.
		await cf.checkout({
			paymentSessionId: handles.payment_session_id,
			redirectTarget: "_modal",
		});
	} catch (e) {
		// Deliberately swallowed. A modal error is NOT a verdict on the payment:
		// the customer may have paid and then hit a rendering hiccup, and the
		// only account of the payment we trust is the server-side confirm below.
		// The wizard makes the same call for the same reason.
	}

	return {
		status: CHECKOUT_SUCCESS,
		payload: {
			// The discriminator admin's confirm_payment branches on. Without it
			// the request falls into the Razorpay branch and fails signature
			// verification on a payment that has no signature.
			provider: "cashfree",
			cashfree_order_id: handles.cashfree_order_id,
		},
	};
}

/** Test seam: forget the cached script promise. Not used by app code. */
export function _resetCashfreeLoader() {
	loadPromise = null;
}
