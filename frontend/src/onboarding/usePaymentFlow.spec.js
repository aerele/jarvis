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

	test("a 429 is NOT a check the customer got an answer to", async () => {
		// The support ceiling counts answers, not attempts. Counting a backoff
		// would march an impatient customer to the support handoff without a
		// single provider-truth reply behind it.
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				REFUSAL(
					{ code: CODES.PAYMENT_CHECK_RATE_LIMITED, message: "", recovery: "retry", retry_after_seconds: 5 },
					429
				)
			),
		});
		const { flow } = makeFlow({ api });
		await flow.hydrate();
		for (let i = 0; i < 8; i++) await flow.checkStatus();
		expect(flow.state.value.supportChecks.checks).toBe(0);
		expect(flow.state.value.supportOffered).toBe(false);
	});

	test("the cooldown re-arms the Check button through the flow's own clock", async () => {
		// The reducer must get a real clock however the flow passes it. tickCooldown
		// sends {type} with the time in opts; reading only event.nowMs left the
		// cooldown stuck forever and the charging action as the page's only live
		// control.
		let now = 1_000_000;
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				REFUSAL(
					{ code: CODES.PAYMENT_CHECK_RATE_LIMITED, message: "", recovery: "retry", retry_after_seconds: 60 },
					429
				)
			),
		});
		const { flow } = makeFlow({ api, now: () => now });
		await flow.hydrate();
		await flow.checkStatus();
		expect(flow.state.value.canCheck).toBe(false);
		now += 61_000;
		flow.tickCooldown();
		expect(flow.state.value.checkCooldownUntil).toBe(0);
		expect(flow.state.value.canCheck).toBe(true);
	});
});

describe("the confirm is not a transport check", () => {
	test("a 200 with an empty body is NOT a payment", async () => {
		const api = makeApi({
			confirmSignupPayment: vi.fn(async () => ({ status: 200, body: { message: {} } })),
		});
		const { flow } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(flow.state.value.value).not.toBe(STATES.PAID);
		expect(api.checkSignupPaymentStatus).toHaveBeenCalled();
	});

	test("admin's allocation-FAILURE confirm is paid, and its reason reaches the page", async () => {
		const api = makeApi({
			confirmSignupPayment: vi.fn(async () => ({
				status: 200,
				body: {
					message: {
						tenant_status: "pending",
						agent_url: "",
						chat_readiness: "Provisioning",
						chat_readiness_reason: "Something went wrong finishing setup — our team has been alerted.",
					},
				},
			})),
		});
		const { flow } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(flow.state.value.value).toBe(STATES.PAID);
		expect(flow.state.value.provisioningNote).toMatch(/our team has been alerted/);
	});

	test("a Cashfree confirm poll STOPS on a decided decline instead of re-asking 11 times", async () => {
		const confirm = vi.fn(async () => ({
			status: 402,
			body: {
				exc_type: "ValidationError",
				error: { code: CODES.PAYMENT_DECLINED, message: "This Cashfree mandate is not authorized." },
			},
		}));
		const openCheckout = vi.fn(async () => ({
			status: CHECKOUT_SUCCESS,
			payload: { provider: "cashfree", cashfree_order_id: "cf_1" },
			pollConfirm: true,
		}));
		const { flow } = makeFlow({ api: makeApi({ confirmSignupPayment: confirm }), openCheckout });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(confirm).toHaveBeenCalledTimes(1);
		expect(flow.state.value.code).toBe(CODES.PAYMENT_DECLINED);
		expect(flow.state.value.value).toBe(STATES.FAILED_RETRYABLE);
	});
});

describe("check-on-failure keeps the fact that matters", () => {
	test("parked money learned by the mandatory check suppresses the pay affordance", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 1,
					can_initiate_payment: false,
					can_check_status: true,
					awaiting_manual_reconciliation: true,
				})
			),
		});
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow } = makeFlow({ api, openCheckout });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(flow.state.value.awaitingReconciliation).toBe(true);
		expect(flow.state.value.canInitiate).toBe(false);
	});
});

describe("verification continues in one round trip", () => {
	test("a verified signup opens its checkout on the same click", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 1,
					payment_provider: "razorpay",
					razorpay_order_id: "order_1",
					razorpay_key_id: "k",
					can_check_status: true,
				})
			),
		});
		const { flow, openCheckout } = makeFlow({ api });
		await flow.verifyAndContinue();
		expect(openCheckout).toHaveBeenCalledTimes(1);
		expect(flow.state.value.value).toBe(STATES.PAID);
	});

	test("the verify button guards its own round trip", async () => {
		// The last unguarded action. Its two siblings (checkStatus, initiatePayment)
		// each hold a busy flag; without one here an impatient customer on the
		// "check your email" screen fires N concurrent state reads, and - because
		// this path OPENS THE CHECKOUT on success - N of them can race into opening
		// a gateway sheet.
		let inFlight = 0;
		let peak = 0;
		const api = makeApi({
			getOnboardingState: vi.fn(async () => {
				inFlight += 1;
				peak = Math.max(peak, inFlight);
				await Promise.resolve();
				inFlight -= 1;
				return ENVELOPE({
					code: CODES.SIGNUP_VERIFICATION_REQUIRED,
					pending_verification: true,
					attempt_id: "att_1",
					generation: 0,
				});
			}),
		});
		const { flow } = makeFlow({ api });
		await Promise.all([
			flow.verifyAndContinue(),
			flow.verifyAndContinue(),
			flow.verifyAndContinue(),
		]);
		expect(peak).toBe(1);
		expect(api.getOnboardingState).toHaveBeenCalledTimes(1);
		expect(flow.state.value.busy).toBe(null);
	});

	test("the verify guard clears even when the round trip throws", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () => {
				throw new Error("network died mid-verify");
			}),
		});
		const { flow } = makeFlow({ api });
		await expect(flow.verifyAndContinue()).rejects.toThrow();
		// A stuck busy flag would leave the button disabled forever - the same
		// class of trap as the cooldown that never lifted.
		expect(flow.state.value.busy).toBe(null);
	});

	test("a still-unverified signup opens nothing and keeps its own copy", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({
					code: CODES.SIGNUP_VERIFICATION_REQUIRED,
					pending_verification: true,
					attempt_id: "att_1",
					generation: 0,
				})
			),
		});
		const { flow, openCheckout } = makeFlow({ api });
		await flow.verifyAndContinue();
		expect(openCheckout).not.toHaveBeenCalled();
		expect(flow.state.value.value).toBe(STATES.VERIFICATION_REQUIRED);
	});
});

