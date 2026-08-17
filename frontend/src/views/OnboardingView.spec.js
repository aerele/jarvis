// The onboarding wizard's VIEW half - the paths that live in OnboardingView.vue
// itself, not in the reducer or the orchestrator (those have their own specs).
// This is the first spec to mount the view; it exists for the bench re-probe's
// view-level gaps: provider discovery loading (X4) and fail-closed narrowing
// (D10), the stale checkout-marker mount clear (X3), the defensive reconnect
// identity fields (X7), and the refused-restart note (X8).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

import { CHECKOUT_NAV_KEY } from "@/onboarding/checkoutNav";
import { STATES } from "@/onboarding/paymentMachine";
import { ACTIONS } from "@/onboarding/paymentCodes";
import { toast } from "frappe-ui";

// A raw {status, body} envelope, exactly what the flow's codec decodes.
const ENVELOPE = (data, over = {}) => ({
	status: 200,
	body: { message: { ok: true, contract_version: 2, data, context: {}, ...over } },
});

const api = vi.hoisted(() => ({
	isReadyForChat: vi.fn(async () => ({ ready: false, reason: "" })),
	getLlmSyncStatus: vi.fn(async () => ({})),
	listPlans: vi.fn(async () => []),
	listPaymentProviders: vi.fn(async () => ({
		providers: ["razorpay", "cashfree"],
		default: "razorpay",
	})),
	reconnectAvailable: vi.fn(async () => ({ available: false })),
	startAccountReconnect: vi.fn(async () => ({})),
	checkAccountReconnect: vi.fn(async () => ({})),
	redeemReconnectCode: vi.fn(async () => ({})),
	getAccountDefaults: vi.fn(async () => ({})),
	captureOnboardingLead: vi.fn(async () => ({ ok: true })),
	getTermsUrl: vi.fn(async () => ({ url: "" })),
	onboardingPaymentApi: {
		getOnboardingState: vi.fn(async () => ENVELOPE({ code: "BENCH_NO_SIGNUP_CONTEXT" })),
		startSignup: vi.fn(),
		initiateSignupPayment: vi.fn(),
		checkSignupPaymentStatus: vi.fn(async () =>
			ENVELOPE({ code: "PAYMENT_CONFIRMATION_PENDING" })
		),
		confirmSignupPayment: vi.fn(),
		syncConnection: vi.fn(async () => ({ synced: false })),
		resendVerification: vi.fn(),
	},
}));
vi.mock("@/api", () => api);
// The connect controller (plan-05) navigates via useRouter; stub it so mounting
// the view here does not warn about a missing router injection.
vi.mock("vue-router", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("frappe-ui", () => ({
	Button: { name: "Button", template: "<button><slot /></button>" },
	FormControl: { name: "FormControl", template: "<input />" },
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: '<div v-if="message">{{ message }}</div>',
	},
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
	Checkbox: {
		name: "Checkbox",
		props: ["modelValue", "label", "id"],
		emits: ["update:modelValue"],
		template:
			'<input type="checkbox" :id="id" :checked="!!modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
	},
	call: vi.fn(),
	dayjs: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	toast: { error: vi.fn(), success: vi.fn() },
}));

import OnboardingView from "./OnboardingView.vue";

const STUBS = {
	LlmPoolEditor: true,
	JvCombo: true,
	JvSpinner: true,
	JarvisMark: true,
	Banner: true,
	TourIntro: true,
	SetupNeuralNet: true,
};

function mountView() {
	return mount(OnboardingView, { global: { stubs: STUBS } });
}

beforeEach(() => {
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent() {},
	});
	// Reset the mutable mocks to their happy defaults.
	api.listPaymentProviders.mockResolvedValue({
		providers: ["razorpay", "cashfree"],
		default: "razorpay",
	});
	api.onboardingPaymentApi.getOnboardingState.mockResolvedValue(
		ENVELOPE({ code: "BENCH_NO_SIGNUP_CONTEXT" })
	);
	try {
		window.sessionStorage.clear();
	} catch (e) {
		/* jsdom */
	}
});
afterEach(() => {
	vi.clearAllMocks();
});

describe("X4: provider discovery loading state", () => {
	it("providersLoading is true while discovery is in flight and false once it resolves", async () => {
		let resolveProviders;
		api.listPaymentProviders.mockReturnValue(new Promise((r) => (resolveProviders = r)));
		const wrapper = mountView();
		// loadPaymentProviders is fired (not awaited) in onMounted and flips the flag
		// true synchronously before its first await.
		expect(wrapper.vm.state.providersLoading).toBe(true);
		resolveProviders({ providers: ["razorpay", "cashfree"], default: "razorpay" });
		await flushPromises();
		expect(wrapper.vm.state.providersLoading).toBe(false);
		expect(wrapper.vm.state.availableProviders).toEqual(["razorpay", "cashfree"]);
	});
});

