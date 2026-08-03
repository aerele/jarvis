import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
	loadRazorpay,
	openCheckout,
	_resetRazorpayLoader,
	CHECKOUT_SUCCESS,
	CHECKOUT_DISMISSED,
} from "@/lib/useRazorpay";

// NOTE: Razorpay is stubbed everywhere in this file. Nothing here reaches
// checkout.razorpay.com and no payment is ever initiated - this environment has
// LIVE keys configured, so the real sheet must never open from a test.

/** Records the options it was constructed with and exposes the callbacks. */
function stubRazorpay() {
	const calls = [];
	const Ctor = vi.fn(function (opts) {
		const entry = { opts, opened: false };
		calls.push(entry);
		this.open = () => {
			entry.opened = true;
		};
	});
	window.Razorpay = Ctor;
	return calls;
}

const SUBSCRIPTION_HANDLES = {
	razorpay_key_id: "rzp_test_key",
	razorpay_subscription_id: "sub_123",
	amount_inr: 4999,
};
const ORDER_HANDLES = {
	razorpay_key_id: "rzp_test_key",
	razorpay_order_id: "order_123",
	amount_inr: 1234.5,
};

beforeEach(() => {
	_resetRazorpayLoader();
	delete window.Razorpay;
	// Drop only the tag under test, so the rest of the jsdom head is untouched.
	document.querySelectorAll("script#jv-razorpay-checkout").forEach((el) => el.remove());
});
afterEach(() => {
	delete window.Razorpay;
});

describe("loadRazorpay", () => {
	it("resolves immediately when Razorpay is already on window, injecting nothing", async () => {
		stubRazorpay();
		await loadRazorpay();
		expect(document.getElementById("jv-razorpay-checkout")).toBeNull();
	});

	it("injects the checkout script once and reuses the same tag on later calls", async () => {
		const first = loadRazorpay();
		const el = document.getElementById("jv-razorpay-checkout");
		expect(el).not.toBeNull();
		expect(el.src).toBe("https://checkout.razorpay.com/v1/checkout.js");

		// A second caller while the first is still in flight must not add a tag.
		const second = loadRazorpay();
		expect(document.querySelectorAll("script#jv-razorpay-checkout")).toHaveLength(1);

		stubRazorpay();
		el.dispatchEvent(new Event("load"));
		expect(await first).toBe(window.Razorpay);
		expect(await second).toBe(window.Razorpay);

		// Third call, after load: still exactly one tag.
		await loadRazorpay();
		expect(document.querySelectorAll("script#jv-razorpay-checkout")).toHaveLength(1);
	});

	it("lets a retry try again after a load failure", async () => {
		const failing = loadRazorpay();
		document.getElementById("jv-razorpay-checkout").dispatchEvent(new Event("error"));
		await expect(failing).rejects.toThrow(/Could not load the payment form/);

		// The cached rejection must be gone, so a retry injects a fresh tag.
		loadRazorpay();
		expect(document.getElementById("jv-razorpay-checkout")).not.toBeNull();
	});
});

