import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

vi.hoisted(() => {
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		onchange: null,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent: () => false,
	});
});

const api = vi.hoisted(() => ({
	getAccount: vi.fn(),
	cancelPlanAtPeriodEnd: vi.fn(),
	resumePlan: vi.fn(),
	cancelScheduledDowngrade: vi.fn(),
}));
vi.mock("@/api", () => api);

vi.mock("frappe-ui", () => ({
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="stub-badge" :data-label="label">{{ label }}</span>`,
	},
	Button: {
		name: "Button",
		props: ["label", "variant", "theme", "iconRight", "iconLeft", "loading", "disabled"],
		emits: ["click"],
		template: `<button class="stub-button" :data-label="label" @click="$emit('click')">{{ label }}</button>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: `<span />` },
}));

const confirm = vi.fn();
vi.mock("@/composables/useConfirm", () => ({
	useConfirm: () => ({ confirm }),
}));

const shell = { settingsOpen: false };
vi.mock("@/stores/shell", () => ({
	useShellStore: () => shell,
}));

vi.mock("vue-router", () => ({
	useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/components/settings/SettingsPane.vue", () => ({
	default: { name: "SettingsPane", template: `<div><slot /></div>` },
}));

import PlanBillingPane from "./PlanBillingPane.vue";

function baseAccount(overrides = {}) {
	return {
		plan: { plan_name: "Pro", price_inr: 3999, billing_cycle: "Monthly" },
		subscription_status: "Active",
		current_period_end: "2026-09-15 00:00:00",
		access_ends_on: "2026-09-15",
		days_remaining: 30,
		autorenew: 1,
		has_mandate: true,
		can_cancel: true,
		can_reauthorize: false,
		cancel_at_period_end: false,
		scheduled_plan: null,
		...overrides,
	};
}

async function mountPane(accountData = {}) {
	const acct = baseAccount(accountData);
	api.getAccount.mockResolvedValue(acct);
	const w = mount(PlanBillingPane);
	await flushPromises();
	return w;
}

function cancelButton(w) {
	return w.findAll(".stub-button").find((b) => b.text().includes("Cancel"));
}

describe("PlanBillingPane", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		confirm.mockClear();
	});

	describe("cancel button visibility", () => {
		it("shows cancel button when can_cancel is true", async () => {
			const w = await mountPane({ can_cancel: true });
			expect(cancelButton(w)).toBeDefined();
		});

		it("hides cancel button when can_cancel is false", async () => {
			const w = await mountPane({ can_cancel: false });
			expect(cancelButton(w)).toBeUndefined();
		});

		it("hides cancel button when autopay is off", async () => {
			// AutoPay turned off: mandate released, nothing left to cancel
			const w = await mountPane({ can_cancel: false, has_mandate: false });
			expect(cancelButton(w)).toBeUndefined();
		});

		it("shows cancel button when can_cancel is absent and neither cancelling nor ended (legacy admin fallback)", async () => {
			// Old admin versions lack the can_cancel field; fallback to !cancelling && !ended
			const acct = baseAccount({
				cancel_at_period_end: false,
				subscription_status: "Active",
			});
			delete acct.can_cancel;
			api.getAccount.mockResolvedValue(acct);
			const w = mount(PlanBillingPane);
			await flushPromises();
			expect(cancelButton(w)).toBeDefined();
		});

		it("hides cancel button when can_cancel is absent and cancelling", async () => {
			const acct = baseAccount({ cancel_at_period_end: true });
			delete acct.can_cancel;
			api.getAccount.mockResolvedValue(acct);
			const w = mount(PlanBillingPane);
			await flushPromises();
			expect(cancelButton(w)).toBeUndefined();
		});

		it("hides cancel button when can_cancel is absent and ended", async () => {
			const acct = baseAccount({ subscription_status: "Expired" });
			delete acct.can_cancel;
			api.getAccount.mockResolvedValue(acct);
			const w = mount(PlanBillingPane);
			await flushPromises();
			expect(cancelButton(w)).toBeUndefined();
		});
	});

	describe("cancel button label", () => {
		it("shows 'Cancel auto-renewal' for mandate customers", async () => {
			const w = await mountPane({ has_mandate: true, can_cancel: true });
			const btn = cancelButton(w);
			expect(btn?.text()).toContain("Cancel auto-renewal");
		});

		it("shows 'Cancel subscription' for non-mandate customers", async () => {
			const w = await mountPane({ has_mandate: false, can_cancel: true });
			const btn = cancelButton(w);
			expect(btn?.text()).toContain("Cancel subscription");
		});
	});

	describe("cancel confirmation dialog", () => {
		it("shows autopay-specific message for mandate customers", async () => {
			const w = await mountPane({
				has_mandate: true,
				can_cancel: true,
				access_ends_on: "2026-09-15",
			});
			const btn = cancelButton(w);
			await btn?.trigger("click");
			await flushPromises();

			expect(confirm).toHaveBeenCalled();
			const { message } = confirm.mock.calls[0][0];
			expect(message).toContain("Auto-renewal will turn off");
			expect(message).toContain("can set it up again anytime");
			expect(message).toContain("2026-09-15");
		});

		it("shows autopay message without date when access_ends_on is missing", async () => {
			const w = await mountPane({ has_mandate: true, can_cancel: true, access_ends_on: "" });
			const btn = cancelButton(w);
			await btn?.trigger("click");
			await flushPromises();

			expect(confirm).toHaveBeenCalled();
			const { message } = confirm.mock.calls[0][0];
			expect(message).toContain("Auto-renewal will turn off");
			expect(message).toContain("can set it up again anytime");
		});

		it("shows period-end message for non-mandate customers", async () => {
			const w = await mountPane({
				has_mandate: false,
				can_cancel: true,
				access_ends_on: "2026-09-15",
			});
			const btn = cancelButton(w);
			await btn?.trigger("click");
			await flushPromises();

			expect(confirm).toHaveBeenCalled();
			const { message } = confirm.mock.calls[0][0];
			expect(message).toContain("You'll keep full access until 2026-09-15");
			expect(message).toContain("resume any time");
			expect(message).not.toContain("Auto-renewal");
		});

		it("confirms with danger flag", async () => {
			const w = await mountPane({ has_mandate: true, can_cancel: true });
			const btn = cancelButton(w);
			await btn?.trigger("click");
			await flushPromises();

			expect(confirm).toHaveBeenCalled();
			const opts = confirm.mock.calls[0][0];
			expect(opts.danger).toBe(true);
		});
	});

	describe("cancel action", () => {
		it("calls API and reloads account on confirm", async () => {
			confirm.mockResolvedValue(true);
			api.cancelPlanAtPeriodEnd.mockResolvedValue({});
			const acct = baseAccount({ has_mandate: true, can_cancel: true });
			api.getAccount.mockResolvedValueOnce(acct);
			const w = await mountPane({ has_mandate: true, can_cancel: true });

			const btn = cancelButton(w);
			await btn?.trigger("click");
			await flushPromises();

			expect(api.cancelPlanAtPeriodEnd).toHaveBeenCalled();
			expect(api.getAccount).toHaveBeenCalledTimes(2); // Initial load + reload
		});

		it("does nothing on cancel/dismiss", async () => {
			confirm.mockResolvedValue(false);
			const w = await mountPane({ has_mandate: true, can_cancel: true });

			const btn = cancelButton(w);
			await btn?.trigger("click");
			await flushPromises();

			expect(api.cancelPlanAtPeriodEnd).not.toHaveBeenCalled();
		});
	});
});
