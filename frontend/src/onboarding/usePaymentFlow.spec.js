// The orchestrator: everything the reducer is not allowed to do. Every side
// effect is injected, so this file can assert the rules that only show up in a
// sequence - the navigate-to-pay decision, check-on-failure after a return, a
// superseded attempt's loop stopping, a rate limit that does not become a retry.
//
// plan-09 WS7 (the admin-hosted checkout cutover): the flow opens NO gateway
// sheet. A payable answer carries a pay-page token + the bench's OWN attested
// origin, and the customer is TOP-LEVEL NAVIGATED to `{origin}/jarvis-checkout#t=
// <token>` via the injected `navigate`. There is NO FALLBACK: a token with no
// attested origin, or a pre-cutover admin's raw handles with no token, fails
// closed and navigates nothing.

import { describe, test, expect, vi } from "vitest";

import { CODES } from "@/onboarding/paymentCodes";
import { STATES, canNavigateToPay } from "@/onboarding/paymentMachine";
import { createPaymentFlow } from "@/onboarding/usePaymentFlow";

const ORIGIN = "https://fleet.klerk.in";

const ENVELOPE = (data, over = {}) => ({
	status: 200,
	body: { message: { ok: true, contract_version: 2, data, context: {}, ...over } },
});
const REFUSAL = (error, status = 409) => ({
	status,
	body: { message: { ok: false, error, context: {} } },
});

// A navigable pay-page token answer (the cutover shape): token + bench origin +
// admin's attestation, injected by onboarding_contract.augment_pay_page.
const TOKEN_DATA = (over = {}) => ({
	code: CODES.PAYMENT_PAGE_REDIRECT,
	attempt_id: "att_1",
	generation: 1,
	payment_provider: "razorpay",
	pay_page_token: "tok_1",
	pay_origin: ORIGIN,
	pay_origin_attested: true,
	can_check_status: true,
	...over,
});

function makeApi(over = {}) {
	return {
		startSignup: vi.fn(async () => ENVELOPE(TOKEN_DATA())),
		getOnboardingState: vi.fn(async () =>
			ENVELOPE({
				code: CODES.PAYMENT_CONFIRMATION_PENDING,
				attempt_id: "att_1",
				generation: 1,
			})
		),
		initiateSignupPayment: vi.fn(async () =>
			ENVELOPE(TOKEN_DATA({ generation: 2, pay_page_token: "tok_2" }))
		),
		checkSignupPaymentStatus: vi.fn(async () =>
			ENVELOPE({
				code: CODES.PAYMENT_CONFIRMATION_PENDING,
				attempt_id: "att_1",
				generation: 1,
				gateway_consulted: true,
			})
		),
		syncConnection: vi.fn(async () => ({ synced: true })),
		...over,
	};
}

function makeFlow(over = {}) {
	const api = over.api || makeApi();
	const navigate = over.navigate || vi.fn();
	const store = new Map();
	const flow = createPaymentFlow({
		api,
		navigate,
		sleep: over.sleep || (async () => {}),
		now: over.now || (() => 1_000_000),
		storage: { get: (k) => store.get(k) || null, set: (k, v) => store.set(k, v) },
		strict: true,
		...over.options,
	});
	return { flow, api, navigate };
}

// ---------------------------------------------------------------------------
describe("the first payment: navigate to the admin-hosted pay page", () => {
	test("a review submit signs up once and top-level-navigates to the built URL", async () => {
		const { flow, api, navigate } = makeFlow();
		await flow.submitReview({
			email: "a@b.com",
			company: "Acme",
			plan: "pro",
			provider: "razorpay",
		});
		expect(api.startSignup).toHaveBeenCalledTimes(1);
		expect(navigate).toHaveBeenCalledTimes(1);
		expect(navigate).toHaveBeenCalledWith({
			url: `${ORIGIN}/jarvis-checkout#t=tok_1`,
			attemptId: "att_1",
		});
		// The machine records we left for the pay page.
		expect(flow.state.value.value).toBe(STATES.CHECKOUT_OPEN);
		expect(flow.state.value.illegalTransitions).toBe(0);
	});

	test("a signup still awaiting verification navigates nothing", async () => {
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
		const { flow, navigate } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(flow.state.value.value).toBe(STATES.VERIFICATION_REQUIRED);
		expect(navigate).not.toHaveBeenCalled();
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
		const { flow, navigate } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(navigate).not.toHaveBeenCalled();
		expect(flow.state.value.awaitingReconciliation).toBe(true);
		expect(flow.state.value.canInitiate).toBe(false);
	});
});

