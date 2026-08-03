// The orchestrator: everything the reducer is not allowed to do. Every side
// effect is injected, so this file can assert the rules that only show up in a
// sequence - check-on-failure after a dead checkout, a superseded attempt's
// loop stopping, a rate limit that does not become a retry.

import { describe, test, expect, vi } from "vitest";

import { CHECKOUT_SUCCESS, CHECKOUT_DISMISSED } from "@/lib/useRazorpay";
import { classifyOnboardingHandles } from "@/onboarding/onboardingCheckout";
import { CODES } from "@/onboarding/paymentCodes";
import { STATES, canOpenCheckout } from "@/onboarding/paymentMachine";
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
			ENVELOPE({
				code: CODES.PAYMENT_CONFIRMATION_PENDING,
				attempt_id: "att_1",
				generation: 1,
			})
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
		vi.fn(async () => ({
			status: CHECKOUT_SUCCESS,
			payload: { razorpay_payment_id: "pay_1" },
		}));
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
		await flow.submitReview({
			email: "a@b.com",
			company: "Acme",
			plan: "pro",
			provider: "razorpay",
		});
		expect(api.startSignup).toHaveBeenCalledTimes(1);
		expect(openCheckout).toHaveBeenCalledTimes(1);
		expect(api.confirmSignupPayment).toHaveBeenCalledTimes(1);
		expect(flow.state.value.value).toBe(STATES.PAID);
		// The silent counter, read as an oracle: a happy path that racks one up has
		// done something the machine refused to record.
		expect(flow.state.value.illegalTransitions).toBe(0);
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
		// The mandatory check found the intent still pending, so the page reflects
		// server truth (a checkable UNKNOWN) - the old code froze on the SDK-failure
		// framing and discarded the answer (P0-1). The "could not open" reason
		// survives as presentation metadata, never as a preserved code/state.
		expect(flow.state.value.value).toBe(STATES.UNKNOWN);
		expect(flow.state.value.checkoutNote).toBe("Could not load the payment form.");
	});

	test("a failed confirm checks rather than declaring a failure", async () => {
		const api = makeApi({
			confirmSignupPayment: vi.fn(async () => ({
				status: 417,
				body: {
					exc_type: "ValidationError",
					error: { code: "", message: "gateway timeout" },
				},
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
					{
						code: CODES.PAYMENT_CHECK_RATE_LIMITED,
						message: "",
						recovery: "retry",
						retry_after_seconds: 5,
					},
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
					{
						code: CODES.PAYMENT_CHECK_RATE_LIMITED,
						message: "",
						recovery: "retry",
						retry_after_seconds: 60,
					},
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
						chat_readiness_reason:
							"Something went wrong finishing setup — our team has been alerted.",
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
				error: {
					code: CODES.PAYMENT_DECLINED,
					message: "This Cashfree mandate is not authorized.",
				},
			},
		}));
		const openCheckout = vi.fn(async () => ({
			status: CHECKOUT_SUCCESS,
			payload: { provider: "cashfree", cashfree_order_id: "cf_1" },
			pollConfirm: true,
		}));
		const { flow } = makeFlow({
			api: makeApi({ confirmSignupPayment: confirm }),
			openCheckout,
		});
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
		expect(flow.state.value.illegalTransitions).toBe(0);
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

describe("the machine decides what opens", () => {
	// The SPA must never open a gateway sheet the machine refused. The decision to
	// open used to be read off the ANSWER (`decoded.data`) while the reducer gates
	// CHECKOUT_OPENED on the STATE's handles - and three of applyContract's early
	// returns never merge an answer's handles into the state (the generation
	// fence, the unattributable branch, the paid floor). When the two disagreed
	// the transition was refused SILENTLY, so an interactive card stayed on screen
	// underneath an opening sheet.
	//
	// These run NON-strict on purpose: production passes no `strict`, so the
	// refusal is counted rather than thrown, and `illegalTransitions` is the
	// oracle. Under strict the refusal throws into runCheckout's own catch and the
	// bug hides behind a CHECKOUT_FAILED.
	const PAYABLE = {
		attempt_id: "att_1",
		payment_provider: "razorpay",
		razorpay_order_id: "order_verified",
		razorpay_key_id: "k",
		can_initiate_payment: true,
		can_check_status: true,
	};
	// The state every one of these builds from, through the real flow: a signup
	// that has been started and is waiting on the magic link.
	const unverified = (generation) =>
		vi.fn(async () =>
			ENVELOPE({
				code: CODES.SIGNUP_VERIFICATION_REQUIRED,
				pending_verification: true,
				attempt_id: "att_1",
				generation,
				can_initiate_payment: false,
			})
		);

	test("an UNATTRIBUTABLE verify answer opens nothing and leaves a live card", async () => {
		const api = makeApi({
			startSignup: unverified(0),
			// No generation at all: the reducer cannot attribute these handles to the
			// live intent and keeps its own (which are empty), so it would refuse the
			// open. The flow must refuse it too.
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({ code: CODES.PAYMENT_CONFIRMATION_PENDING, ...PAYABLE })
			),
		});
		const { flow, openCheckout } = makeFlow({ api, options: { strict: false } });
		await flow.submitReview({
			email: "a@b.com",
			company: "Acme",
			plan: "pro",
			provider: "razorpay",
		});
		expect(flow.state.value.value).toBe(STATES.VERIFICATION_REQUIRED);
		await flow.verifyAndContinue();
		expect(openCheckout).not.toHaveBeenCalled();
		expect(flow.state.value.illegalTransitions).toBe(0);
		// ...and what the customer is left looking at is a card they can use, not a
		// spinner over an invisible sheet.
		expect(flow.state.value.value).toBe(STATES.UNKNOWN);
		expect(flow.state.value.busy).toBe(null);
		expect(flow.state.value.canInitiate).toBe(true);
	});

	test("a GENERATION-FENCED verify answer opens nothing and keeps Verify alive", async () => {
		const api = makeApi({
			startSignup: unverified(7),
			// A losing generation: the reducer's fence discards this answer outright,
			// handles included. Opening on them is opening a DEAD order.
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					...PAYABLE,
					generation: 2,
					razorpay_order_id: "order_STALE",
				})
			),
		});
		const { flow, openCheckout } = makeFlow({ api, options: { strict: false } });
		await flow.submitReview({
			email: "a@b.com",
			company: "Acme",
			plan: "pro",
			provider: "razorpay",
		});
		expect(flow.state.value.generation).toBe(7);
		await flow.verifyAndContinue();
		expect(openCheckout).not.toHaveBeenCalled();
		expect(flow.state.value.illegalTransitions).toBe(0);
		// The answer was refused, so nothing about the page moved: the customer is
		// still on the verify screen with a live button.
		expect(flow.state.value.value).toBe(STATES.VERIFICATION_REQUIRED);
		expect(flow.state.value.busy).toBe(null);
	});

	test("an ACCEPTED verify answer opens exactly one sheet, on the handles the machine kept", async () => {
		const api = makeApi({
			startSignup: unverified(0),
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({ code: CODES.PAYMENT_CONFIRMATION_PENDING, ...PAYABLE, generation: 1 })
			),
		});
		let flow;
		let atOpen = null;
		let handedTo = null;
		const openCheckout = vi.fn(async (handles) => {
			handedTo = handles;
			const st = flow.state.value;
			atOpen = {
				value: st.value,
				busy: st.busy,
				handles: { ...st.handles },
				provider: st.provider,
			};
			return { status: CHECKOUT_SUCCESS, payload: { razorpay_payment_id: "pay_1" } };
		});
		({ flow } = makeFlow({ api, openCheckout, options: { strict: false } }));
		await flow.submitReview({
			email: "a@b.com",
			company: "Acme",
			plan: "pro",
			provider: "razorpay",
		});
		await flow.verifyAndContinue();
		expect(openCheckout).toHaveBeenCalledTimes(1);
		// The busy view while the sheet opens - nothing to press underneath it.
		expect(atOpen.value).toBe(STATES.CHECKOUT_OPEN);
		expect(atOpen.busy).toBe(null);
		// The gateway is handed exactly what the machine accepted, never the raw
		// answer: if the reducer did not keep it, it does not reach the sheet.
		expect(handedTo).toEqual({ ...atOpen.handles, payment_provider: atOpen.provider });
		expect(flow.state.value.illegalTransitions).toBe(0);
		expect(flow.state.value.value).toBe(STATES.PAID);
	});

	test("a RETRY whose answer the machine refuses opens nothing and leaves the card usable", async () => {
		// initiatePayment had the identical disagreement, on the surface where it
		// costs the most: the recovery card, whose two buttons are the customer's
		// only way forward. A refused answer must give them back, not raise a sheet
		// over them (and not strand the flag that disables them either).
		const api = makeApi({
			// A resumed page that knows its generation but holds no handles - the
			// state read carries none, exactly as the default admin answer does.
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 7,
					can_initiate_payment: true,
					can_check_status: true,
				})
			),
			initiateSignupPayment: vi.fn(async () =>
				ENVELOPE({ code: CODES.PAYMENT_CONFIRMATION_PENDING, ...PAYABLE, generation: 3 })
			),
		});
		const { flow, openCheckout } = makeFlow({ api, options: { strict: false } });
		await flow.hydrate();
		expect(flow.state.value.canInitiate).toBe(true);
		await flow.initiatePayment({ plan: "pro" });
		expect(openCheckout).not.toHaveBeenCalled();
		expect(flow.state.value.illegalTransitions).toBe(0);
		expect(flow.state.value.busy).toBe(null);
		expect(flow.state.value.canInitiate).toBe(true);
		expect(flow.state.value.canCheck).toBe(true);
	});
});

