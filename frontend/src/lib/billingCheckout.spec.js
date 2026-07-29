import { describe, it, expect, vi } from "vitest";
import {
	payAndApply,
	pollUntilChanged,
	accountSnapshot,
	needsCheckout,
	unwrapData,
	PAY_APPLIED,
	PAY_DISMISSED,
	PAY_PENDING,
	PAY_NOTHING_TO_PAY,
	PAY_UNCONFIRMED,
	classifyHandles,
	CHECKOUT_RAZORPAY,
	CHECKOUT_CASHFREE_ORDER,
	CHECKOUT_NONE,
	CHECKOUT_UNSUPPORTED,
	UnsupportedCheckoutError,
} from "@/lib/billingCheckout";
import { CHECKOUT_SUCCESS, CHECKOUT_DISMISSED } from "@/lib/useRazorpay";

// Every side effect is injected, so no network, no timers and no Razorpay
// global are involved. No payment path is exercised.

const HANDLES = { razorpay_key_id: "k", razorpay_order_id: "order_1", amount_inr: 100 };
const PRO = { plan: { name: "pro" }, subscription_status: "Active" };
const STARTER = { plan: { name: "starter" }, subscription_status: "Active" };

/** Resolve instantly so a 30s poll window costs no real time. */
const noSleep = () => Promise.resolve();

function baseArgs(over = {}) {
	return {
		handles: HANDLES,
		description: "Plan upgrade",
		before: accountSnapshot(STARTER),
		openCheckout: vi.fn(async () => ({
			status: CHECKOUT_SUCCESS,
			payload: { razorpay_payment_id: "pay_1" },
		})),
		finishPayment: vi.fn(async () => ({})),
		getAccount: vi.fn(async () => PRO),
		sleep: noSleep,
		...over,
	};
}

describe("accountSnapshot", () => {
	it("changes when the plan changes", () => {
		expect(accountSnapshot(STARTER)).not.toBe(accountSnapshot(PRO));
	});

	it("is stable for an unchanged account and tolerates an empty one", () => {
		expect(accountSnapshot(PRO)).toBe(accountSnapshot({ ...PRO }));
		expect(() => accountSnapshot(null)).not.toThrow();
	});

	it("moves for flows that never touch plan, end or status", () => {
		// The desk page's narrower plan/end/status triple missed both of these,
		// which would have timed them out into a false "pending".
		const base = { plan: { name: "pro" }, subscription_status: "Active" };
		expect(accountSnapshot({ ...base, autorenew: 1 })).not.toBe(accountSnapshot(base));
		expect(accountSnapshot({ ...base, scheduled_plan: "starter" })).not.toBe(
			accountSnapshot(base)
		);
		expect(accountSnapshot({ ...base, has_mandate: 1 })).not.toBe(accountSnapshot(base));
	});
});

describe("needsCheckout / unwrapData", () => {
	it("needs checkout only when a gateway handle is present", () => {
		expect(needsCheckout({ razorpay_order_id: "o" })).toBe(true);
		expect(needsCheckout({ razorpay_subscription_id: "s" })).toBe(true);
		expect(needsCheckout({ scheduled: 1 })).toBe(false);
		expect(needsCheckout(null)).toBe(false);
	});

	it("peels an {ok,data} envelope but leaves a flat payload alone", () => {
		expect(unwrapData({ ok: true, data: { razorpay_order_id: "o" } })).toEqual({
			razorpay_order_id: "o",
		});
		const flat = { razorpay_order_id: "o" };
		expect(unwrapData(flat)).toBe(flat);
		expect(unwrapData(null)).toEqual({});
	});
});

