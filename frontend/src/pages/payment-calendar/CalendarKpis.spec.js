import { vi } from "vitest";
import { mount } from "@vue/test-utils";
import CalendarKpis from "./CalendarKpis.vue";
import { complete } from "../../../tests/payment-calendar/fixtures";

describe("Payment Calendar KPIs", () => {
	it("displays source values and never derives a missing projection", () => {
		const snapshot = complete();
		const day = {
			...snapshot.projection.days["2026-10-24"],
			projected_closing: null,
			projected_opening: null,
			risk: null,
		};
		const wrapper = mount(CalendarKpis, { props: { snapshot, day, selected: "2026-10-24" } });
		expect(wrapper.get('[data-kpi="cash"]').text()).toContain("₹100.00");
		expect(wrapper.get('[data-kpi="closing"]').text()).toContain("—");
		expect(wrapper.get('[data-kpi="closing"]').text()).not.toContain("At or above");
		wrapper.unmount();
	});
	it("identifies historical forecast unavailability", () => {
		const snapshot = complete();
		const wrapper = mount(CalendarKpis, {
			props: { snapshot, day: null, selected: "2026-10-20" },
		});
		expect(wrapper.text()).toContain("Forecast unavailable before");
		wrapper.unmount();
	});
	it("shows the carried opening and the snapshot minimum alongside closing", () => {
		const snapshot = complete();
		const wrapper = mount(CalendarKpis, {
			props: {
				snapshot,
				day: snapshot.projection.days["2026-10-24"],
				selected: "2026-10-24",
			},
		});
		expect(wrapper.get('[data-kpi="closing"]').text()).toContain("Opening ₹100.00");
		expect(wrapper.get('[data-kpi="closing"]').text()).toContain("Minimum ₹20.00");
		wrapper.unmount();
	});
	it("shows the server due-now composition on the anchor date", () => {
		const snapshot = complete();
		snapshot.projection.due_now = {
			total: "590000.00",
			overdue: "400000.00",
			undated: "100000.00",
		};
		const wrapper = mount(CalendarKpis, {
			props: {
				snapshot,
				day: snapshot.projection.days["2026-10-24"],
				selected: "2026-10-24",
			},
		});
		expect(wrapper.get('[data-kpi="payable"]').text()).toContain("Payable now ₹5,90,000.00");
		expect(wrapper.get('[data-kpi="payable"]').text()).toContain("Overdue ₹4,00,000.00");
		expect(wrapper.get('[data-kpi="payable"]').text()).toContain("Undated ₹1,00,000.00");
		wrapper.unmount();
	});
	it("preserves the KPI footprint and wide layout while loading blank amounts", async () => {
		const snapshot = complete();
		snapshot.projection.book_cash = "123456789012345.00";
		const wrapper = mount(CalendarKpis, {
			props: { snapshot, selected: "2026-10-24", day: null },
		});
		const bounds = vi
			.spyOn(wrapper.element, "getBoundingClientRect")
			.mockReturnValue({ height: 240 });
		await wrapper.setProps({ snapshot: { ...snapshot } });
		expect(wrapper.element.style.minHeight).toBe("");
		await wrapper.setProps({
			loading: true,
			snapshot: { ...snapshot, projection: { ...snapshot.projection, book_cash: null } },
		});
		expect(wrapper.classes()).toContain("long-values");
		expect(wrapper.element.style.minHeight).toBe("240px");
		bounds.mockRestore();
		wrapper.unmount();
	});
});
