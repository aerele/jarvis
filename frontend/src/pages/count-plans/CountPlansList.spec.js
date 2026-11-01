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

beforeEach(() => vi.clearAllMocks());

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
});