describe("the generation fence", () => {
	test("a superseded initiate's answer is discarded BY THE FENCE, not by luck", async () => {
		// The previous version of this test was worthless: its stale answer both
		// carried a LOSING generation (so the reducer's own generation fence caught
		// it) and reached `paid` (so the paid floor caught it too), and it asserted
		// only "the code is not DECLINED" - which two other mechanisms already
		// guaranteed. Deleting the client fence left it green.
		//
		// This version isolates the client fence: the stale answer carries the SAME
		// generation as the live one and a perfectly acceptable non-paid code, so
		// NOTHING in the reducer would reject it. Only the flow's own token can.
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
						generation: 7, // same generation as the winner below
						razorpay_order_id: "order_STALE",
						razorpay_key_id: "k",
					});
				})
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 7,
						razorpay_order_id: "order_LIVE",
						razorpay_key_id: "k",
					})
				),
		});
		// ...and the winner must NOT reach `paid`, or the PAID FLOOR becomes the
		// thing rejecting the stale answer and the client fence is again untested.
		// A dismissed sheet leaves the page on a live, non-terminal state that a
		// stale CONTRACT_STATE is perfectly entitled to overwrite - so the ONLY
		// thing standing between the two is the token.
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow } = makeFlow({ api, openCheckout });
		const first = flow.initiatePayment({ plan: "pro" });
		flow.cancelInFlight(); // the customer moved on; the first answer is now stale
		await flow.initiatePayment({ plan: "pro" });
		const valueAfterWinner = flow.state.value.value;
		const codeAfterWinner = flow.state.value.code;
		const handleAfterWinner = flow.state.value.handles.razorpay_order_id;
		// Guard the guard: if the winner ever reaches paid this test has stopped
		// testing the fence, and should fail loudly rather than pass for free.
		expect(valueAfterWinner).not.toBe(STATES.PAID);
		expect(valueAfterWinner).not.toBe(STATES.PROVISIONING);
		resolveSlow();
		await first;
		expect(flow.state.value.value).toBe(valueAfterWinner);
		expect(flow.state.value.code).toBe(codeAfterWinner);
		expect(flow.state.value.code).not.toBe(CODES.PAYMENT_DECLINED);
		expect(flow.state.value.handles.razorpay_order_id).toBe(handleAfterWinner);
		expect(handleAfterWinner).toBe("order_LIVE");
	});

	test("a slow MOUNT read cannot reset a checkout that is already open", async () => {
		// The mount read and the checkout it races share one token (nothing bumps
		// between them), so the machine STATE is the only discriminator. Without
		// this guard a stale day-one answer landed while the gateway sheet was open
		// and reset the page to `review` underneath it.
		let releaseSheet;
		const sheetOpen = new Promise((r) => (releaseSheet = r));
		const openCheckout = vi.fn(async () => {
			await sheetOpen;
			return { status: CHECKOUT_DISMISSED };
		});
		const api = makeApi({
			getOnboardingState: vi.fn(async () =>
				REFUSAL({ code: CODES.BENCH_NO_SIGNUP_CONTEXT, message: "", recovery: "retry" })
			),
		});
		const { flow } = makeFlow({ api, openCheckout });
		const paying = flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		await Promise.resolve();
		expect(flow.state.value.value).toBe(STATES.CHECKOUT_OPEN);
		const out = await flow.hydrate();
		expect(out.superseded).toBe(true);
		expect(flow.state.value.value).toBe(STATES.CHECKOUT_OPEN);
		releaseSheet();
		await paying;
	});

	test("the provisioning poll refuses to run twice at once", async () => {
		let inFlight = 0;
		let peak = 0;
		const api = makeApi({
			syncConnection: vi.fn(async () => {
				inFlight += 1;
				peak = Math.max(peak, inFlight);
				await Promise.resolve();
				inFlight -= 1;
				return { synced: false, tenant_status: "pending" };
			}),
		});
		const { flow } = makeFlow({ api, sleep: async () => {} });
		const a = flow.waitForProvisioning();
		const b = flow.waitForProvisioning();
		const [, second] = await Promise.all([a, b]);
		expect(second.status).toBe("already_running");
		expect(peak).toBe(1);
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
