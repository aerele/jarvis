import { vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
vi.mock("@/api/paymentCalendar", async () =>
	(await import("../../../tests/payment-calendar/fixtures")).fakeApi()
);
vi.mock("@/utils/datetime", () => ({ siteTimezone: () => "Asia/Kolkata" }));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ mobile: false }) }));
vi.mock("vue-router", () => ({ useRoute: () => ({ query: {} }) }));
vi.mock("frappe-ui", () => ({
	Breadcrumbs: { template: "<nav>Dashboards</nav>", props: ["items"] },
	FeatherIcon: { template: "<span />", props: ["name"] },
	Dialog: {
		props: ["modelValue", "options"],
		template:
			'<div v-if="modelValue" role="dialog"><slot name="body-content"/><slot name="actions"/></div>',
	},
}));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { template: '<header><slot name="left-header"/></header>' },
}));
import PaymentCalendarPage from "./PaymentCalendarPage.vue";
import * as api from "@/api/paymentCalendar";
import { ok, refusal } from "../../../tests/payment-calendar/fixtures";

describe("Native Payment Calendar page", () => {
	let wrapper;
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-10-24T12:00:00Z"));
		vi.clearAllMocks();
	});
	afterEach(() => {
		wrapper?.unmount();
		vi.useRealTimers();
	});
	async function render() {
		wrapper = mount(PaymentCalendarPage);
		await flushPromises();
	}
	it("connects a native date selection to one snapshot and independent detail tabs", async () => {
		await render();
		expect(wrapper.text()).toContain("Payment Calendar");
		await wrapper.get('[data-date="2026-10-25"]').trigger("click");
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
		expect(api.getDayDetail.mock.calls.at(-1)[1]).toBe("2026-10-25");
		await wrapper.get(".pcal-tabs button:nth-child(3)").trigger("click");
		await flushPromises();
		expect(api.getDayDetail.mock.calls.at(-1)[2]).toBe("Receivable");
	});
	it("removes financial panels at expiry and offers explicit recovery", async () => {
		await render();
		expect(wrapper.find('[data-kpi="cash"]').exists()).toBe(true);
		await vi.advanceTimersByTimeAsync(300001);
		expect(wrapper.find('[data-kpi="cash"]').exists()).toBe(false);
		expect(wrapper.text()).toContain("Snapshot expired");
		expect(wrapper.text()).toContain("Refresh calendar");
	});
	it("keeps the entered floor after a concurrent update refusal", async () => {
		await render();
		await wrapper.get(".minimum-field button").trigger("click");
		await wrapper.get('[role="dialog"] input').setValue("44.00");
		api.configureFloor.mockResolvedValueOnce(refusal("settings_changed", "scope_changed"));
		await wrapper.get('[role="dialog"] .pcal-primary').trigger("click");
		await flushPromises();
		expect(wrapper.get('[role="dialog"] input').element.value).toBe("44.00");
		expect(wrapper.get('[role="dialog"]').text()).toContain("changed by another user");
	});
	it("hides the floor mutation action for an accounts reader", async () => {
		const normal = await api.getCapabilities("Manufacturing");
		api.getCapabilities
			.mockResolvedValueOnce(normal)
			.mockResolvedValueOnce(ok({ ...normal.data, can_configure: false }));
		await render();
		expect(wrapper.find(".minimum-field button").exists()).toBe(false);
	});
	it("clears floor and account controls when company access is revoked", async () => {
		await render();
		api.getDayDetail.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		await wrapper.get('[data-date="2026-10-25"]').trigger("click");
		await flushPromises();
		expect(wrapper.find(".minimum-field button").exists()).toBe(false);
		expect(wrapper.get(".minimum-value strong").text()).toBe("—");
		expect(wrapper.get(".pcal-select-button").attributes("disabled")).toBeDefined();
	});
});
