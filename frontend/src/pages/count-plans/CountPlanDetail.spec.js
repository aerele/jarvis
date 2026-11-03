import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

const countApi = vi.hoisted(() => ({
	correctCounts: vi.fn(),
	getCountPlan: vi.fn(),
	recordCounts: vi.fn(),
}));
vi.mock("@/api/countPlans", () => countApi);
vi.mock("@/lib/errors", () => ({
	errMessage: (cause, fallback) => cause?.message || fallback,
}));
vi.mock("@/utils/datetime", () => ({
	siteTimezone: () => "Europe/Berlin",
	toSiteDatetime: (value) => value,
}));
vi.mock("@/data/session", () => ({ session: { user: "counter@example.com" } }));
vi.mock("frappe-ui", () => ({
	Badge: { name: "Badge", props: ["label"], template: "<span>{{ label }}</span>" },
	Breadcrumbs: { name: "Breadcrumbs", props: ["items"], template: "<div />" },
	Button: {
		name: "Button",
		props: ["label", "loading", "disabled"],
		emits: ["click"],
		template:
			'<button :disabled="disabled" :data-label="label" @click="$emit(\'click\')">{{ label }}</button>',
	},
}));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: {
		name: "LayoutHeader",
		template: '<div><slot name="left-header" /><slot name="right-header" /></div>',
	},
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: "<div />" },
}));

import CountPlanDetail from "./CountPlanDetail.vue";

const plan = {
	request: "AR-COUNT-1",
	title: "North count",
	company: "Example Ltd",
	warehouse: "Example - North",
	can_record: 1,
	lines: [
		{
			item_code: "ITEM-ONE",
			stock_uom: "Nos",
			capture_required: 1,
			current_book_qty: "5",
			count_name: null,
		},
	],
};

beforeEach(() => {
	vi.clearAllMocks();
	sessionStorage.clear();
	countApi.getCountPlan.mockResolvedValue(plan);
});