describe("the sheet is built for the gateway the machine names", () => {
	// applyContract MERGES handles at an equal generation and replaces them
	// wholesale only when the generation advances - and a same-generation answer
	// is a DESIGNED event, not a race: the bench reuses its stored idempotency key
	// so a double-clicked Pay, a retried POST and a refreshed page converge on one
	// gateway object. Meanwhile classifyOnboardingHandles sniffs the MANDATE SHAPE
	// before it ever consults payment_provider. So a set that has accumulated two
	// gateways' keys is classified by whichever shape it matches first: a stale
	// Cashfree session sitting beside a live Razorpay order sent the customer to a
	// full-page Cashfree redirect for a Razorpay order. The sheet must be built
	// from the handles of the gateway the MACHINE NAMES, and nothing else.
	const CASHFREE_KEYS = [
		"payment_session_id",
		"cashfree_order_id",
		"cashfree_subscription_id",
		"subscription_session_id",
		"cashfree_app_id",
		"cashfree_env",
	];
	const RAZORPAY_KEYS = ["razorpay_order_id", "razorpay_subscription_id", "razorpay_key_id"];

	function recordingFlow(api) {
		const handed = [];
		const openCheckout = vi.fn(async (h) => {
			handed.push(h);
			return { status: CHECKOUT_DISMISSED };
		});
		const { flow } = makeFlow({ api, openCheckout, options: { strict: false } });
		return { flow, handed, openCheckout };
	}

	test("a cross-provider merge built by a STATUS CHECK never reaches the SDK", async () => {
		// No initiate is needed to build the mix: checkStatus absorbs answers too.
		const api = makeApi({
			initiateSignupPayment: vi
				.fn()
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						payment_provider: "cashfree",
						subscription_session_id: "cf_sub_sess",
						cashfree_env: "sandbox",
						can_check_status: true,
					})
				)
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5, // the SAME intent: the bench converged the retry onto it
						payment_provider: "razorpay",
						razorpay_order_id: "order_RZP",
						razorpay_key_id: "k",
						can_check_status: true,
					})
				),
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 5,
					payment_provider: "razorpay",
					razorpay_order_id: "order_RZP",
					razorpay_key_id: "k",
					can_check_status: true,
				})
			),
		});
		const { flow, handed } = recordingFlow(api);
		await flow.initiatePayment({ plan: "pro", provider: "cashfree" }); // the mandate
		await flow.checkStatus(); // a plain status read, carrying the other gateway
		await flow.initiatePayment({ plan: "pro", provider: "razorpay" });
		// The reducer's merge is untouched - this is a fix at the sheet, not in the
		// transition table.
		expect(flow.state.value.handles).toEqual({
			subscription_session_id: "cf_sub_sess",
			cashfree_env: "sandbox",
			razorpay_order_id: "order_RZP",
			razorpay_key_id: "k",
		});
		expect(flow.state.value.provider).toBe("razorpay");
		expect(handed[1]).toEqual({
			razorpay_order_id: "order_RZP",
			razorpay_key_id: "k",
			payment_provider: "razorpay",
		});
		for (const k of CASHFREE_KEYS) expect(handed[1]).not.toHaveProperty(k);
		expect(classifyOnboardingHandles(handed[1])).toBe("razorpay");
		expect(flow.state.value.illegalTransitions).toBe(0);
	});

	test("the mirror direction - a stale Razorpay subscription cannot capture a Cashfree order", async () => {
		const api = makeApi({
			initiateSignupPayment: vi
				.fn()
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						payment_provider: "razorpay",
						razorpay_subscription_id: "sub_RZP",
						razorpay_key_id: "k",
						can_check_status: true,
					})
				)
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						payment_provider: "cashfree",
						payment_session_id: "sess_CF",
						cashfree_order_id: "cf_1",
						cashfree_env: "sandbox",
						can_check_status: true,
					})
				),
		});
		const { flow, handed } = recordingFlow(api);
		await flow.initiatePayment({ plan: "pro", provider: "razorpay" });
		await flow.initiatePayment({ plan: "pro", provider: "cashfree" });
		expect(flow.state.value.provider).toBe("cashfree");
		expect(handed[1]).toEqual({
			payment_session_id: "sess_CF",
			cashfree_order_id: "cf_1",
			cashfree_env: "sandbox",
			payment_provider: "cashfree",
		});
		for (const k of RAZORPAY_KEYS) expect(handed[1]).not.toHaveProperty(k);
		expect(classifyOnboardingHandles(handed[1])).toBe("cashfree_order");
		expect(flow.state.value.illegalTransitions).toBe(0);
	});

	test("CONTROL: an ordinary same-provider retry still opens on the newest handles", async () => {
		const api = makeApi({
			initiateSignupPayment: vi
				.fn()
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						payment_provider: "razorpay",
						razorpay_order_id: "order_A",
						razorpay_key_id: "k",
						amount_inr: 4999,
						can_check_status: true,
					})
				)
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						payment_provider: "razorpay",
						razorpay_order_id: "order_B",
						razorpay_key_id: "k",
						amount_inr: 4999,
						can_check_status: true,
					})
				),
		});
		const { flow, handed } = recordingFlow(api);
		await flow.initiatePayment({ plan: "pro", provider: "razorpay" });
		await flow.initiatePayment({ plan: "pro", provider: "razorpay" });
		// The price rides along with either gateway - it is not a handle.
		expect(handed[1]).toEqual({
			razorpay_order_id: "order_B",
			razorpay_key_id: "k",
			amount_inr: 4999,
			payment_provider: "razorpay",
		});
		expect(classifyOnboardingHandles(handed[1])).toBe("razorpay");
		expect(flow.state.value.illegalTransitions).toBe(0);
	});

	test("a rider that cannot open anything never claims the sheet from a live order", async () => {
		// `cashfree_subscription_id` opens NOTHING on its own (the mandate opener
		// reads subscription_session_id), so it does not make its own family
		// openable - which sends the narrowing to its fallback. It was still
		// decisive at classification time, so the fallback's set handed a live
		// Razorpay order to a Cashfree mandate redirect.
		const api = makeApi({
			initiateSignupPayment: vi
				.fn()
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						payment_provider: "cashfree",
						cashfree_subscription_id: "cf_sub_STALE", // no session id: nothing to open
						cashfree_env: "sandbox",
						can_check_status: true,
					})
				)
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						// names no provider, so the machine's label stays `cashfree`
						razorpay_order_id: "order_LIVE",
						razorpay_key_id: "k",
						amount_inr: 4999,
						can_check_status: true,
					})
				),
		});
		const { flow, handed } = recordingFlow(api);
		await flow.initiatePayment({ plan: "pro", provider: "cashfree" });
		expect(handed).toHaveLength(0); // nothing openable yet - no sheet was raised
		await flow.initiatePayment({ plan: "pro" });
		expect(flow.state.value.provider).toBe("cashfree"); // the label is stale, and stays stale
		expect(handed).toHaveLength(1);
		expect(classifyOnboardingHandles(handed[0])).toBe("razorpay");
		for (const k of CASHFREE_KEYS) expect(handed[0]).not.toHaveProperty(k);
		expect(handed[0]).toEqual({
			razorpay_order_id: "order_LIVE",
			razorpay_key_id: "k",
			amount_inr: 4999,
			// The machine's own provider, passed along as it always was. The sheet no
			// longer depends on it: a handle decides the classification first.
			payment_provider: "cashfree",
		});
		expect(flow.state.value.illegalTransitions).toBe(0);
	});

	test("the same rider does not outrank a live session INSIDE its own family", async () => {
		// Narrowing cannot help here - both keys are Cashfree's - so this one is
		// settled purely by refusing to classify on a key that opens nothing.
		const api = makeApi({
			initiateSignupPayment: vi
				.fn()
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						payment_provider: "cashfree",
						cashfree_subscription_id: "cf_sub_STALE",
						cashfree_env: "sandbox",
						can_check_status: true,
					})
				)
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						payment_provider: "cashfree",
						payment_session_id: "sess_LIVE",
						cashfree_order_id: "cf_order_LIVE",
						cashfree_env: "sandbox",
						can_check_status: true,
					})
				),
		});
		const { flow, handed } = recordingFlow(api);
		await flow.initiatePayment({ plan: "pro", provider: "cashfree" });
		await flow.initiatePayment({ plan: "pro", provider: "cashfree" });
		expect(handed).toHaveLength(1);
		expect(classifyOnboardingHandles(handed[0])).toBe("cashfree_order");
		expect(handed[0].payment_session_id).toBe("sess_LIVE");
		expect(handed[0].cashfree_order_id).toBe("cf_order_LIVE");
		expect(flow.state.value.illegalTransitions).toBe(0);
	});

	test("CONTROL: a real mandate still takes the mandate journey", async () => {
		const api = makeApi({
			initiateSignupPayment: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 5,
					payment_provider: "cashfree",
					subscription_session_id: "sub_sess_LIVE",
					cashfree_subscription_id: "cf_sub_LIVE",
					cashfree_env: "sandbox",
					can_check_status: true,
				})
			),
		});
		const { flow, handed } = recordingFlow(api);
		await flow.initiatePayment({ plan: "pro", provider: "cashfree" });
		expect(handed).toHaveLength(1);
		expect(classifyOnboardingHandles(handed[0])).toBe("cashfree_mandate");
		expect(handed[0].subscription_session_id).toBe("sub_sess_LIVE");
		expect(flow.state.value.illegalTransitions).toBe(0);
	});

	test("CONTROL: an advanced generation replaces the set wholesale, as it always did", async () => {
		const api = makeApi({
			initiateSignupPayment: vi
				.fn()
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 5,
						payment_provider: "cashfree",
						subscription_session_id: "cf_sub_sess",
						cashfree_env: "sandbox",
						can_check_status: true,
					})
				)
				.mockImplementationOnce(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 6, // a NEW intent: the old order is dead
						payment_provider: "razorpay",
						razorpay_order_id: "order_RZP",
						razorpay_key_id: "k",
						amount_inr: 4999,
						can_check_status: true,
					})
				),
		});
		const { flow, handed } = recordingFlow(api);
		await flow.initiatePayment({ plan: "pro", provider: "cashfree" });
		await flow.initiatePayment({ plan: "pro", provider: "razorpay" });
		expect(flow.state.value.handles).toEqual({
			razorpay_order_id: "order_RZP",
			razorpay_key_id: "k",
			amount_inr: 4999,
		});
		expect(handed[1]).toEqual({
			razorpay_order_id: "order_RZP",
			razorpay_key_id: "k",
			amount_inr: 4999,
			payment_provider: "razorpay",
		});
		expect(classifyOnboardingHandles(handed[1])).toBe("razorpay");
		expect(flow.state.value.illegalTransitions).toBe(0);
	});
});