describe("D10 (view): provider discovery is narrowed to known families and fails closed", () => {
	it("drops an unknown third gateway and keeps only the known ones", async () => {
		api.listPaymentProviders.mockResolvedValue({
			providers: ["stripe", "razorpay", "cashfree"],
			default: "stripe",
		});
		const wrapper = mountView();
		await flushPromises();
		expect(wrapper.vm.state.availableProviders).toEqual(["razorpay", "cashfree"]);
		// The default was the unknown gateway; the preselect must fall to a known one.
		expect(["razorpay", "cashfree"]).toContain(wrapper.vm.state.paymentProvider);
		expect(wrapper.vm.state.providersError).toBe(false);
	});

	it("an unknown-ONLY answer fails closed (no provider, error surfaced)", async () => {
		api.listPaymentProviders.mockResolvedValue({ providers: ["stripe"], default: "stripe" });
		const wrapper = mountView();
		await flushPromises();
		expect(wrapper.vm.state.availableProviders).toEqual([]);
		expect(wrapper.vm.state.paymentProvider).toBe("");
		expect(wrapper.vm.state.providersError).toBe(true);
	});

	it("the Secured-by label is empty for anything not a known gateway (never defaults to Razorpay)", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.paymentProvider = "stripe";
		await flushPromises();
		expect(wrapper.vm.securedProviderLabel).toBe("");
		wrapper.vm.state.paymentProvider = "cashfree";
		await flushPromises();
		expect(wrapper.vm.securedProviderLabel).toBe("Cashfree");
	});
});

describe("X3 (view-half): a stale external-checkout marker is cleared on mount", () => {
	it("a leftover marker is dropped when the mount is not mid-sheet", async () => {
		window.sessionStorage.setItem(CHECKOUT_NAV_KEY, "att_from_a_previous_attempt");
		const wrapper = mountView();
		await flushPromises();
		// Fresh mount lands on the intro (not checkout_open/confirming), so the stale
		// marker must be gone - it can no longer drive a returnFromCheckout into a
		// later live sheet.
		expect(wrapper.vm.pay.value).not.toBe(STATES.CHECKOUT_OPEN);
		expect(window.sessionStorage.getItem(CHECKOUT_NAV_KEY)).toBeNull();
	});
});

describe("X7: defensive reconnect identity fields", () => {
	it("reconnectNeedsIdentity is true when reconnect is offered but no identity exists", async () => {
		api.onboardingPaymentApi.getOnboardingState.mockResolvedValue(
			ENVELOPE({
				code: "ACCOUNT_RECONNECT_REQUIRED",
				attempt_id: "att_1",
				generation: 1,
				can_reconnect: true,
			})
		);
		const wrapper = mountView();
		await flushPromises();
		expect(wrapper.vm.pay.value).toBe(STATES.RECONNECT);
		expect(wrapper.vm.reconnectNeedsIdentity).toBe(true);
	});

	it("reconnectNeedsIdentity is false when server truth carries an identity", async () => {
		api.onboardingPaymentApi.getOnboardingState.mockResolvedValue(
			ENVELOPE({
				code: "ACCOUNT_RECONNECT_REQUIRED",
				attempt_id: "att_1",
				generation: 1,
				can_reconnect: true,
				email: "known@example.com",
				company: "Known Co",
			})
		);
		const wrapper = mountView();
		await flushPromises();
		expect(wrapper.vm.pay.value).toBe(STATES.RECONNECT);
		expect(wrapper.vm.reconnectNeedsIdentity).toBe(false);
	});
});

describe("operator-issued reconnect code: direct redeem vs emailed request", () => {
	it("enterReconnectDirect opens the code screen in direct mode WITHOUT mailing a request", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.enterReconnectDirect();
		expect(wrapper.vm.state.step).toBe("reconnect");
		expect(wrapper.vm.state.reconnectDirect).toBe(true);
		// A support code already exists out of band - never ask admin to mail one.
		expect(api.startAccountReconnect).not.toHaveBeenCalled();
	});

	it("direct-mode submit redeems the code WITH the email, never the request poll", async () => {
		api.redeemReconnectCode.mockResolvedValue({ status: "connected" });
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "reconnect";
		wrapper.vm.state.reconnectDirect = true;
		wrapper.vm.state.reconnectCode = "ABCD2345";
		wrapper.vm.state.reconnectEmail = "known@example.com";
		await wrapper.vm.submitReconnectCode();
		expect(api.redeemReconnectCode).toHaveBeenCalledWith("ABCD2345", "known@example.com");
		expect(api.checkAccountReconnect).not.toHaveBeenCalled();
	});

	it("a lapsed (renew_payment) landing hands off to the billing page, not Connect", async () => {
		// The v1 fix for the shipped-gate strand: an Expired account's container was stopped on
		// expiry, so the code re-authenticates but there is nothing to connect to. The bench must
		// hand off to /jarvis/billing (the renew surface), never advance to Connect and strand.
		const assign = vi.fn();
		const realLocation = window.location;
		Object.defineProperty(window, "location", {
			configurable: true,
			value: { ...realLocation, assign },
		});
		api.redeemReconnectCode.mockResolvedValue({ status: "renew_payment" });
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "reconnect";
		wrapper.vm.state.reconnectDirect = true;
		wrapper.vm.state.reconnectCode = "ABCD2345";
		wrapper.vm.state.reconnectEmail = "known@example.com";
		await wrapper.vm.submitReconnectCode();
		expect(assign).toHaveBeenCalledWith("/jarvis/billing");
		expect(wrapper.vm.state.step).not.toBe("connect");
		Object.defineProperty(window, "location", { configurable: true, value: realLocation });
	});

	it("request-mode submit polls the started request, never the direct redeem", async () => {
		api.checkAccountReconnect.mockResolvedValue({ status: "connected" });
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "reconnect";
		wrapper.vm.state.reconnectDirect = false;
		wrapper.vm.state.reconnectRequestId = "rid-1";
		wrapper.vm.state.reconnectCode = "ABCD2345";
		await wrapper.vm.submitReconnectCode();
		expect(api.checkAccountReconnect).toHaveBeenCalledWith("rid-1", "ABCD2345");
		expect(api.redeemReconnectCode).not.toHaveBeenCalled();
	});

	it("direct-mode submit is blocked until the email is supplied (the 2nd factor)", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "reconnect";
		wrapper.vm.state.reconnectDirect = true;
		wrapper.vm.state.reconnectCode = "ABCD2345";
		wrapper.vm.state.reconnectEmail = "   ";
		await wrapper.vm.submitReconnectCode();
		expect(api.redeemReconnectCode).not.toHaveBeenCalled();
	});

	it("a direct-mode invalid shows the code-or-email copy, never the confirmation-page one", async () => {
		api.redeemReconnectCode.mockResolvedValue({ status: "invalid" });
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "reconnect";
		wrapper.vm.state.reconnectDirect = true;
		wrapper.vm.state.reconnectCode = "ABCD2345";
		wrapper.vm.state.reconnectEmail = "known@example.com";
		await wrapper.vm.submitReconnectCode();
		// A support-code customer never saw a confirmation page - the copy must not send them there.
		expect(wrapper.vm.state.payErr).not.toContain("confirmation page");
		expect(wrapper.vm.state.payErr.toLowerCase()).toContain("email");
	});

	it("startReconnect (emailed path) clears direct-mode state so it can't leak in", async () => {
		const wrapper = mountView();
		await flushPromises();
		// Simulate a prior direct attempt leaving residue.
		wrapper.vm.state.reconnectDirect = true;
		wrapper.vm.state.reconnectEmail = "stale@example.com";
		await wrapper.vm.startReconnect();
		expect(wrapper.vm.state.reconnectDirect).toBe(false);
		expect(wrapper.vm.state.reconnectEmail).toBe("");
	});
});