describe("payAndApply", () => {
	it("returns APPLIED with the fresh account after a verified payment", async () => {
		const args = baseArgs();
		const out = await payAndApply(args);

		expect(out.status).toBe(PAY_APPLIED);
		expect(out.verified).toBe(true);
		expect(out.account).toBe(PRO);
		expect(args.finishPayment).toHaveBeenCalledWith({ razorpay_payment_id: "pay_1" });
	});

	it("returns DISMISSED without verifying or polling", async () => {
		const args = baseArgs({
			openCheckout: vi.fn(async () => ({ status: CHECKOUT_DISMISSED })),
		});
		const out = await payAndApply(args);

		expect(out).toEqual({ status: PAY_DISMISSED });
		// The whole point: a dismissal must cost nothing and say nothing.
		expect(args.finishPayment).not.toHaveBeenCalled();
		expect(args.getAccount).not.toHaveBeenCalled();
	});

	it("distinguishes a verification failure from a dismissal", async () => {
		const args = baseArgs({
			finishPayment: vi.fn(async () => {
				throw new Error("signature verify failed");
			}),
		});
		const out = await payAndApply(args);

		// Charged, and the webhook still applied it, so this is APPLIED - but
		// flagged unverified so the page can say so. Never DISMISSED.
		expect(out.status).toBe(PAY_APPLIED);
		expect(out.verified).toBe(false);
		expect(args.getAccount).toHaveBeenCalled();
	});

	it("returns PENDING when the plan never moves, even though money was taken", async () => {
		const args = baseArgs({ getAccount: vi.fn(async () => STARTER) });
		const out = await payAndApply(args);

		expect(out.status).toBe(PAY_PENDING);
		expect(out.verified).toBe(true);
		expect(out.account).toBeUndefined();
	});

	it("returns NOTHING_TO_PAY and never opens the sheet when there are no handles", async () => {
		// An annual downgrade is scheduled server-side with nothing to charge.
		const args = baseArgs({ handles: { scheduled: 1 } });
		const out = await payAndApply(args);

		expect(out).toEqual({ status: PAY_NOTHING_TO_PAY });
		expect(args.openCheckout).not.toHaveBeenCalled();
	});

	it("reports its phases in order so the overlay can follow", async () => {
		const phases = [];
		await payAndApply(baseArgs({ onPhase: (p) => phases.push(p) }));
		expect(phases).toEqual(["sheet", "verifying", "applying"]);
	});

	it("stops at the sheet phase on a dismissal", async () => {
		const phases = [];
		await payAndApply(
			baseArgs({
				openCheckout: vi.fn(async () => ({ status: CHECKOUT_DISMISSED })),
				onPhase: (p) => phases.push(p),
			})
		);
		expect(phases).toEqual(["sheet"]);
	});

	it("propagates a sheet that could not be opened, so the caller can show it", async () => {
		const args = baseArgs({
			openCheckout: vi.fn(async () => {
				throw new Error("blocked");
			}),
		});
		await expect(payAndApply(args)).rejects.toThrow("blocked");
	});
});

describe("pollUntilChanged", () => {
	it("reads once before sleeping when the change already landed", async () => {
		const sleep = vi.fn(noSleep);
		const getAccount = vi.fn(async () => PRO);
		const out = await pollUntilChanged({
			before: accountSnapshot(STARTER),
			getAccount,
			sleep,
		});

		expect(out).toBe(PRO);
		expect(getAccount).toHaveBeenCalledTimes(1);
		expect(sleep).not.toHaveBeenCalled();
	});

	it("keeps waiting through transient read failures", async () => {
		let n = 0;
		const getAccount = vi.fn(async () => {
			n += 1;
			if (n < 3) throw new Error("network");
			return PRO;
		});
		const out = await pollUntilChanged({
			before: accountSnapshot(STARTER),
			getAccount,
			sleep: noSleep,
		});

		expect(out).toBe(PRO);
		expect(getAccount).toHaveBeenCalledTimes(3);
	});

	it("gives up with null once the window elapses", async () => {
		const getAccount = vi.fn(async () => STARTER);
		const out = await pollUntilChanged({
			before: accountSnapshot(STARTER),
			getAccount,
			sleep: noSleep,
			intervalMs: 1000,
			timeoutMs: 3000,
		});

		expect(out).toBeNull();
		// One read up front, then one per interval until the window closes.
		expect(getAccount).toHaveBeenCalledTimes(4);
	});
});

// --- the Cashfree silent-success regression ---------------------------------
// A Cashfree customer clicking Renew used to fall through to "nothing to pay",
// and the page then reloaded reporting SUCCESS for a renewal that took no money
// and changed nothing. These pin that it cannot happen again.

