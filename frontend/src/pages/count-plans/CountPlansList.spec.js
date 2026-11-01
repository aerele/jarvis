import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

const countApi = vi.hoisted(() => ({ listCountPlans: vi.fn() }));
vi.mock("@/api/countPlans", () => countApi);
vi.mock("@/lib/errors", () => ({
	errMessage: (cause, fallback) => cause?.message || fallback,
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
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
	FeatherIcon: { name: "FeatherIcon", template: "<span />" },
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

import CountPlansList from "./CountPlansList.vue";

beforeEach(() => {
	vi.clearAllMocks();
	countApi.listCountPlans.mockReset();
});

describe("CountPlansList", () => {
	it("continues after an empty page caused by unreadable approved rows", async () => {
		countApi.listCountPlans
			.mockResolvedValueOnce({ rows: [], start: 0, next_start: 500, has_more: 1 })
			.mockResolvedValueOnce({
				rows: [
					{
						request: "AR-COUNT-1",
						title: "North count",
						company: "Example Ltd",
						warehouse: "Example - North",
						line_count: 1,
						can_record: 1,
					},
				],
				start: 500,
				next_start: 501,
				has_more: 0,
			});
		const wrapper = mount(CountPlansList);
		await flushPromises();

		expect(wrapper.text()).toContain("Some approved plans could not be read.");
		expect(countApi.listCountPlans).toHaveBeenCalledWith({ start: 0, pageLength: 20 });

		await wrapper.get('[data-label="Load more"]').trigger("click");
		await flushPromises();

		expect(countApi.listCountPlans).toHaveBeenLastCalledWith({ start: 500, pageLength: 20 });
		expect(wrapper.text()).toContain("North count");
	});

	it("does not call a fully recorded plan ready when optional count totals are present", async () => {
		countApi.listCountPlans.mockResolvedValueOnce({
			rows: [
				{
					request: "AR-COUNT-COMPLETE",
					title: "Complete count",
					company: "Example Ltd",
					warehouse: "Example - North",
					line_count: 2,
					recorded_line_count: 2,
					remaining_line_count: 0,
					can_record: 1,
				},
				{
					request: "AR-COUNT-LEGACY",
					title: "Legacy count",
					company: "Example Ltd",
					warehouse: "Example - South",
					line_count: 1,
					can_record: 1,
				},
			],
			start: 0,
			next_start: 2,
			has_more: 0,
		});
		const wrapper = mount(CountPlansList);
		await flushPromises();

		const completeRow = wrapper
			.findAll("button")
			.find((button) => button.text().includes("Complete count"));
		const legacyRow = wrapper
			.findAll("button")
			.find((button) => button.text().includes("Legacy count"));

		expect(completeRow?.text()).toContain("All items recorded");
		expect(completeRow?.text()).not.toContain("Ready to record");
		expect(legacyRow?.text()).toContain("Ready to record");
	});

	it("keeps visible rows and gives an inline refresh retry after a failed refresh", async () => {
		countApi.listCountPlans
			.mockResolvedValueOnce({
				rows: [
					{
						request: "AR-COUNT-1",
						title: "North count",
						company: "Example Ltd",
						warehouse: "Example - North",
						line_count: 1,
						can_record: 1,
					},
				],
				start: 0,
				next_start: 1,
				has_more: 0,
			})
			.mockRejectedValueOnce(new Error("Refresh failed"))
			.mockResolvedValueOnce({
				rows: [
					{
						request: "AR-COUNT-1",
						title: "North count",
						company: "Example Ltd",
						warehouse: "Example - North",
						line_count: 1,
						can_record: 1,
					},
				],
				start: 0,
				next_start: 1,
				has_more: 0,
			});
		const wrapper = mount(CountPlansList);
		await flushPromises();

		await wrapper.get('[data-label="Refresh"]').trigger("click");
		await flushPromises();

		expect(wrapper.text()).toContain("North count");
		expect(wrapper.text()).toContain("Refresh failed");
		expect(wrapper.find('[data-label="Retry refresh"]').exists()).toBe(true);

		await wrapper.get('[data-label="Retry refresh"]').trigger("click");
		await flushPromises();

		expect(wrapper.text()).toContain("North count");
		expect(wrapper.text()).not.toContain("Refresh failed");
	});

	it("keeps visible rows and gives an inline load-more retry after a failed page", async () => {
		countApi.listCountPlans
			.mockResolvedValueOnce({
				rows: [
					{
						request: "AR-COUNT-1",
						title: "North count",
						company: "Example Ltd",
						warehouse: "Example - North",
						line_count: 1,
						can_record: 1,
					},
				],
				start: 0,
				next_start: 1,
				has_more: 1,
			})
			.mockRejectedValueOnce(new Error("Load more failed"))
			.mockResolvedValueOnce({
				rows: [
					{
						request: "AR-COUNT-2",
						title: "South count",
						company: "Example Ltd",
						warehouse: "Example - South",
						line_count: 1,
						can_record: 1,
					},
				],
				start: 1,
				next_start: 2,
				has_more: 0,
			});
		const wrapper = mount(CountPlansList);
		await flushPromises();

		await wrapper.get('[data-label="Load more"]').trigger("click");
		await flushPromises();

		expect(wrapper.text()).toContain("North count");
		expect(wrapper.text()).toContain("Load more failed");
		expect(wrapper.find('[data-label="Retry load more"]').exists()).toBe(true);

		await wrapper.get('[data-label="Retry load more"]').trigger("click");
		await flushPromises();

		expect(wrapper.text()).toContain("North count");
		expect(wrapper.text()).toContain("South count");
		expect(wrapper.text()).not.toContain("Load more failed");
	});
});