describe("X8: a refused restart explains itself instead of a silent no-op", () => {
	it("sets restartHeldNote when restart is refused, and clears it on the next state change", async () => {
		api.onboardingPaymentApi.getOnboardingState.mockResolvedValue(
			ENVELOPE({
				code: "PAYMENT_AUTHORIZED_PENDING_CONFIRM",
				attempt_id: "att_1",
				generation: 1,
				can_initiate_payment: false,
			})
		);
		const wrapper = mountView();
		await flushPromises();
		expect(wrapper.vm.pay.value).toBe(STATES.CONFIRM_REQUIRED);
		// "Start again" here is refused (a payment is still recoverable) - it must say
		// so rather than appear to do nothing.
		await wrapper.vm.onPayAction(ACTIONS.RESTART);
		expect(wrapper.vm.restartHeldNote).toBeTruthy();
		expect(wrapper.vm.state.step).not.toBe("details");
		// A later real state change drops the note so it never lingers past the state
		// it explained: a check that lands paid moves the machine off confirm_required.
		api.onboardingPaymentApi.checkSignupPaymentStatus.mockResolvedValue(
			ENVELOPE({
				code: "PAYMENT_ALREADY_ACTIVE",
				subscription_status: "Active",
				attempt_id: "att_1",
				generation: 1,
			})
		);
		await wrapper.vm.flow.checkStatus();
		await flushPromises();
		expect(wrapper.vm.restartHeldNote).toBe("");
	});
});