const CASHFREE_ORDER = {
	payment_provider: "cashfree",
	cashfree_order_id: "cf_order_1",
	payment_session_id: "sess_1",
	amount_inr: 100,
};
const CASHFREE_MANDATE = {
	payment_provider: "cashfree",
	cashfree_subscription_id: "cf_sub_1",
	subscription_session_id: "sub_sess_1",
};

describe("classifyHandles", () => {
	it("recognises a razorpay order", () => {
		expect(classifyHandles(HANDLES)).toBe(CHECKOUT_RAZORPAY);
	});

	it("recognises a cashfree order", () => {
		expect(classifyHandles(CASHFREE_ORDER)).toBe(CHECKOUT_CASHFREE_ORDER);
	});

	it("refuses a cashfree mandate rather than rendering the wrong sheet", () => {
		expect(classifyHandles(CASHFREE_MANDATE)).toBe(CHECKOUT_UNSUPPORTED);
	});

	it("refuses an unknown gateway that still minted something", () => {
		expect(classifyHandles({ payment_provider: "stripe", stripe_pi: "pi_1" })).toBe(
			CHECKOUT_UNSUPPORTED
		);
	});

	it("only calls it nothing-to-pay when there are no handles at all", () => {
		expect(classifyHandles({})).toBe(CHECKOUT_NONE);
		expect(classifyHandles(null)).toBe(CHECKOUT_NONE);
	});
});

describe("needsCheckout", () => {
	it("is true for a cashfree order (the bug: it used to be false)", () => {
		expect(needsCheckout(CASHFREE_ORDER)).toBe(true);
	});

	it("is true for an unsupported gateway object, so it can be refused not skipped", () => {
		expect(needsCheckout(CASHFREE_MANDATE)).toBe(true);
	});
});

describe("payAndApply across gateways", () => {
	it("opens a sheet for a cashfree order instead of reporting nothing to pay", async () => {
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_SUCCESS, payload: {} }));
		const out = await payAndApply({
			handles: CASHFREE_ORDER,
			description: "Renew",
			before: accountSnapshot(PRO),
			openCheckout,
			finishPayment: vi.fn(async () => ({})),
			getAccount: vi.fn(async () => STARTER),
			sleep: async () => {},
		});
		expect(openCheckout).toHaveBeenCalled();
		expect(out.status).not.toBe(PAY_NOTHING_TO_PAY);
		// and it is told WHICH sheet, rather than the opener having to guess
		expect(openCheckout.mock.calls[0][2]).toBe(CHECKOUT_CASHFREE_ORDER);
	});

	it("refuses an unsupported gateway loudly, and never opens a sheet", async () => {
		const openCheckout = vi.fn();
		await expect(
			payAndApply({
				handles: CASHFREE_MANDATE,
				description: "Renew",
				before: accountSnapshot(PRO),
				openCheckout,
				finishPayment: vi.fn(),
				getAccount: vi.fn(),
			})
		).rejects.toBeInstanceOf(UnsupportedCheckoutError);
		expect(openCheckout).not.toHaveBeenCalled();
	});

	it("reports UNCONFIRMED, not PENDING, when cashfree could not be confirmed", async () => {
		// Cashfree gives no trustworthy dismissal signal, so an unconfirmed
		// settle must not claim the customer paid.
		const out = await payAndApply({
			handles: CASHFREE_ORDER,
			description: "Renew",
			before: accountSnapshot(PRO),
			openCheckout: async () => ({ status: CHECKOUT_SUCCESS, payload: {} }),
			finishPayment: vi.fn(async () => {
				throw new Error("confirm failed");
			}),
			getAccount: vi.fn(async () => PRO),
			sleep: async () => {},
			timeoutMs: 0,
		});
		expect(out.status).toBe(PAY_UNCONFIRMED);
	});

	it("still reports PENDING for razorpay, which did reach its success handler", async () => {
		const out = await payAndApply({
			handles: HANDLES,
			description: "Renew",
			before: accountSnapshot(PRO),
			openCheckout: async () => ({ status: CHECKOUT_SUCCESS, payload: {} }),
			finishPayment: vi.fn(async () => {
				throw new Error("confirm failed");
			}),
			getAccount: vi.fn(async () => PRO),
			sleep: async () => {},
			timeoutMs: 0,
		});
		expect(out.status).toBe(PAY_PENDING);
	});
});
