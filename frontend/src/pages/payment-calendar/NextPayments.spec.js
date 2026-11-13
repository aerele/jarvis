import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import NextPayments from "./NextPayments.vue";
import { modernStatus } from "../../../tests/payment-calendar/headlineFixtures";

const ROWS = [
	{ date: "2026-11-11", remaining: "42048.00", payees: 3, rows: 3 },
	{ date: "2026-11-12", remaining: "6994.00", payees: 1, rows: 1 },
	{ date: "2026-11-13", remaining: "0.00", payees: 1, rows: 1 },
	{ date: "2026-11-14", remaining: "15000.00", payees: 2, rows: 2 },
	{ date: "2026-11-16", remaining: "8000.00", payees: 1, rows: 1 },
	{ date: "2026-11-17", remaining: "9000.00", payees: 1, rows: 1 },
	{ date: "2026-12-01", remaining: "70000.00", payees: 1, rows: 1 },
];

function render(props = {}) {
	return mount(NextPayments, {
		props: {
			snapshot: modernStatus(),
			selected: "2026-11-11",
			month: "2026-11",
			rows: ROWS,
			...props,
		},
	});
}
const dates = (wrapper) =>
	wrapper.findAll("[data-upcoming]").map((n) => n.attributes("data-upcoming"));

describe("NextPayments", () => {
	it("shows the heading with the displayed month", () => {
		const wrapper = render();
		expect(wrapper.get("h2").text()).toBe("Next payments");
		expect(wrapper.get("#upcoming-month").text()).toBe("November 2026");
	});

	it("lists up to three dates after the selected date with a payment, in the month", () => {
		const wrapper = render();
		// 11 Nov is the selected date, 13 Nov pays nothing, December is another month
		expect(dates(wrapper)).toEqual(["2026-11-12", "2026-11-14", "2026-11-16"]);
	});

	it("writes the date, record count and amount of a row", () => {
		const wrapper = render();
		const [first, second] = wrapper.findAll("[data-upcoming]");
		expect(first.text()).toBe("12 Nov1 record₹6,994.00");
		expect(second.text()).toBe("14 Nov2 records₹15,000.00");
	});

	it("selects a row's date on the Payable side", async () => {
		const wrapper = render();
		await wrapper.get('[data-upcoming="2026-11-14"]').trigger("click");
		expect(wrapper.emitted("select")).toEqual([["2026-11-14"]]);
	});

	it("moves the rows with the selected date", async () => {
		const wrapper = render({ selected: "2026-11-14" });
		expect(dates(wrapper)).toEqual(["2026-11-16", "2026-11-17"]);
		await wrapper.setProps({ selected: "2026-11-30" });
		expect(dates(wrapper)).toEqual([]);
	});

	it("asks the page for the List view, and disables the footer with no rows", async () => {
		const wrapper = render();
		const more = wrapper.get("#all-payments");
		expect(more.text()).toBe("View remaining dates");
		expect(more.find("svg.pcal-arrow").exists()).toBe(true);
		await more.trigger("click");
		expect(wrapper.emitted("view-all")).toHaveLength(1);
		await wrapper.setProps({ rows: [] });
		expect(wrapper.get("#all-payments").attributes("disabled")).toBeDefined();
	});

	it("says so when nothing is scheduled after the date", () => {
		const wrapper = render({ rows: [] });
		expect(wrapper.get(".schedule-empty").text()).toBe(
			"No payments scheduled after this date in November 2026."
		);
	});

	it("says the schedule is unavailable when the Payable side is", () => {
		const snapshot = modernStatus();
		snapshot.unavailable = { Payable: "payables_unavailable" };
		const wrapper = render({ snapshot, rows: [] });
		expect(wrapper.get(".schedule-empty").text()).toBe(
			"Payment schedule unavailable for November 2026."
		);
	});

	it("shows a loading note and a load-error note instead of an empty claim", async () => {
		const wrapper = render({ rows: [], loading: true });
		expect(wrapper.get(".schedule-empty").text()).toBe("Loading upcoming payments…");
		await wrapper.setProps({ loading: false, loadError: true });
		expect(wrapper.get(".schedule-empty").text()).toBe(
			"Payments will appear after this month is loaded."
		);
	});

	it("treats a snapshot of another month as loading, not as unavailable", async () => {
		const snapshot = modernStatus();
		snapshot.unavailable = { Payable: "payables_unavailable" };
		const wrapper = render({ snapshot, month: "2026-12", selected: "2026-12-01", rows: [] });
		expect(wrapper.get(".schedule-empty").text()).toBe("Loading upcoming payments…");
		expect(wrapper.get("#all-payments").attributes("disabled")).toBeDefined();
		await wrapper.setProps({ snapshot: modernStatus("month_dec") });
		expect(wrapper.get(".schedule-empty").text()).toBe(
			"No payments scheduled after this date in December 2026."
		);
	});
});