// ---------------------------------------------------------------------------
// jarvis#297 P0-2a: the email-verification dead end - resend + change email.
// ---------------------------------------------------------------------------
describe("jarvis#297 P0-2a: verification is no longer a dead end", () => {
	async function mountOnVerify(overrides = {}) {
		api.onboardingPaymentApi.getOnboardingState.mockResolvedValue(
			ENVELOPE({
				code: "SIGNUP_VERIFICATION_REQUIRED",
				pending_verification: true,
				attempt_id: "att_1",
				generation: 0,
				email: "typo@exmaple.com",
				...overrides,
			})
		);
		const wrapper = mountView();
		await flushPromises();
		expect(wrapper.vm.pay.value).toBe(STATES.VERIFICATION_REQUIRED);
		return wrapper;
	}

	it("RESEND is absent until the server grants can_resend_verification", async () => {
		const wrapper = await mountOnVerify();
		expect(wrapper.vm.verifyActions).toEqual([ACTIONS.VERIFY, ACTIONS.RESTART]);
		expect(wrapper.vm.pay.canResendVerification).toBe(false);
	});

	it("RESEND appears once granted, sends, then disables itself for the cooldown", async () => {
		const wrapper = await mountOnVerify({ can_resend_verification: true });
		expect(wrapper.vm.verifyActions).toEqual([
			ACTIONS.VERIFY,
			ACTIONS.RESEND,
			ACTIONS.RESTART,
		]);
		expect(wrapper.vm.payActionDisabled(ACTIONS.RESEND)).toBe(false);

		api.onboardingPaymentApi.resendVerification.mockResolvedValue(
			ENVELOPE({
				code: "SIGNUP_VERIFICATION_REQUIRED",
				pending_verification: true,
				attempt_id: "att_1",
				generation: 0,
				can_resend_verification: true,
			})
		);
		await wrapper.vm.onPayAction(ACTIONS.RESEND);
		await flushPromises();

		expect(api.onboardingPaymentApi.resendVerification).toHaveBeenCalledTimes(1);
		// Truthful confirmation, and the button cannot be spammed while it cools.
		expect(wrapper.vm.resendNote).toBe("We sent a new link to typo@exmaple.com.");
		expect(wrapper.vm.payActionDisabled(ACTIONS.RESEND)).toBe(true);
	});

	it("a failed resend claims nothing sent and starts no cooldown", async () => {
		const wrapper = await mountOnVerify({ can_resend_verification: true });
		api.onboardingPaymentApi.resendVerification.mockResolvedValue({
			status: 0,
			body: null,
			networkError: true,
		});
		await wrapper.vm.onPayAction(ACTIONS.RESEND);
		await flushPromises();

		expect(wrapper.vm.resendNote).toBe("");
		expect(wrapper.vm.payActionDisabled(ACTIONS.RESEND)).toBe(false);
	});

	it("'Use a different email' walks back to Details, ready to re-request verification", async () => {
		const wrapper = await mountOnVerify();
		expect(wrapper.vm.payActionLabel(ACTIONS.RESTART)).toBe("Use a different email");
		await wrapper.vm.onPayAction(ACTIONS.RESTART);
		await flushPromises();

		expect(wrapper.vm.pay.value).toBe(STATES.REVIEW);
		expect(wrapper.vm.state.step).toBe("details");
		// The mistyped address the customer typed is still there to correct, not
		// wiped along with the machine.
		expect(wrapper.vm.state.email).toBe("typo@exmaple.com");

		// Re-submitting a corrected address re-requests verification exactly like
		// the first attempt did - the existing signup path, unchanged.
		api.onboardingPaymentApi.startSignup.mockResolvedValue(
			ENVELOPE({
				code: "SIGNUP_VERIFICATION_REQUIRED",
				pending_verification: true,
				attempt_id: "att_fixed",
				generation: 0,
				email: "real@example.com",
			})
		);
		wrapper.vm.state.email = "real@example.com";
		wrapper.vm.state.company = "Acme";
		wrapper.vm.state.planName = "pro";
		await wrapper.vm.flow.submitReview({
			email: "real@example.com",
			company: "Acme",
			plan: "pro",
		});
		await flushPromises();
		expect(wrapper.vm.pay.value).toBe(STATES.VERIFICATION_REQUIRED);
	});

	it("VERIFY still works unchanged", async () => {
		const wrapper = await mountOnVerify();
		expect(wrapper.vm.payActionLabel(ACTIONS.VERIFY)).toBe("I've verified my email");
		api.onboardingPaymentApi.getOnboardingState.mockResolvedValue(
			ENVELOPE({ code: "SIGNUP_VERIFICATION_REQUIRED", pending_verification: true })
		);
		await wrapper.vm.onPayAction(ACTIONS.VERIFY);
		await flushPromises();
		// verifyAndContinue re-reads state exactly as it did before this change.
		expect(api.onboardingPaymentApi.getOnboardingState).toHaveBeenCalled();
		expect(wrapper.vm.pay.value).toBe(STATES.VERIFICATION_REQUIRED);
	});
});

// ---------------------------------------------------------------------------
// GST tax breakdown (2026-08): Review & pay card's Subtotal/GST/Total rows.
// ---------------------------------------------------------------------------
describe("GST tax breakdown: Review & pay Subtotal/GST/Total rows", () => {
	it("a trial Standard plan with gst_percent 18 shows the base/GST/total split and a ₹0 due-today", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.plans = [
			{
				name: "standard",
				plan_name: "Standard",
				price_inr: 3500,
				gst_percent: 18,
				billing_cycle: "Monthly",
				trial_days: 7,
				signup_fee_inr: 0,
			},
		];
		wrapper.vm.state.planName = "standard";
		wrapper.vm.state.company = "Acme";
		wrapper.vm.state.email = "acme@example.com";
		wrapper.vm.state.step = "pay";
		await flushPromises();

		// The default pay machine state is STATES.REVIEW (initialState()), so a fresh
		// mount with none of the paid/verify/confirming/busy/recovery/maintenance
		// branches triggered lands on the Review & pay summary card.
		expect(wrapper.vm.pay.value).toBe(STATES.REVIEW);
		expect(wrapper.vm.pricing).toEqual({
			subtotal: 3500,
			gstPercent: 18,
			gstAmount: 630,
			total: 4130,
		});
		expect(wrapper.vm.dueTodayLabel).toBe("₹0 today · then ₹4,130/mo after 7 days");

		const text = wrapper.text();
		const html = wrapper.html();
		expect(text).toContain("Subtotal");
		expect(text).toContain("GST (18%)");
		expect(text).toContain("₹630");
		// A plain toContain("Total") would also pass if the Total row went missing,
		// since "Subtotal" and "₹4,130" (from the due-today label) are already on
		// the page - match the row's own span so it verifies the row itself renders.
		expect(html).toMatch(/<span[^>]*class="text-ink-gray-5">Total<\/span>/);
		expect(text).toContain("₹4,130");
		expect(text).toContain("Due today");
		expect(text).toContain("₹0 today");
	});

	it("a plan without gst_percent renders no Subtotal/GST/Total rows", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.plans = [
			{
				name: "legacy",
				plan_name: "Legacy",
				price_inr: 3500,
				billing_cycle: "Monthly",
				trial_days: 0,
				signup_fee_inr: 0,
			},
		];
		wrapper.vm.state.planName = "legacy";
		wrapper.vm.state.company = "Acme";
		wrapper.vm.state.email = "acme@example.com";
		wrapper.vm.state.step = "pay";
		await flushPromises();

		expect(wrapper.vm.pay.value).toBe(STATES.REVIEW);
		expect(wrapper.vm.pricing.gstPercent).toBe(0);
		const text = wrapper.text();
		expect(text).not.toContain("Subtotal");
		expect(text).not.toContain("GST (");
	});

	// #10-e: gstAmount/total can be a genuine fraction (price_inr=3475,
	// gst_percent=18 -> gstAmount=625.5, total=4100.5), and the backend charges
	// exactly that paise-precise value (to_paise -> 410050). The Review card
	// must render the SAME value it will be charged - inrExact, not inr()'s
	// bare one-decimal "₹4,100.5" and never a rounded-off "₹4,101".
	it("a fractional GST split renders its exact paise-precise value on every row", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.plans = [
			{
				name: "standard",
				plan_name: "Standard",
				price_inr: 3475,
				gst_percent: 18,
				billing_cycle: "Monthly",
				trial_days: 0,
				signup_fee_inr: 0,
			},
		];
		wrapper.vm.state.planName = "standard";
		wrapper.vm.state.company = "Acme";
		wrapper.vm.state.email = "acme@example.com";
		wrapper.vm.state.step = "pay";
		await flushPromises();

		expect(wrapper.vm.pricing).toEqual({
			subtotal: 3475,
			gstPercent: 18,
			gstAmount: 625.5,
			total: 4100.5,
		});
		expect(wrapper.vm.dueTodayLabel).toBe("₹4,100.50");

		const text = wrapper.text();
		expect(text).toContain("₹3,475");
		expect(text).toContain("₹625.50");
		expect(text).toContain("₹4,100.50");
		// Neither the collapsed one-decimal form nor a rounded integer may appear.
		expect(text).not.toContain("₹625.5 ");
		expect(text).not.toContain("₹4,101");
	});
});

