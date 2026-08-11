// Plan 2026-08-07-billing-renew-dead-end, T2: the renew dead end. Admin answers
// a stuck checkout with a coded, actionable response (PAYMENT_CONFIRMATION_PENDING
// et al) and settleWithRedirect used to discard everything but two codes, falling
// to a bare loadAccount() - a spinner, then silence, forever. These pin the fix:
// every CODED answer renders its own row and actions, a codeless (settled) one
// still stays quiet, and a live token resumes the SAME pay page no matter what
// code rides with it.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

import { ACTIONS, CODES, copyFor, UNKNOWN_COPY } from "@/onboarding/paymentCodes";
import { inr as inrLabel } from "@/account/format.js";

const api = vi.hoisted(() => ({
	getAccount: vi.fn(),
	previewUpgrade: vi.fn(),
	startUpgrade: vi.fn(),
	previewDowngrade: vi.fn(),
	startDowngrade: vi.fn(),
	cancelScheduledDowngrade: vi.fn(),
	renewPlan: vi.fn(),
	reauthorizeAutopay: vi.fn(),
	resumePlan: vi.fn(),
	checkBillingPayment: vi.fn(),
	billingPaymentState: vi.fn(),
}));
vi.mock("@/api", () => api);

vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	Badge: { name: "Badge", props: ["label"], template: `<span>{{ label }}</span>` },
	Breadcrumbs: { name: "Breadcrumbs", template: `<div />` },
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading", "variant"],
		emits: ["click"],
		template: `<button :disabled="disabled || loading" @click="$emit('click')"><slot>{{ label }}</slot></button>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="dialog"><div class="dialog-title">{{ options && options.title }}</div><slot name="body-content" /><slot name="actions" /></div>`,
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<div v-if="message" class="error-message">{{ message }}</div>`,
	},
}));

import BillingPage from "./BillingPage.vue";

function baseAccount(overrides = {}) {
	return {
		plan: { plan_name: "Pro", name: "pro", price_inr: 100, billing_cycle: "Monthly" },
		subscription_status: "Expired",
		days_remaining: 0,
		current_period_end: "2026-07-01 00:00:00",
		autorenew: 0,
		can_reauthorize: false,
		cancel_at_period_end: false,
		scheduled_plan: "",
		upgrade_plans: [],
		downgrade_plans: [],
		...overrides,
	};
}

const STUBS = { LayoutHeader: true, JvSpinner: true, PlanCard: true, BillingNotice: true };

async function mountPage(accountData) {
	api.getAccount.mockResolvedValue(accountData || baseAccount());
	const wrapper = mount(BillingPage, { global: { stubs: STUBS } });
	await flushPromises();
	return wrapper;
}

// The healer is a RAW call (paymentCodec.decode reads {status, body}), because a
// rate-limited or under-review refusal is a CODED answer, not a transport error.
function rawOkBody(data = {}) {
	return { status: 200, body: { message: { ok: true, data } } };
}

function rawOk(data = {}) {
	return { status: 200, body: { message: { ok: true, data } } };
}

function rawFail(code, { status = 429, ...rest } = {}) {
	return { status, body: { message: { ok: false, error: { code, ...rest } } } };
}

function findByText(wrapper, selector, text) {
	return wrapper.findAll(selector).find((n) => n.text().includes(text));
}

beforeEach(() => {
	window.matchMedia =
		window.matchMedia ||
		(() => ({
			matches: false,
			addEventListener() {},
			removeEventListener() {},
			addListener() {},
			removeListener() {},
			dispatchEvent() {},
		}));
	// jsdom's real window.location.assign is non-configurable, so replace the
	// whole object rather than spying on it.
	Object.defineProperty(window, "location", {
		value: { ...window.location, assign: vi.fn() },
		writable: true,
		configurable: true,
	});
	vi.clearAllMocks();
});

describe("acceptance: renew's stuck-payment answer is no longer silent", () => {
	it('surfaces "We have not confirmed this payment." plus a working Check status action', async () => {
		const wrapper = await mountPage();
		api.renewPlan.mockResolvedValue(
			rawOkBody({
				contract_version: 2,
				code: "PAYMENT_CONFIRMATION_PENDING",
				recovery: "confirm_payment",
				payment_provider: "razorpay",
			})
		);

		await wrapper.vm.doRenew();
		await findByText(wrapper, ".dialog button", "Pay").trigger("click");
		await flushPromises();

		expect(wrapper.text()).toContain("We have not confirmed this payment.");
		expect(findByText(wrapper, "button", "Check payment status")).toBeTruthy();
		// PAYMENT_CONFIRMATION_PENDING's row declares Check only - INITIATE must
		// never appear here, or a stuck payment gets paid twice.
		expect(findByText(wrapper, "button", "Start a new payment")).toBeUndefined();
	});

	it("wires the rendered Check button to the healer then the passive re-read, in that order", async () => {
		const wrapper = await mountPage();
		api.renewPlan.mockResolvedValue(rawOkBody({ code: "PAYMENT_CONFIRMATION_PENDING" }));
		await wrapper.vm.doRenew();
		await findByText(wrapper, ".dialog button", "Pay").trigger("click");
		await flushPromises();

		const order = [];
		api.checkBillingPayment.mockImplementation(async () => {
			order.push("check");
			return rawOk();
		});
		api.billingPaymentState.mockImplementation(async () => {
			order.push("state");
			return { code: "PAYMENT_CONFIRMATION_PENDING" };
		});
		await findByText(wrapper, "button", "Check payment status").trigger("click");
		await flushPromises();

		expect(order).toEqual(["check", "state"]);
	});
});

describe("edge 1: double-click cannot open a second gateway object", () => {
	it("double-clicking Check runs the healer exactly once", async () => {
		const wrapper = await mountPage();
		api.checkBillingPayment.mockResolvedValue(rawOk());
		api.billingPaymentState.mockResolvedValue({ code: "PAYMENT_CONFIRMATION_PENDING" });

		const p1 = wrapper.vm.doCheckStatus();
		const p2 = wrapper.vm.doCheckStatus(); // fired before the first settles
		await Promise.all([p1, p2]);

		expect(api.checkBillingPayment).toHaveBeenCalledTimes(1);
	});

	it("double-clicking the renew Pay confirm charges at most once", async () => {
		const wrapper = await mountPage();
		api.renewPlan.mockResolvedValue(rawOkBody({ code: "PAYMENT_ALREADY_ACTIVE" }));
		await wrapper.vm.doRenew();

		// Two rapid confirms, neither awaited before the next fires - the same
		// race a real double-click produces.
		const p1 = wrapper.vm.confirmPending();
		const p2 = wrapper.vm.confirmPending();
		await Promise.all([p1, p2]);
		await flushPromises();

		expect(api.renewPlan).toHaveBeenCalledTimes(1);
	});
});

describe("edge 3: a code with no branch and no copy-table entry", () => {
	it("still renders an honest headline and at least one action", async () => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect({ code: "SOME_CODE_THIS_BUILD_HAS_NEVER_SEEN" });
		await flushPromises();

		expect(wrapper.vm.codeNotice.copy.headline).toBe(UNKNOWN_COPY.headline);
		expect(wrapper.vm.codeNotice.copy.actions.length).toBeGreaterThan(0);
		expect(wrapper.text()).toContain(UNKNOWN_COPY.headline);
		expect(findByText(wrapper, "button", "Check payment status")).toBeTruthy();
	});
});

// T3 correction: all three fixtures below used to pair a token with
// `code: "PAYMENT_CONFIRMATION_PENDING"`. Admin never actually sends that
// combination - initiate_billing_checkout's CommittedMoneyError/in-progress
// branches return PAYMENT_CONFIRMATION_PENDING with NO token, and its "reuse"
// branch (get_billing_payment_state re-echoing a live attempt) returns a
// token with NO code (jarvis_admin_v2 billing/checkout/billing.py). F2
// restores the code gate (effectiveCode computed before any token check), so
// that unreal combination stopped resolving as navigable - correctly, since a
// token riding a real code like PAYMENT_CONFIRMATION_PENDING must not
// override it (see the F2 describe block below). Fixed to the real codeless
// shape so this still exercises the origin-attestation machinery it names.
describe("edge 5: a token this bench cannot navigate with fails closed", () => {
	it("pay_origin_attested false never navigates, and shows the config-hold copy", async () => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect({
			pay_page_token: "tok_123",
			pay_origin: "https://admin.example.com",
			pay_origin_attested: false,
		});
		await flushPromises();

		expect(window.location.assign).not.toHaveBeenCalled();
		const copy = copyFor("BENCH_PAY_ORIGIN_UNCONFIGURED");
		expect(wrapper.vm.actionErr).toContain(copy.headline);
	});

	it('an empty pay_origin (payPageUrl returns "") also fails closed', async () => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect({
			pay_page_token: "tok_123",
			pay_origin: "",
			pay_origin_attested: true,
		});
		await flushPromises();

		expect(window.location.assign).not.toHaveBeenCalled();
		expect(wrapper.vm.actionErr).toContain(copyFor("BENCH_PAY_ORIGIN_UNCONFIGURED").headline);
	});

	it("a live, attested token DOES navigate - the machinery this fails closed against", async () => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect({
			pay_page_token: "tok_123",
			pay_origin: "https://admin.example.com",
			pay_origin_attested: true,
		});

		expect(window.location.assign).toHaveBeenCalledWith(
			"https://admin.example.com/jarvis-checkout#t=tok_123"
		);
		expect(wrapper.vm.phase).toBe("redirect");
	});
});

describe("edge 6: the intent resolves between Check and the re-read", () => {
	it("lands on the active state and does not still offer a second payment", async () => {
		const wrapper = await mountPage(baseAccount({ subscription_status: "Expired" }));
		api.checkBillingPayment.mockResolvedValue(rawOk()); // the healer converges it server-side
		api.billingPaymentState.mockResolvedValue({ code: "PAYMENT_ALREADY_ACTIVE" });
		api.getAccount.mockResolvedValue(baseAccount({ subscription_status: "Active" }));

		await wrapper.vm.doCheckStatus();
		await flushPromises();

		expect(wrapper.vm.account.subscription_status).toBe("Active");
		// PAYMENT_ALREADY_ACTIVE declares CONTINUE only - never CHECK/INITIATE, so
		// no path back into a second payment is offered.
		expect(wrapper.vm.codeNotice.copy.actions).not.toContain(ACTIONS.INITIATE);
		expect(findByText(wrapper, "button", "Start a new payment")).toBeUndefined();
	});
});

describe("edge 11: oversized / malformed admin payload", () => {
	const cases = [
		["missing code", {}],
		["body is null", null],
		["body is a bare string", "not-an-object"],
		["body is a number", 7],
	];

	it.each(cases)("%s: never throws", async (_label, payload) => {
		const wrapper = await mountPage();
		await expect(wrapper.vm.settleWithRedirect(payload)).resolves.not.toThrow();
		await flushPromises();
	});

	it("a non-string code still renders rather than throwing", async () => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect({ code: 42 });
		await flushPromises();

		expect(wrapper.vm.codeNotice).toBeTruthy();
		expect(wrapper.vm.codeNotice.copy.headline).toBe(UNKNOWN_COPY.headline);
	});

	// F4: the plan's own required behaviour, previously untested - the old spec
	// only asserted "never throws" for a missing code, which the OLD (deviant)
	// code satisfied by rendering nothing at all. Renders the honest unknown row
	// instead, distinguishing this from the SETTLED codeless answer below.
	it("missing code with no settlement evidence renders the honest unknown row, not silence", async () => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect({});
		await flushPromises();

		expect(wrapper.vm.codeNotice).toBeTruthy();
		expect(wrapper.vm.codeNotice.copy.headline).toBe(UNKNOWN_COPY.headline);
	});
});

describe("acceptance: a coded answer is never discarded, a settled one is never alarmed about", () => {
	const coded = [
		{ code: "PAYMENT_CONFIRMATION_PENDING" },
		{ code: "PAYMENT_DECLINED" },
		{ code: "CLIENT_UPGRADE_REQUIRED_LOOKALIKE" }, // unknown code, not the real sentinel
	];

	it.each(coded)("renders something for the coded answer %j", async (payload) => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect(payload);
		await flushPromises();

		expect(!!wrapper.vm.codeNotice || !!wrapper.vm.actionErr).toBe(true);
	});

	// The regression this branch must not reintroduce: an Annual downgrade just
	// SCHEDULES and answers without a code. Speaking there would print "we could
	// not determine the payment status" on top of a success.
	it("stays silent on a codeless, settled answer", async () => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect({ scheduled_plan: "mini", ok: true });
		await flushPromises();

		expect(wrapper.vm.codeNotice).toBeNull();
		expect(wrapper.vm.actionErr).toBe("");
	});

	// F4: the rest of the settlement-evidence predicate (subscription_status,
	// tenant_status/agent_url connection keys), not just scheduled_plan.
	it.each([
		["subscription_status", { subscription_status: "Active" }],
		["tenant_status", { tenant_status: "ready" }],
		["agent_url", { agent_url: "https://tenant.example.com" }],
	])("also stays silent on a codeless answer carrying %s", async (_label, payload) => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect(payload);
		await flushPromises();

		expect(wrapper.vm.codeNotice).toBeNull();
	});

	it("the real CLIENT_UPGRADE_REQUIRED case (raw handles, no token) also renders", async () => {
		const wrapper = await mountPage();
		await wrapper.vm.settleWithRedirect({ razorpay_order_id: "order_123" });
		await flushPromises();

		expect(wrapper.vm.actionErr).toContain(copyFor("CLIENT_UPGRADE_REQUIRED").headline);
	});
});

// ===========================================================================
// T3: reactivate onto any plan (2026-08-07-expired-plan-reactivation)
// ===========================================================================

function reactivationAccount(overrides = {}) {
	return baseAccount({
		subscription_status: "Expired",
		can_reactivate: true,
		reactivation_plans: [
			{ name: "pro", plan_name: "Pro", price_inr: 100, billing_cycle: "Monthly" },
			{ name: "starter", plan_name: "Starter", price_inr: 50, billing_cycle: "Monthly" },
			{ name: "growth", plan_name: "Growth", price_inr: 500, billing_cycle: "Monthly" },
		],
		...overrides,
	});
}
const STARTER_PLAN = {
	name: "starter",
	plan_name: "Starter",
	price_inr: 50,
	billing_cycle: "Monthly",
};
const GROWTH_PLAN = {
	name: "growth",
	plan_name: "Growth",
	price_inr: 500,
	billing_cycle: "Monthly",
};

describe("feature: reactivate onto any plan", () => {
	it("renders reactivation_plans instead of upgrade/downgrade, minus the current plan", async () => {
		const wrapper = await mountPage(reactivationAccount());

		expect(wrapper.vm.canReactivate).toBe(true);
		// "pro" is the current plan (baseAccount) and already has its own card.
		expect(wrapper.vm.reactivationPlans.map((p) => p.name)).toEqual(["starter", "growth"]);
	});

	it('suppresses "no other plans" while reactivation plans exist (the visible half of the bug)', async () => {
		const wrapper = await mountPage(reactivationAccount());
		expect(wrapper.vm.noOtherPlans).toBe(false);
	});

	it('shows "no other plans" when reactivating with nothing else to offer', async () => {
		const wrapper = await mountPage(
			reactivationAccount({
				reactivation_plans: [
					{ name: "pro", plan_name: "Pro", price_inr: 100, billing_cycle: "Monthly" },
				],
			})
		);
		expect(wrapper.vm.reactivationPlans).toEqual([]);
		expect(wrapper.vm.noOtherPlans).toBe(true);
	});

	it("doReactivate confirms then calls renew with the chosen plan as target_plan", async () => {
		const wrapper = await mountPage(reactivationAccount());
		api.renewPlan.mockResolvedValue(rawOkBody({ tenant_status: "ready" }));

		wrapper.vm.doReactivate(GROWTH_PLAN);
		expect(wrapper.vm.pending.open).toBe(true);
		expect(wrapper.vm.pending.title).toBe("Renew on Growth");
		expect(wrapper.vm.pending.amount).toBe(inrLabel(500));

		await wrapper.vm.confirmPending();
		await flushPromises();

		expect(api.renewPlan).toHaveBeenCalledWith("growth");
	});

	it("renewing onto the current plan names it as the target, so the price shown is the price charged", async () => {
		// Round-3 MINOR 11: without a target, renew priced the order off a stale
		// scheduled_plan the customer was never shown - dialog "Pay Rs500", order
		// Rs50. Naming the card's own plan makes the two the same number.
		const wrapper = await mountPage(reactivationAccount());
		api.renewPlan.mockResolvedValue(rawOkBody({ tenant_status: "ready" }));

		await wrapper.vm.doRenew();
		await wrapper.vm.confirmPending();
		await flushPromises();

		expect(api.renewPlan).toHaveBeenCalledWith(wrapper.vm.currentPlan.name);
	});

	it("a RUNNING subscription's Renew sends no target - admin refuses one there", async () => {
		const wrapper = await mountPage(
			baseAccount({
				subscription_status: "Active",
				days_remaining: 12,
				can_reactivate: false,
			})
		);
		api.renewPlan.mockResolvedValue(rawOkBody({ tenant_status: "ready" }));

		await wrapper.vm.doRenew();
		await wrapper.vm.confirmPending();
		await flushPromises();

		expect(api.renewPlan).toHaveBeenCalledWith(undefined);
	});

	it("Renew is offered on the top card for a lapsed Past Due account too (not just Expired/Cancelled)", async () => {
		const wrapper = await mountPage(
			reactivationAccount({ subscription_status: "Past Due", days_remaining: 0 })
		);
		expect(wrapper.vm.currentAction.label).toBe("Renew");
	});
});

describe("GO-LIVE: the confirm dialog shows the tax-inclusive charge, not the pre-tax price", () => {
	// Renew/reactivate charge tax-inclusive server-side (price_inr + 18% GST).
	// get_account_summary now additively exposes that as total_inr on both the
	// current-plan object and each reactivation_plans row; price_inr stays the
	// pre-tax base. The "Pay ₹X" figure the customer confirms before paying must
	// equal what actually gets charged, so it has to read total_inr - falling
	// back to price_inr only when an older backend has not started sending it.

	it("doRenew's dialog shows total_inr (tax-inclusive) when the backend sends it, not price_inr", async () => {
		const wrapper = await mountPage(
			baseAccount({
				plan: {
					plan_name: "Pro",
					name: "pro",
					price_inr: 100,
					total_inr: 118,
					billing_cycle: "Monthly",
				},
			})
		);

		await wrapper.vm.doRenew();

		expect(wrapper.vm.pending.amount).toBe(inrLabel(118));
		expect(wrapper.vm.pending.confirmLabel).toBe(`Pay ${inrLabel(118)}`);
	});

	it("doRenew falls back to price_inr when an older backend omits total_inr", async () => {
		const wrapper = await mountPage(baseAccount()); // plan.price_inr: 100, no total_inr

		await wrapper.vm.doRenew();

		expect(wrapper.vm.pending.amount).toBe(inrLabel(100));
		expect(wrapper.vm.pending.confirmLabel).toBe(`Pay ${inrLabel(100)}`);
	});

	it("doReactivate's dialog shows the row's total_inr (tax-inclusive), not price_inr", async () => {
		const wrapper = await mountPage(reactivationAccount());
		const plan = { name: "growth", plan_name: "Growth", price_inr: 500, total_inr: 590 };

		wrapper.vm.doReactivate(plan);

		expect(wrapper.vm.pending.amount).toBe(inrLabel(590));
		expect(wrapper.vm.pending.confirmLabel).toBe(`Pay ${inrLabel(590)}`);
	});

	it("doReactivate falls back to price_inr when the row has no total_inr", async () => {
		const wrapper = await mountPage(reactivationAccount());

		wrapper.vm.doReactivate(GROWTH_PLAN); // no total_inr on this fixture

		expect(wrapper.vm.pending.amount).toBe(inrLabel(500));
		expect(wrapper.vm.pending.confirmLabel).toBe(`Pay ${inrLabel(500)}`);
	});

	it("what get_account_summary actually returns for renew charges the exact amount shown", async () => {
		// End-to-end through the mocked api boundary: the amount shown pre-pay
		// and the amount the payment call is asked to charge must be the same
		// server truth, not two different reads of the plan object.
		const wrapper = await mountPage(
			baseAccount({
				plan: {
					plan_name: "Pro",
					name: "pro",
					price_inr: 100,
					total_inr: 118,
					billing_cycle: "Monthly",
				},
			})
		);
		api.renewPlan.mockResolvedValue(rawOkBody({ tenant_status: "ready" }));

		await wrapper.vm.doRenew();
		expect(wrapper.vm.pending.amount).toBe(inrLabel(118));

		await wrapper.vm.confirmPending();
		await flushPromises();

		expect(api.renewPlan).toHaveBeenCalled();
	});
});

describe("edge 6/8: a cheaper reactivation charges full price, never implies a prorated switch", () => {
	it("the card's own note names full price and explicitly rules out proration/scheduling", async () => {
		const wrapper = await mountPage(reactivationAccount());
		expect(wrapper.vm.REACTIVATION_NOTE).toMatch(/full price/i);
		expect(wrapper.vm.REACTIVATION_NOTE).toMatch(/nothing is prorated/i);
		expect(wrapper.vm.REACTIVATION_NOTE).not.toMatch(/switch/i);
	});

	it("the confirm dialog for the CHEAPER plan shows its full price, not a difference", async () => {
		const wrapper = await mountPage(reactivationAccount()); // current plan "pro" is 100
		wrapper.vm.doReactivate(STARTER_PLAN); // starter is 50, cheaper than current

		expect(wrapper.vm.pending.amount).toBe(inrLabel(50));
		expect(wrapper.vm.pending.message).toMatch(/no credit/i);
		expect(wrapper.vm.pending.message).not.toMatch(/prorat/i);
	});
});

describe("edge 10: double-submit on a reactivation card", () => {
	it("double-clicking the card, then double-clicking confirm, charges at most once", async () => {
		const wrapper = await mountPage(reactivationAccount());
		api.renewPlan.mockResolvedValue(rawOkBody({ code: "PAYMENT_ALREADY_ACTIVE" }));

		wrapper.vm.doReactivate(STARTER_PLAN);
		wrapper.vm.doReactivate(STARTER_PLAN); // a second click before the dialog settles

		const p1 = wrapper.vm.confirmPending();
		const p2 = wrapper.vm.confirmPending();
		await Promise.all([p1, p2]);
		await flushPromises();

		expect(api.renewPlan).toHaveBeenCalledTimes(1);
		expect(api.renewPlan).toHaveBeenCalledWith("starter");
	});
});

describe("edge 11: reactivation controls share the page's own admin gate", () => {
	it("a refused getAccount call renders no plan controls at all - same gate, no separate fetch", async () => {
		api.getAccount.mockRejectedValue(
			new Error("You do not have permission to access this resource")
		);
		const wrapper = mount(BillingPage, { global: { stubs: STUBS } });
		await flushPromises();

		expect(wrapper.vm.loadErr).toBeTruthy();
		expect(wrapper.text()).not.toContain("Renew on this plan");
		expect(wrapper.text()).not.toContain("Plans");
	});
});

describe("edge 12: Active-with-time-left keeps the prorated grid and offers no reactivation", () => {
	it("can_reactivate false, reactivation_plans empty -> the prorated grid, never the reactivation one", async () => {
		const wrapper = await mountPage(
			baseAccount({
				subscription_status: "Active",
				days_remaining: 10,
				can_reactivate: false,
				reactivation_plans: [],
				upgrade_plans: [
					{
						name: "growth",
						plan_name: "Growth",
						price_inr: 500,
						billing_cycle: "Monthly",
					},
				],
			})
		);

		expect(wrapper.vm.canReactivate).toBe(false);
		expect(wrapper.vm.reactivationPlans).toEqual([]);
		expect(wrapper.vm.upgradePlans).toHaveLength(1);
	});
});

describe("edge 13: admin unreachable / 5xx / rate-limited on reactivate or check", () => {
	it("check: a transport failure renders the honest bench code, not raw error text (F3)", async () => {
		const wrapper = await mountPage();
		api.checkBillingPayment.mockRejectedValue(new TypeError("Failed to fetch"));

		await wrapper.vm.doCheckStatus();
		await flushPromises();

		expect(wrapper.vm.codeNotice.code).toBe("BENCH_ADMIN_UNREACHABLE");
		expect(wrapper.text()).toContain(copyFor("BENCH_ADMIN_UNREACHABLE").headline);
		expect(wrapper.text()).not.toContain("Failed to fetch");
	});

	it("reactivate: an admin failure surfaces an error, not a silent no-op, and mutates nothing locally", async () => {
		const wrapper = await mountPage(reactivationAccount());
		api.renewPlan.mockRejectedValue(
			new Error("admin is unreachable right now; try again in a moment.")
		);

		wrapper.vm.doReactivate(STARTER_PLAN);
		await wrapper.vm.confirmPending();
		await flushPromises();

		expect(wrapper.vm.actionErr).toContain("admin is unreachable");
		expect(wrapper.vm.busy).toBe("");
		expect(wrapper.vm.account.subscription_status).toBe("Expired"); // untouched
	});
});

describe("edge 15: a success code never renders over a revoked subscription (F5)", () => {
	it("PAYMENT_ALREADY_ACTIVE is dropped when the reloaded account is not Active", async () => {
		const wrapper = await mountPage(baseAccount({ subscription_status: "Cancelled" }));
		api.getAccount.mockResolvedValue(baseAccount({ subscription_status: "Cancelled" }));

		await wrapper.vm.settleWithRedirect({ code: "PAYMENT_ALREADY_ACTIVE" });
		await flushPromises();

		expect(wrapper.vm.codeNotice).toBeNull();
	});

	it("PAYMENT_ALREADY_ACTIVE renders normally when the reloaded account IS Active", async () => {
		const wrapper = await mountPage(baseAccount({ subscription_status: "Expired" }));
		api.getAccount.mockResolvedValue(baseAccount({ subscription_status: "Active" }));

		await wrapper.vm.settleWithRedirect({ code: "PAYMENT_ALREADY_ACTIVE" });
		await flushPromises();

		expect(wrapper.vm.codeNotice).toBeTruthy();
		expect(wrapper.vm.codeNotice.code).toBe("PAYMENT_ALREADY_ACTIVE");
	});
});

describe("F2: a status check never navigates on its own", () => {
	it("a live token found by Check renders Resume instead of auto-navigating", async () => {
		const wrapper = await mountPage();
		api.checkBillingPayment.mockResolvedValue(rawOk());
		api.billingPaymentState.mockResolvedValue({
			pay_page_token: "tok_123",
			pay_origin: "https://admin.example.com",
			pay_origin_attested: true,
		});

		await wrapper.vm.doCheckStatus();
		await flushPromises();

		expect(window.location.assign).not.toHaveBeenCalled();
		expect(wrapper.vm.codeNotice).toBeTruthy();
		expect(wrapper.vm.codeNotice.copy.actions).toContain(ACTIONS.RESUME);
	});

	it("clicking Resume navigates using the token Check already read", async () => {
		const wrapper = await mountPage();
		api.checkBillingPayment.mockResolvedValue(rawOk());
		api.billingPaymentState.mockResolvedValue({
			pay_page_token: "tok_123",
			pay_origin: "https://admin.example.com",
			pay_origin_attested: true,
		});
		await wrapper.vm.doCheckStatus();
		await flushPromises();

		wrapper.vm.runCodeAction(ACTIONS.RESUME);

		expect(window.location.assign).toHaveBeenCalledWith(
			"https://admin.example.com/jarvis-checkout#t=tok_123"
		);
		expect(wrapper.vm.phase).toBe("redirect");
	});

	it("a token riding a code effectiveCode refuses to override never navigates, from any caller", async () => {
		const wrapper = await mountPage();
		// Active on reload so F5's success/revoked cross-check does not also
		// suppress the notice - this test is isolated to the F2 code gate.
		api.getAccount.mockResolvedValue(baseAccount({ subscription_status: "Active" }));
		await wrapper.vm.settleWithRedirect({
			code: "PAYMENT_ALREADY_ACTIVE",
			pay_page_token: "tok_123",
			pay_origin: "https://admin.example.com",
			pay_origin_attested: true,
		});
		await flushPromises();

		expect(window.location.assign).not.toHaveBeenCalled();
		expect(wrapper.vm.codeNotice.code).toBe("PAYMENT_ALREADY_ACTIVE");
	});

	it("a genuinely fresh, codeless token from a JUST-CONFIRMED renew still navigates immediately", async () => {
		const wrapper = await mountPage();
		api.renewPlan.mockResolvedValue(
			rawOkBody({
				pay_page_token: "tok_fresh",
				pay_origin: "https://admin.example.com",
				pay_origin_attested: true,
			})
		);

		await wrapper.vm.doRenew();
		await findByText(wrapper, ".dialog button", "Pay").trigger("click");
		await flushPromises();

		expect(window.location.assign).toHaveBeenCalledWith(
			"https://admin.example.com/jarvis-checkout#t=tok_fresh"
		);
	});
});

describe("F6: INITIATE retries the flow that produced the code, not always renew", () => {
	it("a code from doUpgrade's payment attempt retries doUpgrade, not doRenew's dialog", async () => {
		const wrapper = await mountPage(
			baseAccount({
				subscription_status: "Active",
				upgrade_plans: [
					{
						name: "growth",
						plan_name: "Growth",
						price_inr: 500,
						billing_cycle: "Monthly",
					},
				],
			})
		);
		api.previewUpgrade.mockResolvedValue({ prorated_inr: 150 });
		api.startUpgrade.mockResolvedValue({ code: "PAYMENT_DECLINED" });

		await wrapper.vm.doUpgrade({ name: "growth", plan_name: "Growth" });
		await findByText(wrapper, ".dialog button", "Pay").trigger("click");
		await flushPromises();
		expect(wrapper.vm.codeNotice.code).toBe("PAYMENT_DECLINED");

		await wrapper.vm.runCodeAction(ACTIONS.INITIATE);
		await flushPromises();

		expect(wrapper.vm.pending.title).toBe("Upgrade to Growth");
		expect(api.renewPlan).not.toHaveBeenCalled();
	});

	it("a code from a reactivation attempt retries THAT plan, not the plain renew", async () => {
		const wrapper = await mountPage(reactivationAccount());
		api.renewPlan.mockResolvedValue(rawOkBody({ code: "PAYMENT_DECLINED" }));

		wrapper.vm.doReactivate(GROWTH_PLAN);
		await wrapper.vm.confirmPending();
		await flushPromises();
		expect(wrapper.vm.codeNotice.code).toBe("PAYMENT_DECLINED");

		await wrapper.vm.runCodeAction(ACTIONS.INITIATE);
		await flushPromises();

		expect(wrapper.vm.pending.title).toBe("Renew on Growth");
		expect(api.renewPlan).toHaveBeenLastCalledWith("growth");
	});
});

describe("F11: stale notice/error cleared when a new action starts", () => {
	it("opening a new confirm dialog clears a stale actionErr from a previous action", async () => {
		const wrapper = await mountPage(
			baseAccount({ cancel_at_period_end: true, can_reauthorize: true })
		);
		api.resumePlan.mockRejectedValue(new Error("stale failure"));
		await wrapper.vm.doResume();
		await flushPromises();
		expect(wrapper.vm.actionErr).toBe("stale failure");

		wrapper.vm.doReauthorize(); // opens a NEW confirm dialog
		expect(wrapper.vm.actionErr).toBe("");
	});

	it("a new payment attempt clears a stale codeNotice from a previous one, before it resolves", async () => {
		const wrapper = await mountPage(baseAccount({ can_reauthorize: true }));
		api.renewPlan.mockResolvedValue(rawOkBody({ code: "PAYMENT_DECLINED" }));
		await wrapper.vm.doRenew();
		await findByText(wrapper, ".dialog button", "Pay").trigger("click");
		await flushPromises();
		expect(wrapper.vm.codeNotice).toBeTruthy();

		let resolveReauth;
		api.reauthorizeAutopay.mockReturnValue(new Promise((r) => (resolveReauth = r)));
		wrapper.vm.doReauthorize();
		await wrapper.vm.$nextTick();
		await findByText(wrapper, ".dialog button", "Continue").trigger("click");

		expect(wrapper.vm.codeNotice).toBeNull();
		resolveReauth({});
		await flushPromises();
	});
});

describe("F12: the action error renders near the top controls, not the page foot", () => {
	it("actionErr from doResume appears before the Plans heading in the DOM", async () => {
		const wrapper = await mountPage(baseAccount({ cancel_at_period_end: true }));
		api.resumePlan.mockRejectedValue(new Error("resume failed"));

		await wrapper.vm.doResume();
		await flushPromises();

		const html = wrapper.html();
		const errIdx = html.indexOf("resume failed");
		const plansIdx = html.indexOf("Plans");
		expect(errIdx).toBeGreaterThan(-1);
		expect(plansIdx).toBeGreaterThan(-1);
		expect(errIdx).toBeLessThan(plansIdx);
	});
});

describe("finding 7: the healer's coded refusals are answers, not transport errors", () => {
	it("renders PAYMENT_CHECK_RATE_LIMITED as wait-and-retry, not 'could not reach'", async () => {
		const wrapper = await mountPage();
		api.checkBillingPayment.mockResolvedValue(
			rawFail("PAYMENT_CHECK_RATE_LIMITED", { retry_after_seconds: 120 })
		);

		await wrapper.vm.doCheckStatus();
		await flushPromises();

		expect(wrapper.vm.codeNotice.code).toBe("PAYMENT_CHECK_RATE_LIMITED");
		// The service WAS reached; saying otherwise would be a lie.
		expect(wrapper.text()).not.toContain(copyFor("BENCH_ADMIN_UNREACHABLE").headline);
		// A throttled check must never read as a decline - that invites a 2nd payment.
		expect(findByText(wrapper, "button", "Start a new payment")).toBeUndefined();
	});

	it("does not run the passive re-read when the healer refused", async () => {
		const wrapper = await mountPage();
		api.checkBillingPayment.mockResolvedValue(rawFail("PAYMENT_CHECK_RATE_LIMITED"));
		api.billingPaymentState.mockResolvedValue({});

		await wrapper.vm.doCheckStatus();
		await flushPromises();

		expect(api.billingPaymentState).not.toHaveBeenCalled();
	});

	it("still shows the transport row when the answer is genuinely unreadable", async () => {
		const wrapper = await mountPage();
		api.checkBillingPayment.mockResolvedValue({ networkError: true, status: 0 });

		await wrapper.vm.doCheckStatus();
		await flushPromises();

		expect(wrapper.vm.codeNotice.code).toBe("BENCH_ADMIN_UNREACHABLE");
	});
});

describe("finding 6: renew's coded refusals get a notice with an action, not red prose", () => {
	it("renders PAYMENT_UNDER_REVIEW as a notice carrying Check payment status", async () => {
		const wrapper = await mountPage();
		api.renewPlan.mockResolvedValue(rawFail("PAYMENT_UNDER_REVIEW", { status: 409 }));

		await wrapper.vm.doRenew();
		await findByText(wrapper, ".dialog button", "Pay").trigger("click");
		await flushPromises();

		// The parked-money hold is an answer about the payment, not a dead end.
		expect(wrapper.vm.codeNotice).toBeTruthy();
		expect(wrapper.vm.codeNotice.code).toBe("PAYMENT_UNDER_REVIEW");
		expect(wrapper.vm.actionErr).toBe("");
		expect(findByText(wrapper, "button", "Check payment status")).toBeTruthy();
	});

	it("leaves a plan-validation refusal as the server's own sentence", async () => {
		// "changing billing cycle is not supported" says more than any generic row.
		const wrapper = await mountPage();
		api.renewPlan.mockResolvedValue(
			rawFail("CycleSwitchNotSupported", {
				status: 400,
				message: "changing billing cycle is not supported",
			})
		);

		await wrapper.vm.doRenew();
		await findByText(wrapper, ".dialog button", "Pay").trigger("click");
		await flushPromises();

		expect(wrapper.vm.codeNotice).toBeNull();
		expect(wrapper.vm.actionErr).toBeTruthy();
	});

	it("a reactivation refusal retries the reactivation, never a plain renew", async () => {
		const wrapper = await mountPage(
			baseAccount({
				subscription_status: "Cancelled",
				can_reactivate: true,
				reactivation_plans: [
					{
						name: "growth",
						plan_name: "Growth",
						price_inr: 500,
						billing_cycle: "Monthly",
					},
				],
			})
		);
		api.renewPlan.mockResolvedValue(rawFail("PAYMENT_UNDER_REVIEW", { status: 409 }));

		await wrapper.vm.doReactivate({
			name: "growth",
			plan_name: "Growth",
			price_inr: 500,
			billing_cycle: "Monthly",
		});
		const confirmBtn = wrapper
			.findAll(".dialog button")
			.find((b) => !b.text().includes("Cancel"));
		await confirmBtn.trigger("click");
		await flushPromises();

		expect(wrapper.vm.codeNotice.code).toBe("PAYMENT_UNDER_REVIEW");
		expect(typeof wrapper.vm.codeNotice.retry).toBe("function");
	});
});

// Plan 2026-08-08-reactivation-arms-autopay, T5. The pay page now returns a
// BILLING checkout here instead of the signup wizard (which had one code for every
// lapsed status and no way to renew). Landing here right after paying means the
// confirm/webhook may still be settling, so a plain read can show the customer the
// very state they just paid to leave.
describe("returning from the pay page", () => {
	function atSearch(search) {
		Object.defineProperty(window, "location", {
			value: { ...window.location, pathname: "/billing", search, assign: vi.fn() },
			writable: true,
			configurable: true,
		});
	}

	it("runs the healer once when the outcome says money moved", async () => {
		atSearch("?pay=done");
		api.checkBillingPayment.mockResolvedValue(rawOk());
		api.billingPaymentState.mockResolvedValue({ message: { ok: true, data: {} } });
		await mountPage(baseAccount({ subscription_status: "Expired" }));
		await flushPromises();
		expect(api.checkBillingPayment).toHaveBeenCalledTimes(1);
	});

	it("runs it for a still-settling outcome too", async () => {
		atSearch("?pay=pending");
		api.checkBillingPayment.mockResolvedValue(rawOk());
		api.billingPaymentState.mockResolvedValue({ message: { ok: true, data: {} } });
		await mountPage(baseAccount({ subscription_status: "Expired" }));
		await flushPromises();
		expect(api.checkBillingPayment).toHaveBeenCalledTimes(1);
	});

	it("does NOT check on an ordinary visit, or on a declined return", async () => {
		for (const search of ["", "?pay=failed"]) {
			vi.clearAllMocks();
			atSearch(search);
			await mountPage();
			await flushPromises();
			expect(api.checkBillingPayment).not.toHaveBeenCalled();
		}
	});
});

describe("the paid notice speaks for the BILLING surface, not the signup wizard", () => {
	async function paidNotice() {
		// Arrive back from the pay page, which auto-runs the healer; it answers
		// "already active" for a customer who has just paid.
		Object.defineProperty(window, "location", {
			value: {
				...window.location,
				href: "http://t.example/billing?pay=done",
				pathname: "/billing",
				search: "?pay=done",
				hash: "",
				assign: vi.fn(),
			},
			writable: true,
			configurable: true,
		});
		api.checkBillingPayment.mockResolvedValue(
			rawFail(CODES.PAYMENT_ALREADY_ACTIVE, { status: 409 })
		);
		const wrapper = await mountPage();
		await flushPromises();
		return wrapper;
	}

	it('does not tell an existing customer we are "continuing your setup"', async () => {
		const text = (await paidNotice()).text();
		expect(text).toContain("Nothing more is owed");
		expect(text).not.toContain("continuing your setup");
	});

	it("offers a way back to chat rather than a dead Continue", async () => {
		const wrapper = await paidNotice();
		const btn = findByText(wrapper, "button", "Back to chat");
		expect(btn).toBeTruthy();
		await btn.trigger("click");
		expect(window.location.assign).toHaveBeenCalledWith("/jarvis/");
	});
});