describe("cancelInFlight", () => {
	// Every release path is fenced on `my === token`, so the bump that invalidates
	// the in-flight work also invalidates its own release. After a cancel the busy
	// flag belongs to nobody, and leaving it set is a dead button forever.
	test("a cancel mid-verify does not strand the busy flag", async () => {
		let flow;
		const api = makeApi({
			getOnboardingState: vi.fn(async () => {
				flow.cancelInFlight();
				return ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 1,
					payment_provider: "razorpay",
					razorpay_order_id: "order_1",
					razorpay_key_id: "k",
				});
			}),
		});
		({ flow } = makeFlow({ api }));
		await flow.verifyAndContinue();
		expect(flow.state.value.busy).toBe(null);
	});

	test("a cancel mid-check does not strand the busy flag", async () => {
		let flow;
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () => {
				flow.cancelInFlight();
				return ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 1,
				});
			}),
		});
		({ flow } = makeFlow({ api }));
		await flow.checkStatus();
		expect(flow.state.value.busy).toBe(null);
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
		expect(valueAfterWinner).not.toBe(STATES.PROVISIONING_DELAYED);
		resolveSlow();
		await first;
		// THE consequence a dead fence has, and the one nothing here asserted: the
		// stale answer is absorbed, its dead order id merges over the live one, and
		// a SECOND gateway sheet opens on `order_STALE`. The state assertions below
		// cannot see it - the dismissed sheet re-lands the same state either way.
		expect(openCheckout).toHaveBeenCalledTimes(1);
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
		// Flush microtasks until the sheet is open (the deadline wrapper adds a few
		// hops before start_signup resolves; the count is incidental to the race).
		for (let i = 0; i < 50 && flow.state.value.value !== STATES.CHECKOUT_OPEN; i++) {
			await Promise.resolve();
		}
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
					REFUSAL(
						{
							code: CODES.INVALID_REQUEST,
							message: "idempotency_key too long",
							recovery: "retry",
						},
						400
					)
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