describe("openCheckout", () => {
	it("resolves SUCCESS with the four signature fields when the handler fires", async () => {
		const calls = stubRazorpay();
		const p = openCheckout(ORDER_HANDLES, "Plan upgrade");
		await vi.waitFor(() => expect(calls).toHaveLength(1));

		calls[0].opts.handler({
			razorpay_payment_id: "pay_1",
			razorpay_order_id: "order_123",
			razorpay_signature: "sig_1",
		});

		const out = await p;
		expect(out.status).toBe(CHECKOUT_SUCCESS);
		expect(out.payload).toEqual({
			razorpay_payment_id: "pay_1",
			razorpay_order_id: "order_123",
			// Explicit null, never undefined: an absent key would reach the server
			// as a missing field and the signature check has nothing to compare.
			razorpay_subscription_id: null,
			razorpay_signature: "sig_1",
		});
	});

	it("resolves DISMISSED, and never rejects, when the sheet is closed", async () => {
		const calls = stubRazorpay();
		const p = openCheckout(ORDER_HANDLES, "Plan upgrade");
		await vi.waitFor(() => expect(calls).toHaveLength(1));

		calls[0].opts.modal.ondismiss();

		await expect(p).resolves.toEqual({ status: CHECKOUT_DISMISSED });
	});

	it("keeps SUCCESS when Razorpay also fires ondismiss after a payment", async () => {
		const calls = stubRazorpay();
		const p = openCheckout(ORDER_HANDLES, "Plan upgrade");
		await vi.waitFor(() => expect(calls).toHaveLength(1));

		// The real sheet closes itself after a successful payment, and some
		// versions run ondismiss on the way out. Success must win.
		calls[0].opts.handler({ razorpay_payment_id: "pay_1", razorpay_signature: "sig" });
		calls[0].opts.modal.ondismiss();

		expect((await p).status).toBe(CHECKOUT_SUCCESS);
	});

	it("keeps DISMISSED when a late handler fires after a dismissal", async () => {
		const calls = stubRazorpay();
		const p = openCheckout(ORDER_HANDLES, "Plan upgrade");
		await vi.waitFor(() => expect(calls).toHaveLength(1));

		calls[0].opts.modal.ondismiss();
		calls[0].opts.handler({ razorpay_payment_id: "pay_1", razorpay_signature: "sig" });

		expect((await p).status).toBe(CHECKOUT_DISMISSED);
	});

	it("uses subscription mode with NO amount or order_id for a mandate", async () => {
		const calls = stubRazorpay();
		openCheckout(SUBSCRIPTION_HANDLES, "Plan upgrade");
		await vi.waitFor(() => expect(calls).toHaveLength(1));

		const { opts } = calls[0];
		expect(opts.subscription_id).toBe("sub_123");
		// Regression guard: an order_id of undefined alongside a subscription made
		// Checkout charge a stray standalone payment.
		expect(opts).not.toHaveProperty("order_id");
		expect(opts).not.toHaveProperty("amount");
	});

	it("uses order mode with the amount in paise", async () => {
		const calls = stubRazorpay();
		openCheckout(ORDER_HANDLES, "Plan upgrade");
		await vi.waitFor(() => expect(calls).toHaveLength(1));

		const { opts } = calls[0];
		expect(opts.order_id).toBe("order_123");
		expect(opts.amount).toBe(123450);
		expect(opts.currency).toBe("INR");
		expect(opts).not.toHaveProperty("subscription_id");
		expect(opts.key).toBe("rzp_test_key");
		expect(opts.description).toBe("Plan upgrade");
	});

	it("opens the sheet", async () => {
		const calls = stubRazorpay();
		openCheckout(ORDER_HANDLES);
		await vi.waitFor(() => expect(calls[0].opened).toBe(true));
	});

	it("rejects when the sheet cannot be opened at all", async () => {
		window.Razorpay = vi.fn(function () {
			this.open = () => {
				throw new Error("blocked");
			};
		});
		await expect(openCheckout(ORDER_HANDLES)).rejects.toThrow("blocked");
	});
});

describe("openCheckout teardown (X1)", () => {
	it("aborting the signal asks Razorpay to close, which resolves a dismissal", async () => {
		const calls = [];
		// A stub whose .close() behaves like the real SDK: it fires ondismiss.
		window.Razorpay = vi.fn(function (opts) {
			const entry = { opts, closed: false };
			calls.push(entry);
			this.open = () => {};
			this.close = () => {
				entry.closed = true;
				opts.modal.ondismiss();
			};
		});
		const controller = new AbortController();
		const p = openCheckout(ORDER_HANDLES, "Plan upgrade", { signal: controller.signal });
		await vi.waitFor(() => expect(calls).toHaveLength(1));
		controller.abort();
		expect(calls[0].closed).toBe(true);
		await expect(p).resolves.toEqual({ status: CHECKOUT_DISMISSED });
	});

	it("an ALREADY-aborted signal closes the sheet immediately on open (D12)", async () => {
		// The real, delete-sensitive abort behavior: if the open deadline already
		// elapsed by the time the sheet opens, the `signal.aborted` branch must fire
		// onAbort synchronously and ask Razorpay to close - which settles a dismissal.
		// The old test here asserted only "does not throw" against a no-close stub,
		// which is ALSO true with the signal wiring deleted entirely (a vacuous pass).
		// This asserts close is actually invoked, so removing the wiring reddens it.
		const calls = [];
		window.Razorpay = vi.fn(function (opts) {
			const entry = { opts, closed: false };
			calls.push(entry);
			this.open = () => {};
			this.close = () => {
				entry.closed = true;
				opts.modal.ondismiss();
			};
		});
		const controller = new AbortController();
		controller.abort(); // the deadline elapsed before the sheet even opened
		const p = openCheckout(ORDER_HANDLES, "Plan upgrade", { signal: controller.signal });
		await vi.waitFor(() => expect(calls).toHaveLength(1));
		expect(calls[0].closed).toBe(true);
		await expect(p).resolves.toEqual({ status: CHECKOUT_DISMISSED });
	});

	it("a build with no close() does not throw on abort and leaves the sheet open", async () => {
		const calls = stubRazorpay(); // this stub instance has no .close
		const controller = new AbortController();
		const p = openCheckout(ORDER_HANDLES, "Plan upgrade", { signal: controller.signal });
		await vi.waitFor(() => expect(calls).toHaveLength(1));
		let settled = false;
		p.then(() => (settled = true));
		expect(() => controller.abort()).not.toThrow();
		await Promise.resolve();
		// A no-close build cannot be torn down, so abort must NOT settle the promise -
		// the sheet is still live; only a real dismiss resolves it.
		expect(settled).toBe(false);
		calls[0].opts.modal.ondismiss();
		await expect(p).resolves.toEqual({ status: CHECKOUT_DISMISSED });
	});
});
