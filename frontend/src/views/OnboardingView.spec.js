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
	getAccountDefaults: vi.fn(async () => ({})),
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