// ---------------------------------------------------------------------------
// #10-e review: the plan-selection card's "excl. GST" caveat must track the
// plan's own gst_percent, not render unconditionally.
// ---------------------------------------------------------------------------
describe("Plan step: excl. GST label tracks the plan's own gst_percent", () => {
	async function mountOnPlanStep(plan) {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.plans = [plan];
		wrapper.vm.state.step = "plan";
		await flushPromises();
		return wrapper;
	}

	it("is absent when gst_percent is undefined (pre-companion-PR get_plans row)", async () => {
		const wrapper = await mountOnPlanStep({
			name: "legacy",
			plan_name: "Legacy",
			price_inr: 3999,
			billing_cycle: "Monthly",
		});
		expect(wrapper.text()).not.toContain("excl. GST");
	});

	it("is absent when gst_percent is 0", async () => {
		const wrapper = await mountOnPlanStep({
			name: "zero-gst",
			plan_name: "Zero GST",
			price_inr: 3999,
			billing_cycle: "Monthly",
			gst_percent: 0,
		});
		expect(wrapper.text()).not.toContain("excl. GST");
	});

	it("is present when gst_percent is a positive number", async () => {
		const wrapper = await mountOnPlanStep({
			name: "standard",
			plan_name: "Standard",
			price_inr: 3999,
			billing_cycle: "Monthly",
			gst_percent: 18,
		});
		expect(wrapper.text()).toContain("excl. GST");
	});
});

describe("B3: gateway picker shows on trial plans", () => {
	it("a trial plan with one available provider still renders the gateway chooser", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.plans = [
			{
				name: "standard",
				plan_name: "Standard",
				price_inr: 3500,
				gst_percent: 18,
				billing_cycle: "Monthly",
				trial_days: 7,
				signup_fee_inr: 0,
			},
		];
		wrapper.vm.state.planName = "standard";
		wrapper.vm.state.company = "Acme";
		wrapper.vm.state.email = "acme@example.com";
		wrapper.vm.state.availableProviders = ["razorpay"];
		wrapper.vm.state.paymentProvider = "razorpay";
		wrapper.vm.state.step = "pay";
		await flushPromises();

		expect(wrapper.vm.pay.value).toBe(STATES.REVIEW);
		expect(wrapper.vm.isTrialPlan).toBe(true);
		expect(wrapper.vm.showProviderChooser).toBe(true);
		expect(wrapper.find(".ob-provseg").exists()).toBe(true);
		expect(wrapper.find('[aria-label="Payment method: Razorpay"]').exists()).toBe(true);
	});
});

describe("812: Company field is constrained once ERPNext has real Company records", () => {
	it("prefillAccount captures erpnext_installed and companies from the server", async () => {
		api.getAccountDefaults.mockResolvedValue({
			email: "",
			company: "Acme",
			companies: ["Acme", "Beta"],
			erpnext_installed: true,
		});
		const wrapper = mountView();
		await flushPromises();
		expect(wrapper.vm.state.erpnextInstalled).toBe(true);
		expect(wrapper.vm.state.companies).toEqual(["Acme", "Beta"]);
	});

	it("ERPNext installed with Company records: the field is a constrained picker", async () => {
		api.getAccountDefaults.mockResolvedValue({
			email: "",
			company: "",
			companies: ["Acme", "Beta"],
			erpnext_installed: true,
		});
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "details";
		await flushPromises();
		const combo = wrapper.find("jv-combo-stub");
		expect(combo.attributes("allowcustom")).toBe("false");
	});

	it("ERPNext absent: the field stays free text even with no companies", async () => {
		api.getAccountDefaults.mockResolvedValue({
			email: "",
			company: "",
			companies: [],
			erpnext_installed: false,
		});
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "details";
		await flushPromises();
		const combo = wrapper.find("jv-combo-stub");
		expect(combo.attributes("allowcustom")).toBe("true");
	});

	it("ERPNext installed but zero Company rows: the field stays free text (no dead end)", async () => {
		api.getAccountDefaults.mockResolvedValue({
			email: "",
			company: "",
			companies: [],
			erpnext_installed: true,
		});
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "details";
		await flushPromises();
		const combo = wrapper.find("jv-combo-stub");
		expect(combo.attributes("allowcustom")).toBe("true");
	});
});