describe("CountPlanDetail", () => {
	it("keeps the main label target during loading and an initial load error", async () => {
		let resolvePlan;
		countApi.getCountPlan.mockImplementationOnce(
			() =>
				new Promise((resolve) => {
					resolvePlan = resolve;
				})
		);
		const loadingWrapper = mount(CountPlanDetail, { props: { request: "AR-COUNT-1" } });
		await loadingWrapper.vm.$nextTick();

		expect(loadingWrapper.get("main").attributes("aria-labelledby")).toBe("count-plan-title");
		expect(loadingWrapper.get("#count-plan-title").text()).toBe("Physical count plan");

		resolvePlan(plan);
		await flushPromises();
		loadingWrapper.unmount();

		countApi.getCountPlan.mockRejectedValueOnce(new Error("Plan load failed"));
		const errorWrapper = mount(CountPlanDetail, { props: { request: "AR-COUNT-1" } });
		await flushPromises();

		expect(errorWrapper.get("main").attributes("aria-labelledby")).toBe("count-plan-title");
		expect(errorWrapper.find("#count-plan-title").exists()).toBe(true);
		expect(errorWrapper.text()).toContain("Plan load failed");
	});

	it("keeps an unfinished observation in the active user's browser scope", async () => {
		const wrapper = mount(CountPlanDetail, { props: { request: "AR-COUNT-1" } });
		await flushPromises();
		await wrapper.get('input[placeholder="Leave blank"]').setValue("3");
		await flushPromises();

		const saved = JSON.parse(
			sessionStorage.getItem("jarvis-count-plan:counter@example.com:AR-COUNT-1")
		);
		expect(saved.quantities).toMatchObject({ "ITEM-ONE": "3" });
	});

	it("retries an unconfirmed save with the same key and explicit zero quantity", async () => {
		countApi.recordCounts.mockRejectedValueOnce({ message: "Network interruption" });
		countApi.recordCounts.mockResolvedValueOnce({
			saved: [
				{ name: "JPC-1", entry_key: "unused", item_codes: ["ITEM-ONE"], idempotent: 1 },
			],
		});
		const wrapper = mount(CountPlanDetail, { props: { request: "AR-COUNT-1" } });
		await flushPromises();

		await wrapper.get('input[placeholder="Leave blank"]').setValue("0");
		await wrapper.get('[data-label="Save physical count"]').trigger("click");
		await flushPromises();

		expect(countApi.recordCounts).toHaveBeenCalledTimes(1);
		const firstCall = countApi.recordCounts.mock.calls[0];
		expect(firstCall[0]).toBe("AR-COUNT-1");
		expect(firstCall[2]).toEqual([{ item_code: "ITEM-ONE", counted_qty: "0" }]);
		expect(firstCall[3]).toMatch(/^ccp_/);
		expect(wrapper.text()).toContain("The save result is not confirmed.");

		await wrapper.get('[data-label="Retry same save"]').trigger("click");
		await flushPromises();

		expect(countApi.recordCounts).toHaveBeenCalledTimes(2);
		const retryCall = countApi.recordCounts.mock.calls[1];
		expect(retryCall[1]).toBe(firstCall[1]);
		expect(retryCall[2]).toEqual(firstCall[2]);
		expect(retryCall[3]).toBe(firstCall[3]);
		expect(wrapper.text()).toContain("This physical-count record was already saved.");
	});

	it("does not offer another save after every planned item has a submitted observation", async () => {
		countApi.getCountPlan.mockResolvedValue({
			...plan,
			lines: [{ ...plan.lines[0], count_name: "JPC-1" }],
		});
		const wrapper = mount(CountPlanDetail, { props: { request: "AR-COUNT-1" } });
		await flushPromises();

		expect(wrapper.text()).toContain(
			"Every planned item already has a submitted physical-count observation."
		);
		expect(wrapper.find('[data-label="Save physical count"]').exists()).toBe(false);
	});

	it("lets the recorder correct one current observation through the controlled endpoint", async () => {
		countApi.getCountPlan.mockResolvedValue({
			...plan,
			lines: [{ ...plan.lines[0], count_name: "JPC-1" }],
			active_counts: [
				{
					name: "JPC-1",
					can_correct: 1,
					recorded_by: "counter@example.com",
					counted_at: "2026-10-31 08:30:00",
					recorded_at: "2026-10-31 09:00:00",
					items: [{ item_code: "ITEM-ONE", counted_qty: "2", stock_uom: "Nos" }],
				},
			],
		});
		countApi.correctCounts.mockResolvedValue({
			saved: [
				{ name: "JPC-2", entry_key: "unused", item_codes: ["ITEM-ONE"], idempotent: 0 },
			],
		});
		const wrapper = mount(CountPlanDetail, { props: { request: "AR-COUNT-1" } });
		await flushPromises();

		expect(wrapper.text()).toContain("Observation JPC-1");
		expect(wrapper.text()).toContain("Recorded by counter@example.com");
		expect(wrapper.text()).toContain("Observed at 2026-10-31 08:30:00");
		expect(wrapper.text()).toContain("Recorded at 2026-10-31 09:00:00");
		expect(wrapper.text()).toContain("ITEM-ONE: 2 Nos");
		await wrapper.get('[data-label="Correct JPC-1"]').trigger("click");
		const input = wrapper.get('input[placeholder="Leave blank"]');
		expect(input.element.value).toBe("2");
		expect(input.attributes("aria-label")).toBe("Physical count for ITEM-ONE (Nos)");
		expect(wrapper.text()).toContain("Recorded by");
		expect(wrapper.text()).toContain("counter@example.com");
		expect(wrapper.text()).toContain("2026-10-31 08:30:00");
		expect(wrapper.text()).toContain("2026-10-31 09:00:00");
		expect(wrapper.text()).toContain("ITEM-ONE: 2 Nos");
		expect(wrapper.text()).toContain(
			"Saving cancels this current observation and creates a replacement observation."
		);
		await input.setValue("3");
		await wrapper.get('[data-label="Save corrected count"]').trigger("click");
		await flushPromises();

		expect(countApi.correctCounts).toHaveBeenCalledTimes(1);
		const call = countApi.correctCounts.mock.calls[0];
		expect(call[0]).toBe("AR-COUNT-1");
		expect(call[1]).toBe("JPC-1");
		expect(call[3]).toEqual([{ item_code: "ITEM-ONE", counted_qty: "3" }]);
		expect(call[4]).toMatch(/^ccp_/);
	});

	it("keeps correction controls usable when optional observation details are absent", async () => {
		countApi.getCountPlan.mockResolvedValue({
			...plan,
			lines: [{ ...plan.lines[0], count_name: "JPC-1" }],
			active_counts: [
				{
					name: "JPC-1",
					can_correct: 1,
					items: [{ item_code: "ITEM-ONE", counted_qty: "2" }],
				},
			],
		});
		const wrapper = mount(CountPlanDetail, { props: { request: "AR-COUNT-1" } });
		await flushPromises();

		expect(wrapper.find('[data-label="Correct JPC-1"]').exists()).toBe(true);
		expect(wrapper.text()).toContain("ITEM-ONE: 2");
		expect(wrapper.text()).not.toContain("Recorded by counter@example.com");

		await wrapper.get('[data-label="Correct JPC-1"]').trigger("click");
		expect(wrapper.get('input[placeholder="Leave blank"]').element.value).toBe("2");
	});

	it("reloads the approved plan when the route request changes", async () => {
		countApi.getCountPlan.mockImplementation(async (request) => ({
			...plan,
			request,
			title: request,
			warehouse: request + " warehouse",
		}));
		const wrapper = mount(CountPlanDetail, { props: { request: "AR-COUNT-1" } });
		await flushPromises();
		await wrapper.setProps({ request: "AR-COUNT-2" });
		await flushPromises();

		expect(countApi.getCountPlan).toHaveBeenLastCalledWith("AR-COUNT-2", expect.any(String));
		expect(wrapper.text()).toContain("AR-COUNT-2 warehouse");
	});
});