// ===========================================================================
// Round-2 hardening (plan 02): the reproduced review probes, as regression
// tests. Each fails if its own mechanism is removed.
// ===========================================================================

const SIGNUP = { email: "a@b.com", company: "Acme", plan: "pro" };

describe("P0-1: the mandatory reconcile absorbs every authoritative answer", () => {
	test("dismiss -> check says authorized-pending-confirm -> no Initiate, correct copy", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM,
					attempt_id: "att_1",
					generation: 1,
					can_initiate_payment: false,
					can_check_status: true,
				})
			),
		});
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow } = makeFlow({ api, openCheckout });
		await flow.submitReview(SIGNUP);
		expect(flow.state.value.value).toBe(STATES.CONFIRM_REQUIRED);
		expect(flow.state.value.code).toBe(CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM);
		expect(flow.state.value.canInitiate).toBe(false);
	});

	test("dismiss -> check says terminal -> no retained handle can open", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.SIGNUP_TERMINAL,
					attempt_id: "att_1",
					generation: 1,
					can_initiate_payment: false,
				})
			),
		});
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow } = makeFlow({ api, openCheckout });
		await flow.submitReview(SIGNUP);
		expect(flow.state.value.value).toBe(STATES.FAILED_TERMINAL);
		expect(flow.state.value.canInitiate).toBe(false);
		// The original pending handle is still in state, but the terminal source
		// state forbids opening it.
		expect(canOpenCheckout(flow.state.value)).toBe(false);
	});

	test("dismiss -> check says reconnect -> reconnect state, no Initiate", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.ACCOUNT_RECONNECT_REQUIRED,
					attempt_id: "att_1",
					generation: 1,
					can_reconnect: true,
					can_initiate_payment: false,
				})
			),
		});
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow } = makeFlow({ api, openCheckout });
		await flow.submitReview(SIGNUP);
		expect(flow.state.value.value).toBe(STATES.RECONNECT);
		expect(flow.state.value.canInitiate).toBe(false);
	});

	test("dismiss -> check returns pending with can_initiate=false -> the old true is replaced", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 1,
					can_initiate_payment: false,
					can_check_status: true,
				})
			),
		});
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow } = makeFlow({ api, openCheckout });
		// start_signup seeded can_initiate_payment:true.
		await flow.submitReview(SIGNUP);
		expect(flow.state.value.canInitiate).toBe(false);
	});

	test("the 'checkout could not open' reason survives as presentation metadata, not as state", async () => {
		const openCheckout = vi.fn(async () => {
			throw new Error("An ad blocker stopped the checkout.");
		});
		const { flow } = makeFlow({ openCheckout });
		await flow.submitReview(SIGNUP);
		// The mandatory check found the intent still pending (default): the state is
		// server truth, and the reason rides in checkoutNote (never a frozen state).
		expect(flow.state.value.value).toBe(STATES.UNKNOWN);
		expect(flow.state.value.checkoutNote).toBe("An ad blocker stopped the checkout.");
	});
});