// Forced-reconnect gate: an eligible returning (email, company) may only reconnect,
// keyed on the customer-ASSERTED identity (identityFromUser), never the admin prefill.
describe("Returning-customer forced reconnect gate", () => {
	async function detailsView({
		eligible,
		needs_company = false,
		typed = true,
		company = "Corp",
	}) {
		api.reconnectAvailable.mockResolvedValue({ eligible, needs_company });
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "details";
		wrapper.vm.state.email = "back@corp.test";
		wrapper.vm.state.company = company;
		wrapper.vm.state.identityFromUser = typed; // typed => reconnectIdentity present
		// Contact number is mandatory on Details now; fill it so onDetailsSubmit's
		// gate doesn't block these reconnect-flow tests on an unrelated field.
		wrapper.vm.billing.setUserValue("contact", "+91 98765 43210");
		// The required T&C checkbox now lives on Details too, but these tests are
		// about the reconnect gate specifically, so tick it by default (a
		// dedicated test below asserts the reconnect branch does NOT need it).
		wrapper.vm.state.termsAccepted = true;
		await flushPromises();
		return wrapper;
	}

	it("C1: forces reconnect (no goNext to plan) for an eligible account matching the typed identity", async () => {
		api.startAccountReconnect.mockResolvedValue({ request: "req_1" });
		const wrapper = await detailsView({ eligible: true });
		await wrapper.vm.onDetailsSubmit();
		await flushPromises();
		// The await inside onDetailsSubmit resolved eligibility (it was NOT pre-settled
		// by the debounce) - this is also the race-closure guarantee (C3).
		expect(api.startAccountReconnect).toHaveBeenCalledWith("back@corp.test", "Corp");
		expect(wrapper.vm.state.step).toBe("reconnect");
	});

	it("the required T&C checkbox does NOT gate the reconnect branch - a returning customer reconnecting an existing paid account never went through it before", async () => {
		api.startAccountReconnect.mockResolvedValue({ request: "req_1" });
		const wrapper = await detailsView({ eligible: true });
		wrapper.vm.state.termsAccepted = false; // deliberately unticked
		await wrapper.vm.onDetailsSubmit();
		await flushPromises();
		expect(api.startAccountReconnect).toHaveBeenCalledWith("back@corp.test", "Corp");
		expect(wrapper.vm.state.step).toBe("reconnect");
	});

	it("C1: does NOT force reconnect when the eligible email was only prefilled (not typed)", async () => {
		const wrapper = await detailsView({ eligible: true, typed: false });
		await wrapper.vm.onDetailsSubmit();
		await flushPromises();
		expect(api.startAccountReconnect).not.toHaveBeenCalled();
		expect(wrapper.vm.state.step).toBe("plan");
	});

	it("mustReconnect is true only when eligible AND the identity is asserted", async () => {
		const wrapper = await detailsView({ eligible: true, typed: false });
		wrapper.vm.state.reconnectEligible = true; // eligible, but prefill-only identity
		await flushPromises();
		expect(wrapper.vm.mustReconnect).toBe(false);
		wrapper.vm.state.identityFromUser = true; // now asserted
		await flushPromises();
		expect(wrapper.vm.mustReconnect).toBe(true);
	});

	it("C2: two rapid submits fire exactly one reconnect request", async () => {
		let resolveReq;
		api.startAccountReconnect.mockReturnValue(new Promise((r) => (resolveReq = r)));
		const wrapper = await detailsView({ eligible: true });
		const p1 = wrapper.vm.onDetailsSubmit();
		const p2 = wrapper.vm.onDetailsSubmit(); // second click during the await window
		resolveReq({ request: "req_1" });
		await Promise.all([p1, p2]);
		await flushPromises();
		expect(api.startAccountReconnect).toHaveBeenCalledTimes(1);
	});

	it("C2: startReconnect ignores a re-entrant call while a request is in flight", async () => {
		let resolveReq;
		api.startAccountReconnect.mockReturnValue(new Promise((r) => (resolveReq = r)));
		const wrapper = await detailsView({ eligible: true });
		const p1 = wrapper.vm.startReconnect();
		const p2 = wrapper.vm.startReconnect(); // e.g. a double-click on the Reconnect button
		resolveReq({ request: "req_1" });
		await Promise.all([p1, p2]);
		await flushPromises();
		expect(api.startAccountReconnect).toHaveBeenCalledTimes(1);
	});

	it("C2: two rapid submits on a non-eligible identity reach 'plan', never skipping to 'pay'", async () => {
		const wrapper = await detailsView({ eligible: false });
		const p1 = wrapper.vm.onDetailsSubmit();
		const p2 = wrapper.vm.onDetailsSubmit();
		await Promise.all([p1, p2]);
		await flushPromises();
		expect(wrapper.vm.state.step).toBe("plan");
	});

	it("C4: cancel then continue with the same identity reuses the request (no second call)", async () => {
		api.startAccountReconnect.mockResolvedValue({ request: "req_1" });
		const wrapper = await detailsView({ eligible: true });
		await wrapper.vm.onDetailsSubmit();
		await flushPromises();
		expect(api.startAccountReconnect).toHaveBeenCalledTimes(1);
		wrapper.vm.cancelReconnect(); // back to details, clears reconnectRequestId
		await flushPromises();
		await wrapper.vm.onDetailsSubmit(); // same identity -> reuse, no fresh request
		await flushPromises();
		expect(api.startAccountReconnect).toHaveBeenCalledTimes(1);
		expect(wrapper.vm.state.step).toBe("reconnect");
		expect(wrapper.vm.state.reconnectRequestId).toBe("req_1");
	});

	it("C6: needs_company (email under a different company) does NOT gate - advances to plan", async () => {
		const wrapper = await detailsView({
			eligible: false,
			needs_company: true,
			company: "WrongCo",
		});
		await wrapper.vm.onDetailsSubmit();
		await flushPromises();
		expect(api.startAccountReconnect).not.toHaveBeenCalled();
		expect(wrapper.vm.state.step).toBe("plan");
	});

	it("a brand-new (non-eligible) customer advances normally to plan", async () => {
		const wrapper = await detailsView({ eligible: false, company: "NewCo" });
		await wrapper.vm.onDetailsSubmit();
		await flushPromises();
		expect(api.startAccountReconnect).not.toHaveBeenCalled();
		expect(wrapper.vm.state.step).toBe("plan");
	});

	it("a failed startReconnect on Details surfaces the error (no silent dead-end)", async () => {
		api.startAccountReconnect.mockRejectedValue(
			new Error("reconnect requests are rate limited")
		);
		const wrapper = await detailsView({ eligible: true });
		await wrapper.vm.startReconnect();
		await flushPromises();
		// Stays on Details (never reached the reconnect screen)...
		expect(wrapper.vm.state.step).toBe("details");
		// ...and the failure is VISIBLE on the Details error banner, not swallowed.
		expect(wrapper.vm.state.detailsErr).toBeTruthy();
	});

	it("fails closed: an admin error on the eligibility check advances to plan (no gate)", async () => {
		api.reconnectAvailable.mockRejectedValue(new Error("admin down"));
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "details";
		wrapper.vm.state.email = "back@corp.test";
		wrapper.vm.state.company = "Corp";
		wrapper.vm.state.identityFromUser = true;
		wrapper.vm.billing.setUserValue("contact", "+91 98765 43210");
		wrapper.vm.state.termsAccepted = true;
		await wrapper.vm.onDetailsSubmit();
		await flushPromises();
		expect(api.startAccountReconnect).not.toHaveBeenCalled();
		expect(wrapper.vm.state.step).toBe("plan");
	});

	// Relies on the Button stub's `label` attr falling through to the root <button>.
	it("renders the Reconnect CTA (not Continue) only when the gate applies", async () => {
		const wrapper = await detailsView({ eligible: true });
		wrapper.vm.state.reconnectEligible = true; // as if the debounce has settled
		await flushPromises();
		expect(wrapper.find('button[label="Reconnect to your workspace"]').exists()).toBe(true);
		expect(wrapper.find('button[label="Continue"]').exists()).toBe(false);
		// A prefill-only (unasserted) identity keeps the normal Continue button.
		wrapper.vm.state.identityFromUser = false;
		await flushPromises();
		expect(wrapper.find('button[label="Reconnect to your workspace"]').exists()).toBe(false);
		expect(wrapper.find('button[label="Continue"]').exists()).toBe(true);
	});

	it("resend keeps the reuse tracker on the fresh request, not the superseded one", async () => {
		api.startAccountReconnect
			.mockResolvedValueOnce({ request: "req_1" })
			.mockResolvedValueOnce({ request: "req_2" });
		const wrapper = await detailsView({ eligible: true });
		vi.useFakeTimers();
		try {
			await wrapper.vm.onDetailsSubmit(); // issues req_1 -> reconnect screen
			await flushPromises();
			await wrapper.vm.resendReconnectCode(); // resend -> req_2 (supersedes req_1)
			await flushPromises();
			expect(wrapper.vm.state.reconnectRequestId).toBe("req_2");
			wrapper.vm.cancelReconnect(); // back to Details (clears reconnectRequestId)
			await flushPromises();
			await wrapper.vm.onDetailsSubmit(); // same identity -> reuse the FRESH request
			await flushPromises();
			expect(wrapper.vm.state.reconnectRequestId).toBe("req_2");
			expect(api.startAccountReconnect).toHaveBeenCalledTimes(2); // req_1 + resend; no 3rd
		} finally {
			vi.useRealTimers();
		}
	});
});