// ---------------------------------------------------------------------------
describe("NO FALLBACK: nothing navigates without an attested token", () => {
	test("a token with an UNATTESTED origin fails closed and navigates nothing", async () => {
		const api = makeApi({
			startSignup: vi.fn(async () => ENVELOPE(TOKEN_DATA({ pay_origin_attested: false }))),
		});
		const { flow, navigate } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(navigate).not.toHaveBeenCalled();
		expect(flow.state.value.value).toBe(STATES.FAILED_TERMINAL);
		expect(flow.state.value.code).toBe(CODES.BENCH_PAY_ORIGIN_UNCONFIGURED);
	});

	test("a token with NO configured origin fails closed", async () => {
		const api = makeApi({
			startSignup: vi.fn(async () =>
				ENVELOPE(TOKEN_DATA({ pay_origin: "", pay_origin_attested: false }))
			),
		});
		const { flow, navigate } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(navigate).not.toHaveBeenCalled();
		expect(flow.state.value.code).toBe(CODES.BENCH_PAY_ORIGIN_UNCONFIGURED);
	});

	test("a pre-cutover admin's RAW HANDLES with no token never navigate (upgrade-required hold)", async () => {
		// The flow's decoder maps raw-handles-no-token to CLIENT_UPGRADE_REQUIRED; the
		// reducer renders an honest terminal hold and the flow navigates nothing.
		const api = makeApi({
			startSignup: vi.fn(async () =>
				ENVELOPE({
					// no `code`, no token: a flat legacy answer carrying raw handles
					attempt_id: "att_1",
					generation: 1,
					payment_provider: "razorpay",
					razorpay_order_id: "order_1",
					razorpay_key_id: "k",
				})
			),
		});
		const { flow, navigate } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(navigate).not.toHaveBeenCalled();
		expect(flow.state.value.value).toBe(STATES.FAILED_TERMINAL);
		expect(flow.state.value.code).toBe(CODES.CLIENT_UPGRADE_REQUIRED);
	});
});

// ---------------------------------------------------------------------------
describe("one navigation, ever, per intent state", () => {
	test("a burst of submit clicks produces exactly one signup and one navigate", async () => {
		const { flow, api, navigate } = makeFlow();
		await Promise.all([
			flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" }),
			flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" }),
			flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" }),
		]);
		expect(api.startSignup).toHaveBeenCalledTimes(1);
		expect(navigate).toHaveBeenCalledTimes(1);
	});

	test("navigateToPay is a no-op once we have already navigated (checkout_open)", async () => {
		const { flow, navigate } = makeFlow();
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(navigate).toHaveBeenCalledTimes(1);
		// A direct re-call cannot double-navigate: checkout_open is not navigable.
		const ok = flow.navigateToPay();
		expect(ok).toBe(false);
		expect(navigate).toHaveBeenCalledTimes(1);
	});
});

// ---------------------------------------------------------------------------
describe("initiate (the authenticated retry)", () => {
	test("a navigable initiate navigates, and the SPA sends NO idempotency key", async () => {
		const { flow, api, navigate } = makeFlow();
		await flow.initiatePayment({ plan: "pro", provider: "razorpay" });
		expect(navigate).toHaveBeenCalledTimes(1);
		expect(navigate).toHaveBeenCalledWith({
			url: `${ORIGIN}/jarvis-checkout#t=tok_2`,
			attemptId: "att_1",
		});
		const [args] = api.initiateSignupPayment.mock.calls[0];
		expect(args).not.toHaveProperty("idempotency_key");
	});
});