describe("P0-3: every payment wait is bounded", () => {
	const never = () => new Promise(() => {});

	test("a never-settling start does not strand the page - it reconciles to a checkable unknown", async () => {
		const api = makeApi({ startSignup: vi.fn(never) });
		const { flow } = makeFlow({ api, options: { fetchDeadlineMs: 10 } });
		await flow.submitReview(SIGNUP); // resolves despite the hung start
		expect(flow.state.value.value).toBe(STATES.UNKNOWN);
		expect(flow.state.value.canCheck).toBe(true);
	});

	test("even a total blackout (start AND check hang) still resolves, never a frozen spinner", async () => {
		const api = makeApi({ startSignup: vi.fn(never), checkSignupPaymentStatus: vi.fn(never) });
		const { flow } = makeFlow({ api, options: { fetchDeadlineMs: 10 } });
		await flow.submitReview(SIGNUP);
		// Truth is unknown, but the customer is not trapped on Starting…; the
		// read-only check is offered.
		expect([STATES.UNKNOWN, STATES.REVIEW]).toContain(flow.state.value.value);
		expect(flow.state.value.busy).toBe(null);
	});

	test("a never-settling checkout open times out into a retryable, checked recovery", async () => {
		const openCheckout = vi.fn(never);
		const { flow } = makeFlow({ openCheckout, options: { openDeadlineMs: 10 } });
		await flow.submitReview(SIGNUP);
		expect(flow.state.value.value).not.toBe(STATES.CHECKOUT_OPEN);
		expect(flow.state.value.busy).toBe(null);
	});

	test("a never-settling confirm falls to unknown and reconciles, never to paid", async () => {
		const api = makeApi({ confirmSignupPayment: vi.fn(never) });
		const openCheckout = vi.fn(async () => ({
			status: CHECKOUT_SUCCESS,
			payload: { razorpay_payment_id: "p" },
		}));
		const { flow } = makeFlow({ api, openCheckout, options: { fetchDeadlineMs: 10 } });
		await flow.submitReview(SIGNUP);
		expect(flow.state.value.value).not.toBe(STATES.PAID);
		expect(flow.state.value.busy).toBe(null);
	});
});

describe("P1-2: method-level serialization", () => {
	test("three concurrent submitReview calls -> one start, one sheet", async () => {
		const { flow, api, openCheckout } = makeFlow();
		await Promise.all([
			flow.submitReview(SIGNUP),
			flow.submitReview(SIGNUP),
			flow.submitReview(SIGNUP),
		]);
		expect(api.startSignup).toHaveBeenCalledTimes(1);
		expect(openCheckout).toHaveBeenCalledTimes(1);
	});

	test("three concurrent initiatePayment calls -> one initiate, one sheet", async () => {
		const { flow, api, openCheckout } = makeFlow();
		await Promise.all([
			flow.initiatePayment({ plan: "pro" }),
			flow.initiatePayment({ plan: "pro" }),
			flow.initiatePayment({ plan: "pro" }),
		]);
		expect(api.initiateSignupPayment).toHaveBeenCalledTimes(1);
		expect(openCheckout).toHaveBeenCalledTimes(1);
	});

	test("check vs initiate race -> exactly one legal action runs", async () => {
		const { flow, api } = makeFlow();
		await Promise.all([flow.checkStatus(), flow.initiatePayment({ plan: "pro" })]);
		// The check took the lock first; the initiate was refused - one live action.
		expect(api.checkSignupPaymentStatus).toHaveBeenCalledTimes(1);
		expect(api.initiateSignupPayment).not.toHaveBeenCalled();
	});

	test("verify vs initiate race -> exactly one legal action runs", async () => {
		const { flow, api } = makeFlow();
		await Promise.all([flow.verifyAndContinue(), flow.initiatePayment({ plan: "pro" })]);
		expect(api.getOnboardingState).toHaveBeenCalledTimes(1);
		expect(api.initiateSignupPayment).not.toHaveBeenCalled();
	});
});

