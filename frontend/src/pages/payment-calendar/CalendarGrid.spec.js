import { afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import CalendarGrid from "./CalendarGrid.vue";
import { complete } from "../../../tests/payment-calendar/fixtures";

describe("Payment Calendar grid and look-ahead limits", () => {
	let wrapper;
	afterEach(() => wrapper?.unmount());
	function render(extra = {}) {
		const snapshot = complete();
		wrapper = mount(CalendarGrid, {
			props: { snapshot, selected: "2026-10-24", month: "2026-10", mode: "month", ...extra },
			attachTo: document.body,
		});
		return wrapper;
	}
	it("exposes settlement and exact amounts without cash-risk labels", () => {
		render();
		const day = wrapper.get('[data-date="2026-10-24"]');
		expect(day.attributes("aria-label")).toContain("₹10.00");
		expect(day.attributes("aria-label")).toContain("Unpaid");
		expect(day.attributes("aria-label")).not.toContain("At or above minimum");
		expect(day.attributes("aria-current")).toBe("date");
	});
	it("gives one date a tab stop and moves focus by keyboard", async () => {
		render();
		const day = wrapper.get('[data-date="2026-10-24"]');
		day.element.focus();
		await day.trigger("keydown", { key: "ArrowRight" });
		expect(document.activeElement.dataset.date).toBe("2026-10-25");
		expect(wrapper.findAll('[data-date][tabindex="0"]')).toHaveLength(1);
		await wrapper.get('[data-date="2026-10-25"]').trigger("keydown", { key: "Enter" });
		expect(wrapper.emitted("select").at(-1)).toEqual(["2026-10-25"]);
	});
	it("disables next-month navigation and carry-window cells at the last month", () => {
		render({ month: "2027-09" });
		expect(wrapper.get('[aria-label="Next month"]').attributes("disabled")).toBeDefined();
		expect(wrapper.get('[data-date="2027-10-01"]').attributes("disabled")).toBeDefined();
	});
	it("never announces a liquidity colour for unavailable data", () => {
		const snapshot = complete();
		snapshot.projection.days["2026-10-24"].risk = null;
		render({ snapshot });
		expect(wrapper.get('[data-date="2026-10-24"]').attributes("aria-label")).not.toContain(
			"At or above minimum"
		);
	});
	it("offers a readable list mode", () => {
		render({ mode: "list" });
		expect(wrapper.find('[role="grid"]').exists()).toBe(false);
		expect(wrapper.get('[data-date="2026-10-24"]').text()).toContain("Unpaid");
	});
	it("provides a visible payment status independent of colour", () => {
		const snapshot = complete();
		snapshot.projection.days["2026-10-24"].risk = "red";
		render({ snapshot });
		const marker = wrapper.get('[data-date="2026-10-24"] .calendar-settlement');
		expect(marker.text()).toContain("Unpaid");
		expect(marker.classes()).not.toContain("sr-only");
	});
	it("moves one row at a time in list mode", async () => {
		render({ mode: "list" });
		const day = wrapper.get('[data-date="2026-10-24"]');
		day.element.focus();
		await day.trigger("keydown", { key: "ArrowDown" });
		expect(document.activeElement.dataset.date).toBe("2026-10-25");
	});
});
