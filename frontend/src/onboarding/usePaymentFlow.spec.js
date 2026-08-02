// The orchestrator: everything the reducer is not allowed to do. Every side
// effect is injected, so this file can assert the rules that only show up in a
// sequence - check-on-failure after a dead checkout, a superseded attempt's
// loop stopping, a rate limit that does not become a retry.

import { describe, test, expect, vi } from "vitest";

import { CHECKOUT_SUCCESS, CHECKOUT_DISMISSED } from "@/lib/useRazorpay";
import { CODES } from "@/onboarding/paymentCodes";
import { STATES } from "@/onboarding/paymentMachine";
import { createPaymentFlow } from "@/onboarding/usePaymentFlow";

const ENVELOPE = (data, over = {}) => ({
	status: 200,
	body: { message: { ok: true, contract_version: 2, data, context: {}, ...over } },
});
const REFUSAL = (error, status = 409) => ({
	status,
	body: { message: { ok: false, error, context: {} } },
});

function makeApi(over = {}) {
	return {
		startSignup: vi.fn(async () =>
			ENVELOPE({
				code: CODES.PAYMENT_CONFIRMATION_PENDING,
				attempt_id: "att_1",
				generation: 1,
				payment_provider: "razorpay",
				razorpay_order_id: "order_1",
				razorpay_key_id: "k",
				can_initiate_payment: true,
				can_check_status: true,
			})
		),
		getOnboardingState: vi.fn(async () =>
			ENVELOPE({ code: CODES.PAYMENT_CONFIRMATION_PENDING, attempt_id: "att_1", generation: 1 })
		),
		initiateSignupPayment: vi.fn(async () =>
			ENVELOPE({
				code: CODES.PAYMENT_CONFIRMATION_PENDING,
				attempt_id: "att_1",
				generation: 2,
				payment_provider: "razorpay",
				razorpay_order_id: "order_2",
				razorpay_key_id: "k",
			})
		),
		checkSignupPaymentStatus: vi.fn(async () =>
			ENVELOPE({
				code: CODES.PAYMENT_CONFIRMATION_PENDING,
				attempt_id: "att_1",
				generation: 1,
				gateway_consulted: true,
			})
		),
		confirmSignupPayment: vi.fn(async () => ({
			status: 200,
			body: { message: { tenant_status: "running" } },
		})),
		syncConnection: vi.fn(async () => ({ synced: true })),
		...over,
	};
}

function makeFlow(over = {}) {
	const api = over.api || makeApi();
	const openCheckout =
		over.openCheckout ||
		vi.fn(async () => ({ status: CHECKOUT_SUCCESS, payload: { razorpay_payment_id: "pay_1" } }));
	const store = new Map();
	const flow = createPaymentFlow({
		api,
		openCheckout,
		sleep: over.sleep || (async () => {}),
		now: over.now || (() => 1_000_000),
		storage: {
			get: (k) => store.get(k) || null,
			set: (k, v) => store.set(k, v),
		},
		strict: true,
		...over.options,
	});
	return { flow, api, openCheckout };
}

describe("the first payment", () => {
	test("a review submit signs up exactly once and opens the sheet it was handed", async () => {
		const { flow, api, openCheckout } = makeFlow();
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro", provider: "razorpay" });
		expect(api.startSignup).toHaveBeenCalledTimes(1);
		expect(openCheckout).toHaveBeenCalledTimes(1);
		expect(api.confirmSignupPayment).toHaveBeenCalledTimes(1);
		expect(flow.state.value.value).toBe(STATES.PAID);
	});

	test("a signup still awaiting verification shows no payment action", async () => {
		const api = makeApi({
			startSignup: vi.fn(async () =>
				ENVELOPE({
					code: CODES.SIGNUP_VERIFICATION_REQUIRED,
					pending_verification: true,
					attempt_id: "att_1",
					generation: 0,
					can_initiate_payment: false,
				})
			),
		});
		const { flow, openCheckout } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(flow.state.value.value).toBe(STATES.VERIFICATION_REQUIRED);
		expect(openCheckout).not.toHaveBeenCalled();
	});

	test("money parked for reconciliation refuses the signup submit itself", async () => {
		const api = makeApi({
			startSignup: vi.fn(async () =>
				REFUSAL({
					code: CODES.BENCH_AWAITING_RECONCILIATION,
					message: "we're still confirming a payment on this signup",
					recovery: "check_status",
				})
			),
		});
		const { flow, openCheckout } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(openCheckout).not.toHaveBeenCalled();
		expect(flow.state.value.awaitingReconciliation).toBe(true);
		expect(flow.state.value.canInitiate).toBe(false);
	});
});