describe("P0-2: the safe return from an external checkout", () => {
	test("a Cashfree-mandate redirect leaves checkout_open; returnFromCheckout reconciles it", async () => {
		const openCheckout = vi.fn(async () => ({ status: "redirected", leavesPage: true }));
		const { flow, api } = makeFlow({ openCheckout });
		await flow.submitReview(SIGNUP);
		// The browser left for the gateway - the machine is parked on checkout_open.
		expect(flow.state.value.value).toBe(STATES.CHECKOUT_OPEN);
		// The bfcache/return path: explicit safe exit + server reconcile (default
		// check = pending), never an assumed dismissal.
		const out = await flow.returnFromCheckout();
		expect(out.returned).toBe(true);
		expect(flow.state.value.value).toBe(STATES.UNKNOWN);
		expect(flow.state.value.canCheck).toBe(true);
		expect(api.checkSignupPaymentStatus).toHaveBeenCalledTimes(1);
	});

	test("returnFromCheckout is a no-op when no checkout is open", async () => {
		const { flow } = makeFlow();
		const out = await flow.returnFromCheckout();
		expect(out.returned).toBe(false);
	});
});

describe("P1-3: server-truth-gated restart", () => {
	test("account-exists restart resets to a fresh review", async () => {
		const api = makeApi({
			startSignup: vi.fn(async () =>
				REFUSAL({ code: CODES.ACCOUNT_ALREADY_EXISTS, message: "exists" })
			),
		});
		const { flow } = makeFlow({ api });
		await flow.submitReview(SIGNUP);
		const { reset } = flow.restart();
		expect(reset).toBe(true);
		expect(flow.state.value.value).toBe(STATES.REVIEW);
	});

	test("an authorized-pending state is preserved, not reset", async () => {
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM,
					attempt_id: "att_1",
					generation: 1,
					can_initiate_payment: false,
				})
			),
		});
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow } = makeFlow({ api, openCheckout });
		await flow.submitReview(SIGNUP);
		expect(flow.state.value.value).toBe(STATES.CONFIRM_REQUIRED);
		const { reset } = flow.restart();
		expect(reset).toBe(false);
		expect(flow.state.value.value).toBe(STATES.CONFIRM_REQUIRED);
	});
});

describe("P1-8: leaving Pay invalidates in-flight work", () => {
	test("a late reconcile answer after cancelInFlight cannot move the machine", async () => {
		let resolveCheck;
		const slow = new Promise((r) => (resolveCheck = r));
		const api = makeApi({
			checkSignupPaymentStatus: vi.fn(async () => {
				await slow;
				return ENVELOPE({
					code: CODES.PAYMENT_ALREADY_ACTIVE,
					subscription_status: "Active",
					attempt_id: "att_1",
					generation: 1,
				});
			}),
		});
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const { flow } = makeFlow({ api, openCheckout });
		const paying = flow.submitReview(SIGNUP); // dismiss -> reconcile awaits slow
		await Promise.resolve();
		flow.cancelInFlight(); // the customer left Pay
		resolveCheck();
		await paying;
		// The late "paid" answer belongs to a superseded token: it must not have
		// advanced the machine to paid behind a torn-down/left surface.
		expect(flow.state.value.value).not.toBe(STATES.PAID);
	});
});

describe("P2-3: transition telemetry is PII-free", () => {
	test("no email/company/handle/payment-id reaches the sink, and the shape is right", async () => {
		const events = [];
		const { flow } = makeFlow({
			options: { telemetry: (e) => events.push(e) },
		});
		await flow.submitReview({
			email: "secret@example.com",
			company: "SecretCorp Ltd",
			plan: "pro",
		});
		const blob = JSON.stringify(events);
		expect(blob).not.toContain("secret@example.com");
		expect(blob).not.toContain("SecretCorp");
		expect(blob).not.toContain("order_1");
		expect(blob).not.toContain("pay_1");
		expect(events.some((e) => e.event === "payment_transition")).toBe(true);
		for (const e of events) {
			expect(e.email).toBeUndefined();
			expect(e.company).toBeUndefined();
			expect(e.handles).toBeUndefined();
			if (e.event === "payment_transition") {
				expect(e).toHaveProperty("from");
				expect(e).toHaveProperty("to");
				expect(e).toHaveProperty("elapsed_bucket");
			}
		}
	});
});

describe("P2-5: the support counter survives a refresh", () => {
	test("a persisted (attempt, generation) count is restored on the next mount", async () => {
		const store = new Map();
		const storage = { get: (k) => store.get(k) || null, set: (k, v) => store.set(k, v) };
		const mkApi = () =>
			makeApi({
				checkSignupPaymentStatus: vi.fn(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 1,
						can_check_status: true,
					})
				),
			});
		const openCheckout = vi.fn(async () => ({ status: CHECKOUT_DISMISSED }));
		const flow1 = createPaymentFlow({
			api: mkApi(),
			openCheckout,
			sleep: async () => {},
			now: () => 1_000_000,
			storage,
			strict: true,
		});
		await flow1.submitReview(SIGNUP); // -> UNKNOWN pending att_1/gen1
		for (let i = 0; i < 4; i++) await flow1.checkStatus();
		expect(flow1.state.value.supportOffered).toBe(true);

		// A fresh page (new machine, same storage) hydrating the same intent
		// restores the count instead of resetting the offer.
		const flow2 = createPaymentFlow({
			api: makeApi({
				getOnboardingState: vi.fn(async () =>
					ENVELOPE({
						code: CODES.PAYMENT_CONFIRMATION_PENDING,
						attempt_id: "att_1",
						generation: 1,
						can_check_status: true,
					})
				),
			}),
			openCheckout,
			sleep: async () => {},
			now: () => 1_000_000,
			storage,
			strict: true,
		});
		await flow2.hydrate();
		expect(flow2.state.value.supportOffered).toBe(true);
	});
});