// ---------------------------------------------------------------------------
describe("verification continues in one round trip", () => {
	test("a verified, now-navigable signup navigates on the same click", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () => ENVELOPE(TOKEN_DATA())),
		});
		const { flow, navigate } = makeFlow({ api });
		await flow.verifyAndContinue();
		expect(navigate).toHaveBeenCalledTimes(1);
	});

	test("a still-unverified signup navigates nothing and keeps its own copy", async () => {
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
		const { flow, navigate } = makeFlow({ api });
		await flow.verifyAndContinue();
		expect(navigate).not.toHaveBeenCalled();
		expect(flow.state.value.value).toBe(STATES.VERIFICATION_REQUIRED);
	});

	test("the verify button guards its own round trip (a triple-click is one call, one navigate)", async () => {
		const api = makeApi({ getOnboardingState: vi.fn(async () => ENVELOPE(TOKEN_DATA())) });
		const { flow, navigate } = makeFlow({ api });
		await Promise.all([
			flow.verifyAndContinue(),
			flow.verifyAndContinue(),
			flow.verifyAndContinue(),
		]);
		expect(api.getOnboardingState).toHaveBeenCalledTimes(1);
		expect(navigate).toHaveBeenCalledTimes(1);
	});
});

// ---------------------------------------------------------------------------
describe("the machine decides what navigates", () => {
	test("a GENERATION-FENCED answer navigates nothing (the reducer refused it)", async () => {
		// Seed a live intent at generation 5.
		const api = makeApi({
			startSignup: vi.fn(async () => ENVELOPE(TOKEN_DATA({ generation: 5 }))),
			// A retry whose answer is a LOSING generation of the same attempt: the
			// reducer ignores it outright, so it is not navigable and nothing navigates.
			initiateSignupPayment: vi.fn(async () =>
				ENVELOPE(TOKEN_DATA({ generation: 1, pay_page_token: "stale" }))
			),
			// The reconcile after the return must not itself supersede the intent.
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 5,
				})
			),
		});
		const { flow, navigate } = makeFlow({ api });
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" }); // → CHECKOUT_OPEN
		await flow.returnFromCheckout(); // → UNKNOWN (gen 5)
		navigate.mockClear();
		await flow.initiatePayment({ plan: "pro" });
		expect(navigate).not.toHaveBeenCalled();
		expect(canNavigateToPay(flow.state.value)).toBe(false);
	});
});

// ---------------------------------------------------------------------------
describe("the rate limit", () => {
	test("a 429 cools the check down and never becomes a payment", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				REFUSAL({ code: CODES.PAYMENT_CHECK_RATE_LIMITED, retry_after_seconds: 30 }, 429)
			),
		});
		const { flow, navigate } = makeFlow({ api });
		await flow.checkStatus();
		expect(flow.state.value.checkCooldownUntil).toBeGreaterThan(0);
		expect(navigate).not.toHaveBeenCalled();
	});

	test("a 429 is NOT a check the customer got an answer to (no support tick)", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				REFUSAL({ code: CODES.PAYMENT_CHECK_RATE_LIMITED, retry_after_seconds: 5 }, 429)
			),
		});
		const { flow } = makeFlow({ api });
		await flow.checkStatus();
		expect(flow.state.value.supportChecks.checks).toBe(0);
	});
});

// ---------------------------------------------------------------------------
describe("check-on-failure keeps the fact that matters (P0-1)", () => {
	test("a check that discovers the payment landed advances to paid", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_ALREADY_ACTIVE,
					attempt_id: "att_1",
					generation: 1,
				})
			),
		});
		const { flow } = makeFlow({ api });
		await flow.checkStatus();
		expect(flow.state.value.value).toBe(STATES.PAID);
	});

	test("parked money learned by a check suppresses the pay affordance", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 1,
					can_initiate_payment: true,
					awaiting_manual_reconciliation: true,
				})
			),
		});
		const { flow } = makeFlow({ api });
		await flow.checkStatus();
		expect(flow.state.value.awaitingReconciliation).toBe(true);
		expect(flow.state.value.canInitiate).toBe(false);
	});
});

// ---------------------------------------------------------------------------
describe("return from the pay page (P0-2)", () => {
	test("returnFromCheckout leaves checkout_open for a reconciled unknown", async () => {
		const { flow, api } = makeFlow();
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(flow.state.value.value).toBe(STATES.CHECKOUT_OPEN);
		const out = await flow.returnFromCheckout();
		expect(out.returned).toBe(true);
		// It reconciled server truth (the mandatory check ran).
		expect(api.checkSignupPaymentStatus).toHaveBeenCalled();
	});

	test("returnFromCheckout is a no-op when we never navigated away", async () => {
		const { flow } = makeFlow();
		const out = await flow.returnFromCheckout();
		expect(out.returned).toBe(false);
	});
});

