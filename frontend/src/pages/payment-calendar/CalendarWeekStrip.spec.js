import { afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import CalendarWeekStrip from "./CalendarWeekStrip.vue";
import fs from "node:fs";
import path from "node:path";

const SCOPE = { selectable_start: "2025-11-01", selectable_end: "2027-10-31" };
const DAYS = {
	"2026-11-10": { payable_remaining: "0.00", receivable_remaining: "0.00" },
	"2026-11-11": { payable_remaining: "42048.00", receivable_remaining: "86250.00" },
	"2026-11-12": { payable_remaining: "0.00", receivable_remaining: "19500.00" },
};

describe("Payment Calendar week strip", () => {
	let wrapper;
	afterEach(() => wrapper?.unmount());
	function render(props = {}) {
		wrapper = mount(CalendarWeekStrip, {
			props: { selected: "2026-11-11", days: DAYS, scope: SCOPE, ...props },
		});
		return wrapper;
	}
	function swipe(from, to) {
		const strip = wrapper.get(".week-strip");
		strip.trigger("touchstart", { changedTouches: [{ clientX: from }] });
		return strip.trigger("touchend", { changedTouches: [{ clientX: to }] });
	}
	const selections = () => (wrapper.emitted("select") || []).map(([date]) => date);

	it("shows the Monday-start week of the selected date", () => {
		render();
		const dates = wrapper
			.findAll("[data-week-date]")
			.map((b) => b.attributes("data-week-date"));
		expect(dates).toEqual([
			"2026-11-09",
			"2026-11-10",
			"2026-11-11",
			"2026-11-12",
			"2026-11-13",
			"2026-11-14",
			"2026-11-15",
		]);
		const first = wrapper.get('[data-week-date="2026-11-09"]');
		expect(first.text()).toContain("Mon");
		expect(first.text()).toContain("9");
	});
	it("marks the selected day pressed and shows an activity dot only when pay or receive is not zero", () => {
		render();
		expect(wrapper.get('[data-week-date="2026-11-11"]').attributes("aria-pressed")).toBe(
			"true"
		);
		expect(wrapper.get('[data-week-date="2026-11-12"]').attributes("aria-pressed")).toBe(
			"false"
		);
		const dot = (date) => wrapper.get(`[data-week-date="${date}"] i`).classes();
		expect(dot("2026-11-11")).toContain("activity");
		expect(dot("2026-11-12")).toContain("activity");
		expect(dot("2026-11-10")).not.toContain("activity");
		expect(dot("2026-11-13")).not.toContain("activity");
	});
	it("selects a tapped day without a side", async () => {
		render();
		await wrapper.get('[data-week-date="2026-11-13"]').trigger("click");
		expect(wrapper.emitted("select")).toEqual([["2026-11-13"]]);
	});
	it("steps a week with the arrow buttons", async () => {
		render();
		await wrapper.get('[aria-label="Previous week"]').trigger("click");
		await wrapper.get('[aria-label="Next week"]').trigger("click");
		expect(selections()).toEqual(["2026-11-04", "2026-11-18"]);
	});
	it("disables the arrow whose target week start is out of range", () => {
		render({
			selected: "2026-11-11",
			scope: { ...SCOPE, selectable_start: "2026-11-09", selectable_end: "2026-11-15" },
		});
		expect(wrapper.get('[aria-label="Previous week"]').attributes("disabled")).toBeDefined();
		expect(wrapper.get('[aria-label="Next week"]').attributes("disabled")).toBeDefined();
	});
	it("disables a day outside the selectable range", () => {
		render({ scope: { ...SCOPE, selectable_start: "2026-11-11" } });
		expect(wrapper.get('[data-week-date="2026-11-10"]').attributes("disabled")).toBeDefined();
		expect(
			wrapper.get('[data-week-date="2026-11-11"]').attributes("disabled")
		).toBeUndefined();
	});
	it("moves forward a week when swiped left by more than 50 px", async () => {
		render();
		await swipe(200, 140);
		expect(selections()).toEqual(["2026-11-18"]);
	});
	it("moves back a week when swiped right by more than 50 px", async () => {
		render();
		await swipe(100, 160);
		expect(selections()).toEqual(["2026-11-04"]);
	});
	it.each([
		[200, 180],
		[200, 150],
		[150, 200],
		[200, 200],
	])("does nothing for a %i to %i swipe of 50 px or less", async (from, to) => {
		render();
		await swipe(from, to);
		expect(selections()).toEqual([]);
	});
	it("does nothing when a touch ends without a start", async () => {
		render();
		await wrapper
			.get(".week-strip")
			.trigger("touchend", { changedTouches: [{ clientX: 10 }] });
		expect(selections()).toEqual([]);
	});
	it("scopes every style rule under .pcal-cal so it beats the toolbar's shared button rules", () => {
		const css = fs.readFileSync(path.resolve(__dirname, "CalendarWeekStrip.css"), "utf8");
		const selectors = [
			...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{/g),
		].flatMap(([, list]) => list.split(",").map((selector) => selector.trim()));
		expect(selectors.length).toBeGreaterThan(5);
		for (const selector of selectors)
			expect(selector).toMatch(/^(\[data-theme="dark"\] )?\.pcal-cal /);
	});
	it("marks the trough target day", () => {
		render({ troughDate: "2026-11-12" });
		expect(wrapper.get('[data-week-date="2026-11-12"]').classes()).toContain("trough-target");
		expect(wrapper.findAll(".trough-target")).toHaveLength(1);
	});
});