describe("P1-7: resumed identity never falls back to prefill (machine half)", () => {
	test("an answer with no email leaves the summary email blank for the view's placeholder", async () => {
		const api = makeApi({
			getOnboardingState: vi.fn(async () =>
				ENVELOPE({
					code: CODES.ACCOUNT_RECONNECT_REQUIRED,
					attempt_id: "att_1",
					generation: 1,
					can_reconnect: true,
				})
			),
		});
		const { flow } = makeFlow({ api });
		await flow.hydrate();
		expect(flow.state.value.value).toBe(STATES.RECONNECT);
		expect(flow.state.value.summary && flow.state.value.summary.email).toBeFalsy();
	});
});

describe("X1 / B2-1: a sheet that settles AFTER the open deadline", () => {
	const flush = async () => {
		for (let i = 0; i < 80; i++) await Promise.resolve();
	};

	test("a late SUCCESS still confirms exactly once, and initiate is never offered meanwhile", async () => {
		let resolveSheet;
		const openCheckout = vi.fn(() => new Promise((r) => (resolveSheet = r)));
		const { flow, api } = makeFlow({ openCheckout, options: { openDeadlineMs: 10 } });
		const p = flow.submitReview(SIGNUP);
		// Let the open deadline elapse: the sheet is still open, machine enters the
		// checkoutMayBeOpen recovery and reconciles a pending answer.
		await new Promise((r) => setTimeout(r, 30));
		await p;
		expect(flow.state.value.checkoutMayBeOpen).toBe(true);
		expect(flow.state.value.canInitiate).toBe(false);
		expect(api.confirmSignupPayment).not.toHaveBeenCalled();
		// The customer finally pays in the still-open sheet, long after our deadline.
		resolveSheet({ status: CHECKOUT_SUCCESS, payload: { razorpay_payment_id: "pay_late" } });
		await flush();
		// The late result ran the NORMAL confirm path - not dropped - exactly once.
		expect(api.confirmSignupPayment).toHaveBeenCalledTimes(1);
		expect(flow.state.value.value).toBe(STATES.PAID);
	});

	test("a late DISMISS lands on a check-first recovery, clears the veto, never charges", async () => {
		let resolveSheet;
		const openCheckout = vi.fn(() => new Promise((r) => (resolveSheet = r)));
		const { flow, api } = makeFlow({ openCheckout, options: { openDeadlineMs: 10 } });
		const p = flow.submitReview(SIGNUP);
		await new Promise((r) => setTimeout(r, 30));
		await p;
		expect(flow.state.value.checkoutMayBeOpen).toBe(true);
		const checksBefore = api.checkSignupPaymentStatus.mock.calls.length;
		resolveSheet({ status: CHECKOUT_DISMISSED });
		await flush();
		expect(flow.state.value.checkoutMayBeOpen).toBe(false);
		expect(api.checkSignupPaymentStatus.mock.calls.length).toBeGreaterThan(checksBefore);
		expect(api.confirmSignupPayment).not.toHaveBeenCalled();
		expect(flow.state.value.value).not.toBe(STATES.PAID);
	});

	test("a late success is DROPPED after a hard teardown (leaving Pay / unmount)", async () => {
		let resolveSheet;
		const openCheckout = vi.fn(() => new Promise((r) => (resolveSheet = r)));
		const { flow, api } = makeFlow({ openCheckout, options: { openDeadlineMs: 10 } });
		const p = flow.submitReview(SIGNUP);
		await new Promise((r) => setTimeout(r, 30));
		await p;
		flow.cancelInFlight(); // the page tore down: the late result must be abandoned
		resolveSheet({ status: CHECKOUT_SUCCESS, payload: { razorpay_payment_id: "pay_late" } });
		await flush();
		expect(api.confirmSignupPayment).not.toHaveBeenCalled();
	});

	test("a benign Check taken after the timeout does NOT drop the later real payment", async () => {
		let resolveSheet;
		const openCheckout = vi.fn(() => new Promise((r) => (resolveSheet = r)));
		const { flow, api } = makeFlow({ openCheckout, options: { openDeadlineMs: 10 } });
		const p = flow.submitReview(SIGNUP);
		await new Promise((r) => setTimeout(r, 30));
		await p;
		// The customer, waiting, taps Check (a benign action - it bumps the action
		// token). The late payment must still confirm despite that bump.
		await flow.checkStatus();
		resolveSheet({ status: CHECKOUT_SUCCESS, payload: { razorpay_payment_id: "pay_late" } });
		await flush();
		expect(api.confirmSignupPayment).toHaveBeenCalledTimes(1);
		expect(flow.state.value.value).toBe(STATES.PAID);
	});
});