// ---------------------------------------------------------------------------
describe("cancelInFlight", () => {
	test("a cancel mid-verify does not strand the busy flag", async () => {
		let release;
		const api = makeApi({
			getOnboardingState: vi.fn(() => new Promise((r) => (release = r))),
		});
		const { flow } = makeFlow({ api });
		const p = flow.verifyAndContinue();
		// Let `deadlined` call the api (it invokes fn on a microtask) so `release` is set.
		await new Promise((r) => setTimeout(r, 0));
		flow.cancelInFlight();
		release(ENVELOPE(TOKEN_DATA()));
		await p;
		expect(flow.state.value.busy).toBe(null);
	});
});

// ---------------------------------------------------------------------------
describe("the mount contract", () => {
	test("day one renders a fresh start, never 'contact support'", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({ code: CODES.BENCH_NO_SIGNUP_CONTEXT })
			),
		});
		const { flow } = makeFlow({ api });
		const truth = await flow.hydrate();
		expect(truth.notStarted).toBe(true);
		expect(flow.state.value.value).toBe(STATES.REVIEW);
	});

	test("an unpaid mid-flight signup is NOT paid, whatever the local credentials say", async () => {
		const { flow } = makeFlow();
		const truth = await flow.hydrate();
		expect(truth.paid).toBe(false);
	});

	test("an Active subscription is paid", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_ALREADY_ACTIVE,
					attempt_id: "att_1",
					generation: 1,
				})
			),
		});
		const { flow } = makeFlow({ api });
		const truth = await flow.hydrate();
		expect(truth.paid).toBe(true);
	});

	test("when the control plane cannot be reached, paid is UNKNOWN - not false", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () => ({ status: 0, body: null, networkError: true })),
		});
		const { flow } = makeFlow({ api });
		const truth = await flow.hydrate();
		expect(truth.paid).toBe(null);
	});

	test("a FROZEN checkout_open on mount takes the safe return exit and reconciles", async () => {
		// Simulate a bfcache-restored instance already in checkout_open, then hydrate.
		const { flow, api } = makeFlow();
		await flow.submitReview({ email: "a@b.com", company: "Acme", plan: "pro" });
		expect(flow.state.value.value).toBe(STATES.CHECKOUT_OPEN);
		api.getOnboardingState.mockClear();
		await flow.hydrate();
		// It left checkout_open via the explicit exit and read server truth.
		expect(flow.state.value.value).not.toBe(STATES.CHECKOUT_OPEN);
		expect(api.getOnboardingState).toHaveBeenCalled();
	});
});

// ---------------------------------------------------------------------------
describe("the support handoff", () => {
	test("checks against one attempt eventually offer a human", async () => {
		const { flow } = makeFlow();
		for (let i = 0; i < 6; i++) await flow.checkStatus();
		expect(flow.state.value.supportOffered).toBe(true);
	});
});

// ---------------------------------------------------------------------------
describe("the provisioning poll", () => {
	test("refuses to run twice at once", async () => {
		let release;
		const api = makeApi({
			syncConnection: vi.fn(() => new Promise((r) => (release = r))),
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_ALREADY_ACTIVE,
					attempt_id: "att_1",
					generation: 1,
				})
			),
		});
		const { flow } = makeFlow({ api });
		await flow.hydrate(); // → paid
		const p1 = flow.waitForProvisioning();
		const p2 = flow.waitForProvisioning();
		expect(await p2).toEqual({ status: "already_running" });
		release({ synced: true });
		await p1;
	});

	test("stops when its attempt is superseded", async () => {
		const api = makeApi({
			syncConnection: vi.fn(async () => ({ synced: false })),
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_ALREADY_ACTIVE,
					attempt_id: "att_1",
					generation: 1,
				})
			),
		});
		const { flow } = makeFlow({ api, sleep: async () => {} });
		await flow.hydrate();
		const p = flow.waitForProvisioning();
		flow.cancelInFlight();
		expect(await p).toEqual({ status: "superseded" });
	});
});
