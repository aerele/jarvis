// The wizard opens the SAME sheets the Account/billing page opens. This file
// asserts the reuse itself: the shipped classifier and the shipped openers do
// the work, and the wizard adds exactly ONE branch the billing page does not
// have - the Cashfree autopay mandate, which the billing page refuses (its
// return_url points at this wizard, and confirm_payment has no route for it
// from a settings page) and the wizard has always supported.

import { describe, test, expect, vi, beforeEach } from "vitest";

import { CHECKOUT_RAZORPAY, CHECKOUT_CASHFREE_ORDER } from "@/lib/billingCheckout";
import { CHECKOUT_SUCCESS, CHECKOUT_DISMISSED } from "@/lib/useRazorpay";
import {
	CHECKOUT_CASHFREE_MANDATE,
	classifyOnboardingHandles,
	openOnboardingCheckout,
} from "@/onboarding/onboardingCheckout";

describe("classifyOnboardingHandles", () => {
	test("delegates the shapes the shipped classifier already knows", () => {
		expect(classifyOnboardingHandles({ razorpay_order_id: "o" })).toBe(CHECKOUT_RAZORPAY);
		expect(classifyOnboardingHandles({ razorpay_subscription_id: "s" })).toBe(
			CHECKOUT_RAZORPAY
		);
		expect(classifyOnboardingHandles({ payment_session_id: "p" })).toBe(
			CHECKOUT_CASHFREE_ORDER
		);
	});

	test("the Cashfree mandate is the wizard's own branch, not 'unsupported'", () => {
		// billingCheckout classifies this as UNSUPPORTED on purpose. Inheriting
		// that verdict here would tell every Cashfree autopay customer their
		// signup cannot be paid for - on the one surface that can pay for it.
		expect(classifyOnboardingHandles({ subscription_session_id: "sess" })).toBe(
			CHECKOUT_CASHFREE_MANDATE
		);
	});

	test("the mandate is the SESSION id - the subscription id alone opens nothing", () => {
		// openCashfreeMandate is handed `subsSessionId: handles.subscription_session_id`
		// and nothing else, so a set without one cannot raise a mandate however
		// Cashfree-ish it looks. Claiming the mandate shape on `cashfree_subscription_id`
		// promised a sheet that could not open - and, because the sniff runs first,
		// let that stale rider outrank a LIVE order sitting beside it.
		expect(classifyOnboardingHandles({ cashfree_subscription_id: "jrvs_1" })).not.toBe(
			CHECKOUT_CASHFREE_MANDATE
		);
		// A live order beside the rider is classified as the order it is.
		expect(
			classifyOnboardingHandles({
				cashfree_subscription_id: "jrvs_1",
				razorpay_order_id: "o",
			})
		).toBe(CHECKOUT_RAZORPAY);
		expect(
			classifyOnboardingHandles({
				cashfree_subscription_id: "jrvs_1",
				payment_session_id: "p",
			})
		).toBe(CHECKOUT_CASHFREE_ORDER);
		// ...and a real mandate is untouched: the session id still decides, first.
		expect(
			classifyOnboardingHandles({
				cashfree_subscription_id: "jrvs_1",
				subscription_session_id: "sess",
			})
		).toBe(CHECKOUT_CASHFREE_MANDATE);
	});

	test("no handles at all is not a checkout", () => {
		expect(classifyOnboardingHandles({})).not.toBe(CHECKOUT_RAZORPAY);
		expect(classifyOnboardingHandles(null)).not.toBe(CHECKOUT_RAZORPAY);
	});
});

describe("openOnboardingCheckout", () => {
	let openRazorpay;
	let openCashfree;
	let openMandate;

	beforeEach(() => {
		openRazorpay = vi.fn(async () => ({
			status: CHECKOUT_SUCCESS,
			payload: { razorpay_payment_id: "p" },
		}));
		openCashfree = vi.fn(async () => ({
			status: CHECKOUT_SUCCESS,
			payload: { provider: "cashfree" },
		}));
		openMandate = vi.fn(async () => ({ status: "redirected" }));
	});

	test("a Razorpay order goes through the shipped Razorpay opener", async () => {
		const out = await openOnboardingCheckout(
			{ razorpay_order_id: "o", razorpay_key_id: "k" },
			{ description: "Jarvis subscription", openRazorpay, openCashfree, openMandate }
		);
		expect(openRazorpay).toHaveBeenCalledTimes(1);
		expect(openCashfree).not.toHaveBeenCalled();
		expect(out.status).toBe(CHECKOUT_SUCCESS);
	});

	test("a Cashfree order goes through the shipped Cashfree opener", async () => {
		await openOnboardingCheckout(
			{ payment_session_id: "p", cashfree_order_id: "cf_1" },
			{ openRazorpay, openCashfree, openMandate }
		);
		expect(openCashfree).toHaveBeenCalledTimes(1);
		expect(openRazorpay).not.toHaveBeenCalled();
	});

	test("a Cashfree mandate takes the redirect journey and reports it as such", async () => {
		const out = await openOnboardingCheckout(
			{ subscription_session_id: "sess" },
			{ openRazorpay, openCashfree, openMandate }
		);
		expect(openMandate).toHaveBeenCalledTimes(1);
		expect(out.status).toBe("redirected");
		expect(out.leavesPage).toBe(true);
	});

	test("a dismissal is passed through, never converted into a failure", async () => {
		openRazorpay = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const out = await openOnboardingCheckout(
			{ razorpay_order_id: "o" },
			{ openRazorpay, openCashfree, openMandate }
		);
		expect(out.status).toBe(CHECKOUT_DISMISSED);
	});

	test("handles this build cannot render are refused loudly, never silently skipped", async () => {
		await expect(
			openOnboardingCheckout(
				{ payment_provider: "some_new_gateway", their_handle: "x" },
				{ openRazorpay, openCashfree, openMandate }
			)
		).rejects.toThrow();
		expect(openRazorpay).not.toHaveBeenCalled();
	});

	test("an empty handle set is refused rather than reported as paid", async () => {
		await expect(
			openOnboardingCheckout({}, { openRazorpay, openCashfree, openMandate })
		).rejects.toThrow();
	});
});