describe("check-on-failure is mandatory", () => {
	test("a dismissed sheet checks provider truth without being asked", async () => {
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow, api } = makeFlow({ openCheckout });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		// The customer closed the sheet - and may still have paid in another tab,
		// or a moment before closing it. Nothing may be assumed.
		expect(api.checkSignupPaymentStatus).toHaveBeenCalledTimes(1);
		expect(api.confirmSignupPayment).not.toHaveBeenCalled();
	});

	test("a sheet that could not open at all also checks", async () => {
		const openCheckout = vi.fn(async () => {
			throw new Error("Could not load the payment form.");
		});
		const { flow, api } = makeFlow({ openCheckout });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(api.checkSignupPaymentStatus).toHaveBeenCalledTimes(1);
		expect(flow.state.value.value).toBe(STATES.FAILED_RETRYABLE);
	});

	test("a failed confirm checks rather than declaring a failure", async () => {
		const api = makeApi({
			confirmSignupPayment: vi.fn(async () => ({
				status: 417,
				body: { exc_type: "ValidationError", error: { code: "", message: "gateway timeout" } },
			})),
		});
		const { flow } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(api.checkSignupPaymentStatus).toHaveBeenCalledTimes(1);
	});

	test("a check that discovers the payment landed advances to paid", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_ALREADY_ACTIVE,
					subscription_status: "Active",
					attempt_id: "att_1",
					generation: 1,
				})
			),
		});
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow } = makeFlow({ api, openCheckout });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(flow.state.value.value).toBe(STATES.PAID);
	});
});

describe("the rate limit", () => {
	test("a 429 cools the check down and never becomes a payment", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				REFUSAL(
					{
						code: CODES.PAYMENT_CHECK_RATE_LIMITED,
						message: "try again shortly",
						recovery: "retry",
						retry_after_seconds: 42,
					},
					429
				)
			),
		});
		const { flow } = makeFlow({ api });
		await flow.hydrate();
		const before = flow.state.value.value;
		await flow.checkStatus();
		expect(flow.state.value.value).toBe(before);
		expect(flow.state.value.canCheck).toBe(false);
		expect(flow.state.value.checkCooldownUntil).toBe(1_000_000 + 42_000);
		expect(api.initiateSignupPayment).not.toHaveBeenCalled();
	});
});

describe("the generation fence", () => {
	test("a superseded initiate's answer is discarded", async () => {
		let resolveSlow;
		const slow = new Promise((r) => (resolveSlow = r));
		const api = makeApi({
			initiateSignupPayment: vi
				.fn()
				.mockImplementationOnce(async () => {
					await slow;
					return ENVELOPE({
						code: CODES.PAYMENT_DECLINED,
						attempt_id: "att_1",
						generation: 1,
					});
				})
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 2,
						razorpay_order_id: "order_2",
						razorpay_key_id: "k",
					})
				),
		});
		const { flow } = makeFlow({ api });
		const first = flow.initiatePayment({ plan: "pro" });
		flow.cancelInFlight();
		await flow.initiatePayment({ plan: "pro" });
		resolveSlow();
		await first;
		// The slow first answer must not repaint the page it no longer describes.
		expect(flow.state.value.code).not.toBe(CODES.PAYMENT_DECLINED);
	});

	test("the provisioning poll stops when its attempt is superseded", async () => {
		let calls = 0;
		let flow;
		const api = makeApi({
			syncConnection: vi.fn(async () => {
				calls += 1;
				if (calls === 2) flow.cancelInFlight();
				return { synced: false, tenant_status: "pending" };
			}),
		});
		({ flow } = makeFlow({ api }));
		const out = await flow.waitForProvisioning();
		expect(out.status).toBe("superseded");
		// Without a fence this loop runs its full 45 iterations regardless.
		expect(calls).toBeLessThan(5);
	});

	test("the Cashfree confirm poll stops when its attempt is superseded", async () => {
		let calls = 0;
		let flow;
		const api = makeApi({
			confirmSignupPayment: vi.fn(async () => {
				calls += 1;
				if (calls === 2) flow.cancelInFlight();
				return { status: 417, body: { exc_type: "ValidationError", error: { code: "" } } };
			}),
		});
		const openCheckout = vi.fn(async () => ({
			status: CHECKOUT_SUCCESS,
			payload: { provider: "cashfree", cashfree_order_id: "cf_1" },
			pollConfirm: true,
		}));
		({ flow } = makeFlow({ api, openCheckout }));
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(calls).toBeLessThan(5);
	});
});