describe("lead-capture + T&C (frozen contract)", () => {
	it("captures a lead on entering the Plan step once email+company are present", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.email = "lead@example.com";
		wrapper.vm.state.company = "Acme";
		wrapper.vm.state.step = "plan";
		await flushPromises();
		expect(api.captureOnboardingLead).toHaveBeenCalledWith(
			expect.objectContaining({
				email: "lead@example.com",
				company: "Acme",
				step: "plan",
			})
		);
	});

	it("never captures before both email and company exist", async () => {
		const wrapper = mountView();
		await flushPromises();
		api.captureOnboardingLead.mockClear();
		wrapper.vm.state.email = "lead@example.com";
		wrapper.vm.state.company = "";
		wrapper.vm.state.step = "plan";
		await flushPromises();
		expect(api.captureOnboardingLead).not.toHaveBeenCalled();
	});

	it("a capture rejection never breaks the step transition", async () => {
		api.captureOnboardingLead.mockRejectedValueOnce(new Error("network"));
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.email = "lead@example.com";
		wrapper.vm.state.company = "Acme";
		wrapper.vm.state.step = "plan";
		await flushPromises();
		expect(wrapper.vm.state.step).toBe("plan");
	});

	it("Details renders no separate contact-consent checkbox (consent rides the T&C acceptance)", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "details";
		await flushPromises();
		expect(wrapper.text()).not.toContain("okay to contact me");
		expect(wrapper.vm.billing.consent).toBeUndefined();
	});

	it("Details renders the required T&C checkbox; Pay no longer renders it", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "details";
		await flushPromises();
		expect(wrapper.find("#jv-ob-terms").exists()).toBe(true);

		wrapper.vm.state.step = "pay";
		await flushPromises();
		expect(wrapper.find("#jv-ob-terms").exists()).toBe(false);
	});

	it("Details' Continue is blocked until the required T&C box is ticked, with a visible inline error (not a toast)", async () => {
		// Not eligible for reconnect - keeps this on the normal Continue path
		// the T&C gate actually guards (item 5: reconnect is exempt).
		api.reconnectAvailable.mockResolvedValue({ eligible: false, needs_company: false });
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "details";
		wrapper.vm.state.email = "a@b.com";
		wrapper.vm.state.company = "Acme";
		wrapper.vm.state.identityFromUser = true;
		wrapper.vm.billing.setUserValue("contact", "+91 98765 43210");
		wrapper.vm.state.termsAccepted = false;
		await flushPromises();

		await wrapper.vm.onDetailsSubmit();
		await flushPromises();
		expect(wrapper.vm.state.step).toBe("details");
		expect(wrapper.vm.detailsFieldErrors.terms).toBeTruthy();
		expect(wrapper.text()).toContain(wrapper.vm.detailsFieldErrors.terms);
		expect(toast.error).not.toHaveBeenCalled();

		wrapper.vm.state.termsAccepted = true;
		await flushPromises();
		await wrapper.vm.onDetailsSubmit();
		await flushPromises();
		expect(wrapper.vm.state.step).toBe("plan");
	});

	it("Details' Continue button is faded (disabled) until the T&C box is ticked", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.step = "details";
		await flushPromises();
		wrapper.vm.state.termsAccepted = false;
		await flushPromises();
		expect(wrapper.find('button[label="Continue"]').attributes("disabled")).toBeDefined();
		wrapper.vm.state.termsAccepted = true;
		await flushPromises();
		expect(wrapper.find('button[label="Continue"]').attributes("disabled")).toBeUndefined();
	});

	it("Pay stays disabled until the required T&C box is ticked", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.paymentProvider = "razorpay";
		wrapper.vm.state.termsAccepted = false;
		await flushPromises();
		expect(wrapper.vm.payDisabled).toBe(true);
		wrapper.vm.state.termsAccepted = true;
		await flushPromises();
		expect(wrapper.vm.payDisabled).toBe(false);
	});

	it("onPayClick refuses to start a signup until T&C is ticked, then sends terms_accepted:true", async () => {
		const wrapper = mountView();
		await flushPromises();
		wrapper.vm.state.email = "a@b.com";
		wrapper.vm.state.company = "Acme";
		wrapper.vm.state.planName = "pro";
		wrapper.vm.state.paymentProvider = "razorpay";
		wrapper.vm.state.termsAccepted = false;
		wrapper.vm.billing.setUserValue("contact", "+91 98765 43210");

		await wrapper.vm.onPayClick();
		expect(api.onboardingPaymentApi.startSignup).not.toHaveBeenCalled();

		wrapper.vm.state.termsAccepted = true;
		api.onboardingPaymentApi.startSignup.mockResolvedValue(
			ENVELOPE({
				code: "SIGNUP_VERIFICATION_REQUIRED",
				pending_verification: true,
				attempt_id: "att_1",
				generation: 0,
			})
		);
		await wrapper.vm.onPayClick();
		expect(api.onboardingPaymentApi.startSignup).toHaveBeenCalledWith(
			// contact_consent is granted BY the T&C acceptance (owner decision
			// 2026-08-14): the same click that sends terms_accepted sends it.
			expect.objectContaining({ terms_accepted: true, contact_consent: true }),
			expect.anything()
		);
	});
});

