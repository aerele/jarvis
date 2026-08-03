/**
 * Razorpay Checkout, as a promise.
 *
 * Ports the behaviour of the retired desk page's openCheckout()
 * (jarvis/page/jarvis_account/jarvis_account.js) without its jQuery or its
 * frappe.require() script loader, and adds the one thing the desk page never
 * had: a dismissal signal. The desk page passed only a `handler`, so a customer
 * who closed the sheet left the page wedged behind an overlay with no way back.
 * Here the sheet always settles, exactly once, into a value the caller can
 * branch on.
 */

const CHECKOUT_SRC = "https://checkout.razorpay.com/v1/checkout.js";
const SCRIPT_ID = "jv-razorpay-checkout";

/** The customer paid and Razorpay handed back a signature to verify. */
export const CHECKOUT_SUCCESS = "success";
/** The customer closed the sheet. Not a failure, and never an error toast. */
export const CHECKOUT_DISMISSED = "dismissed";

// Module-level so the second call reuses the first call's <script>, and two
// concurrent callers share one in-flight load instead of racing two tags in.
let loadPromise = null;

/**
 * Inject checkout.js on first use and resolve with the Razorpay constructor.
 * Later calls resolve from cache without touching the DOM.
 */
export function loadRazorpay() {
	// Checked before loadPromise: the script may have been injected by something
	// else entirely (an older desk tab, a gateway return page), in which case
	// there is nothing to wait for and no "load" event left to catch.
	if (typeof window !== "undefined" && window.Razorpay) {
		return Promise.resolve(window.Razorpay);
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
			if (window.Razorpay) resolve(window.Razorpay);
			else reject(new Error("Payment form loaded but did not initialise."));
		});
		el.addEventListener("error", () => {
			// Drop the cached rejection so a retry (offline, blocked, flaky CDN)
			// gets a real second attempt instead of replaying the failure forever.
			loadPromise = null;
			el.remove();
			reject(new Error("Could not load the payment form. Check your connection."));
		});
		if (fresh) document.head.appendChild(el);
	});
	return loadPromise;
}

/**
 * Open Checkout for one set of gateway handles.
 *
 * Resolves with `{ status: CHECKOUT_SUCCESS, payload }` or
 * `{ status: CHECKOUT_DISMISSED }`. Rejects only when the sheet could not be
 * opened at all (script blocked, malformed handles) - never for a dismissal.
 *
 * @param {object} handles  start_upgrade / start_downgrade / renew response
 * @param {string} description  the line the customer sees in the sheet
 */
export async function openCheckout(handles, description = "Subscription payment", opts = {}) {
	const Razorpay = await loadRazorpay();
	// A best-effort teardown signal (X1): the onboarding orchestrator aborts it when
	// the open deadline elapses, so we ask Razorpay to close the modal. Never relied
	// on - Checkout's own `ondismiss` then settles this promise as a dismissal, and
	// if the SDK build has no `.close()` the caller's machine-level veto still
	// protects. Harmless for the billing page, which passes no signal.
	const signal = opts && opts.signal;
	return new Promise((resolve, reject) => {
		// Razorpay may fire BOTH callbacks on a successful payment: `handler`
		// runs first, then the sheet closes and some versions also run
		// `ondismiss`. A one-shot latch is what keeps a completed payment from
		// being downgraded to "dismissed" a tick later. It also covers the
		// mirror case (dismiss then a late handler) and any double-open.
		let settled = false;
		const settle = (fn, value) => {
			if (settled) return;
			settled = true;
			fn(value);
		};

		const opts = {
			key: handles.razorpay_key_id,
			name: "Jarvis",
			description,
			handler: (res) =>
				settle(resolve, {
					status: CHECKOUT_SUCCESS,
					// Exactly the four fields finish_payment verifies. Nulls, not
					// undefined: the desk page learned that an undefined order_id
					// reaches the server as a missing key and the signature check
					// then has nothing to compare.
					payload: {
						razorpay_payment_id: res.razorpay_payment_id,
						razorpay_order_id: res.razorpay_order_id || null,
						razorpay_subscription_id: res.razorpay_subscription_id || null,
						razorpay_signature: res.razorpay_signature,
					},
				}),
			modal: {
				ondismiss: () => settle(resolve, { status: CHECKOUT_DISMISSED }),
			},
		};

		if (handles.razorpay_subscription_id) {
			// Recurring (Monthly): subscription-mode Checkout collects the mandate
			// and the prorated amount rides on the Subscription as an upfront
			// add-on. amount/order_id must NOT be set here - the desk page found
			// that an order_id of undefined made Checkout charge a stray
			// standalone payment on top of the subscription.
			opts.subscription_id = handles.razorpay_subscription_id;
		} else {
			opts.order_id = handles.razorpay_order_id;
			opts.amount = Math.round((handles.amount_inr || 0) * 100);
			opts.currency = "INR";
		}

		try {
			const rzp = new Razorpay(opts);
			rzp.open();
			// Wire the teardown AFTER a successful open, so an abort tries to close the
			// live modal (which triggers `ondismiss` -> a dismissal settle). Guarded:
			// a missing `.close()` or an already-settled promise is a silent no-op.
			if (signal) {
				const onAbort = () => {
					if (settled) return;
					try {
						if (typeof rzp.close === "function") rzp.close();
					} catch (e) {
						/* SDK without a programmatic close - the caller's veto still protects */
					}
				};
				if (signal.aborted) onAbort();
				else signal.addEventListener("abort", onAbort, { once: true });
			}
		} catch (e) {
			settle(reject, e);
		}
	});
}

/** Test seam: forget the cached script promise. Not used by app code. */
export function _resetRazorpayLoader() {
	loadPromise = null;
}