describe("X5 / B2-6: a frozen checkout_open with no live opener", () => {
	test("hydrate exits it safely instead of trapping the customer forever", async () => {
		let resolveSheet;
		const openCheckout = vi.fn(() => new Promise((r) => (resolveSheet = r)));
		const { flow } = makeFlow({ openCheckout });
		const p = flow.submitReview(SIGNUP);
		for (let i = 0; i < 80 && flow.state.value.value !== STATES.CHECKOUT_OPEN; i++) {
			await Promise.resolve();
		}
		expect(flow.state.value.value).toBe(STATES.CHECKOUT_OPEN);
		// The opener is abandoned (leaving Pay / unmount): sheet gone, state frozen.
		flow.cancelInFlight();
		const out = await flow.hydrate();
		expect(out.superseded).not.toBe(true);
		expect(flow.state.value.value).not.toBe(STATES.CHECKOUT_OPEN);
		// Clean up the abandoned opener so the open-deadline timer does not linger.
		resolveSheet({ status: CHECKOUT_DISMISSED });
		await p;
	});

	test("a LIVE sheet is still protected (the normal refusal is not weakened)", async () => {
		let releaseSheet;
		const openCheckout = vi.fn(() => new Promise((r) => (releaseSheet = r)));
		const { flow } = makeFlow({ openCheckout });
		const p = flow.submitReview(SIGNUP);
		for (let i = 0; i < 80 && flow.state.value.value !== STATES.CHECKOUT_OPEN; i++) {
			await Promise.resolve();
		}
		expect(flow.state.value.value).toBe(STATES.CHECKOUT_OPEN);
		// No cancel: the opener is genuinely live, so hydrate must still refuse.
		const out = await flow.hydrate();
		expect(out.superseded).toBe(true);
		expect(flow.state.value.value).toBe(STATES.CHECKOUT_OPEN);
		releaseSheet({ status: CHECKOUT_DISMISSED });
		await p;
	});
});

describe("bench re-probe: late-continuation vs recovery actions", () => {
	const flush = async () => {
		for (let i = 0; i < 80; i++) await Promise.resolve();
	};

	test("D3: a REFUSED restart preserves the late continuation - a later success still confirms once", async () => {
		let resolveSheet;
		const openCheckout = vi.fn(() => new Promise((r) => (resolveSheet = r)));
		// After the timeout, the reconcile answers a plain PENDING (the makeApi
		// default) - a state that is NOT restart-safe, so "Start again" is refused.
		const { flow, api } = makeFlow({ openCheckout, options: { openDeadlineMs: 10 } });
		const p = flow.submitReview(SIGNUP);
		await new Promise((r) => setTimeout(r, 30));
		await p;
		expect(flow.state.value.checkoutMayBeOpen).toBe(true);
		// The customer taps "Start again" while the sheet is still open. Money may be
		// recoverable, so it is REFUSED (the customer stays on recovery).
		const { reset } = flow.restart();
		expect(reset).toBe(false);
		// The still-open sheet finally succeeds AFTER the refused restart. Pre-fix,
		// restart's UNCONDITIONAL cancelInFlight bumped disposeEpoch and fenced this
		// continuation out entirely (0 confirms - the money was lost). Post-fix it runs.
		resolveSheet({ status: CHECKOUT_SUCCESS, payload: { razorpay_payment_id: "pay_late" } });
		await flush();
		expect(api.confirmSignupPayment).toHaveBeenCalledTimes(1);
		expect(flow.state.value.value).toBe(STATES.PAID);
	});

	test("D6: a late sheet FAILURE lifts the may-be-open veto instead of latching it forever", async () => {
		let rejectSheet;
		const openCheckout = vi.fn(() => new Promise((_res, rej) => (rejectSheet = rej)));
		const { flow, api } = makeFlow({ openCheckout, options: { openDeadlineMs: 10 } });
		const p = flow.submitReview(SIGNUP);
		await new Promise((r) => setTimeout(r, 30));
		await p;
		expect(flow.state.value.checkoutMayBeOpen).toBe(true);
		const checksBefore = api.checkSignupPaymentStatus.mock.calls.length;
		// The still-open sheet finally FAILS (an SDK error) long after our deadline.
		rejectSheet(new Error("sdk exploded late"));
		await flush();
		// Pre-fix the rejection handler only cleared openInFlight and never fired
		// CHECKOUT_SHEET_CLOSED, so checkoutMayBeOpen latched true forever - canInitiate
		// dead, the only escape a full reload. Post-fix the veto lifts and a check runs.
		expect(flow.state.value.checkoutMayBeOpen).toBe(false);
		expect(api.checkSignupPaymentStatus.mock.calls.length).toBeGreaterThan(checksBefore);
		expect(api.confirmSignupPayment).not.toHaveBeenCalled();
	});

	test("D7: a late success at confirm_required completes the confirm, not an illegal limbo", async () => {
		let resolveSheet;
		const openCheckout = vi.fn(() => new Promise((r) => (resolveSheet = r)));
		const api = makeApi({
			// After the timeout, a status Check resolves the intent to authorized-
			// pending-confirm (a very likely real answer) -> state confirm_required.
			checkSignupPaymentStatus: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_AUTHORIZED_PENDING_CONFIRM,
					attempt_id: "att_1",
					generation: 1,
					can_initiate_payment: false,
				})
			),
			confirmSignupPayment: vi.fn(async () =>
				ENVELOPE({
					code: CODES.PAYMENT_ALREADY_ACTIVE,
					subscription_status: "Active",
					attempt_id: "att_1",
					generation: 1,
				})
			),
		});
		// strict:false to observe PRODUCTION behaviour (illegal transitions counted,
		// not thrown) - the exact shape the probe measured.
		const { flow } = makeFlow({
			api,
			openCheckout,
			options: { openDeadlineMs: 10, strict: false },
		});
		const p = flow.submitReview(SIGNUP);
		await new Promise((r) => setTimeout(r, 30));
		await p;
		expect(flow.state.value.value).toBe(STATES.CONFIRM_REQUIRED);
		// The still-open sheet finally succeeds, landing at confirm_required. Pre-fix
		// GATEWAY_CALLBACK and CONFIRM_SUCCEEDED were BOTH illegal from there: the money
		// was confirmed (1 call) but 2 illegal transitions accrued and the customer was
		// stranded on the pending card. Post-fix it drives confirm -> paid cleanly.
		resolveSheet({ status: CHECKOUT_SUCCESS, payload: { razorpay_payment_id: "pay_late" } });
		await flush();
		expect(api.confirmSignupPayment).toHaveBeenCalledTimes(1);
		expect(flow.state.value.value).toBe(STATES.PAID);
		expect(flow.state.value.illegalTransitions).toBe(0);
	});
});