// The recovery/summary card's Reference row and the support-ticket body both show
// attemptId IN FULL now (owner decision), not the old `…`+last-6 mask.
describe("payment intent reference is shown in full, not masked", () => {
	it("intentRef, paySummaryRows and supportContext all carry the full attempt id", async () => {
		const longAttemptId = "att_2f9c8b17e4a1d3509b";
		api.onboardingPaymentApi.getOnboardingState.mockResolvedValue(
			ENVELOPE({
				code: "PAYMENT_AUTHORIZED_PENDING_CONFIRM",
				attempt_id: longAttemptId,
				generation: 1,
			})
		);
		const wrapper = mountView();
		await flushPromises();
		expect(wrapper.vm.pay.value).toBe(STATES.CONFIRM_REQUIRED);
		expect(wrapper.vm.pay.attemptId).toBe(longAttemptId);

		// Not truncated, no leading ellipsis - the whole id, unlike the old mask.
		expect(wrapper.vm.intentRef).toBe(longAttemptId);
		expect(wrapper.vm.intentRef).not.toContain("…");

		const refRow = wrapper.vm.paySummaryRows.find((r) => r.label === "Reference");
		expect(refRow?.value).toBe(longAttemptId);

		expect(wrapper.vm.supportContext).toContain(`Reference: ${longAttemptId}`);
	});
});