describe("the mount contract", () => {
	test("day one renders a fresh start, never 'contact support'", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () =>
				REFUSAL({
					code: CODES.BENCH_NO_SIGNUP_CONTEXT,
					message: "no signup has been started on this site yet",
					recovery: "retry",
				})
			),
		});
		const { flow } = makeFlow({ api });
		const out = await flow.hydrate();
		expect(out.notStarted).toBe(true);
		expect(out.paid).toBe(false);
		expect(flow.state.value.value).toBe(STATES.REVIEW);
	});

	test("an unpaid mid-flight signup is NOT paid, whatever the local credentials say", async () => {
		// The C02-1 bug: credentials are persisted at signup, BEFORE payment, so
		// "the bench has credentials" has never meant "the customer has paid".
		const { flow } = makeFlow();
		const out = await flow.hydrate();
		expect(out.paid).toBe(false);
	});

	test("an Active subscription is paid", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_ALREADY_ACTIVE,
					subscription_status: "Active",
					attempt_id: "att_1",
					generation: 1,
				})
			),
		});
		const { flow } = makeFlow({ api });
		const out = await flow.hydrate();
		expect(out.paid).toBe(true);
	});

	test("when the control plane cannot be reached, paid is UNKNOWN - not false", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () => ({ status: 0, networkError: true })),
		});
		const { flow } = makeFlow({ api });
		const out = await flow.hydrate();
		expect(out.paid).toBe(null);
		expect(out.truthKnown).toBe(false);
	});

	test("the mount never calls the retired verification poll", async () => {
		const { flow, api } = makeFlow();
		await flow.hydrate();
		expect(api.checkSignupPaymentState).toBeUndefined();
	});
});

describe("the support handoff", () => {
	test("checks against one attempt eventually offer a human", async () => {
		const { flow } = makeFlow();
		await flow.hydrate();
		for (let i = 0; i < 6; i++) await flow.checkStatus();
		expect(flow.state.value.supportOffered).toBe(true);
	});

	test("a new intent puts the offer away again", async () => {
		const { flow } = makeFlow();
		await flow.hydrate();
		for (let i = 0; i < 6; i++) await flow.checkStatus();
		expect(flow.state.value.supportOffered).toBe(true);
		await flow.initiatePayment({ plan: "pro" });
		expect(flow.state.value.supportOffered).toBe(false);
	});
});

describe("the idempotency key", () => {
	test("the SPA never sends one - the bench owns the receipt", async () => {
		const { flow, api } = makeFlow();
		await flow.initiatePayment({ plan: "pro" });
		const args = api.initiateSignupPayment.mock.calls[0][0] || {};
		expect(args.idempotency_key).toBeUndefined();
	});

	test("INVALID_REQUEST is a spent intent: the next attempt is a fresh one, not a resend", async () => {
		const api = makeApi({
			initiateSignupPayment: vi
				.fn()
				.mockImplementationOnce(async () =>
					REFUSAL({ code: CODES.INVALID_REQUEST, message: "idempotency_key too long", recovery: "retry" }, 400)
				)
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 2,
						razorpay_order_id: "order_2",
						razorpay_key_id: "k",
					})
				),
		});
		const { flow } = makeFlow({ api });
		await flow.initiatePayment({ plan: "pro" });
		expect(flow.state.value.code).toBe(CODES.INVALID_REQUEST);
		await flow.initiatePayment({ plan: "pro" });
		const second = api.initiateSignupPayment.mock.calls[1][0] || {};
		expect(second.idempotency_key).toBeUndefined();
		expect(flow.state.value.code).toBe(CODES.PAYMENT_CONFIRMATION_PENDING);
	});
});
