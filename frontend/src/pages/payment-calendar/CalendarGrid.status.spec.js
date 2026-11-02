import { afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import CalendarGrid from "./CalendarGrid.vue";
import { complete } from "../../../tests/payment-calendar/fixtures";

describe("Calendar payment-status colours", () => {
	let wrapper;
	afterEach(() => wrapper?.unmount());
	function render(status, risk = "green", extra = {}) {
		const { props, ...snapshotExtra } = extra;
		const snapshot = complete();
		snapshot.projection.payable_settlement_complete = true;
		Object.assign(snapshot.projection.days["2026-10-24"], { settlement: status, risk });
		Object.assign(snapshot, snapshotExtra);
		wrapper = mount(CalendarGrid, {
			props: { snapshot, selected: "2026-10-24", month: "2026-10", ...props },
		});
		return wrapper.get('[data-date="2026-10-24"]');
	}
	it.each([
		["Paid", "green"],
		["Settled", "green"],
		["Partially paid", "orange"],
		["Partially settled", "orange"],
		["Unpaid", "red"],
	])("colours %s by settlement despite the opposite cash risk", (status, colour) => {
		const day = render(status, colour === "green" ? "red" : "green");
		expect(day.classes()).toContain(`settlement-${colour}`);
		expect(day.attributes("aria-label")).toContain(status);
		expect(day.attributes("aria-label")).not.toMatch(/Below zero|minimum|Forecast/);
	});
	it.each(["No dues", "Unavailable", "Unknown", undefined])("keeps %s neutral", (status) => {
		const day = render(status);
		expect(day.classes().join(" ")).not.toMatch(/settlement-(red|orange|green)|risk-bg/);
	});
	it.each([false, undefined, "true", 1])(
		"refuses unverified payable completeness %s",
		(flag) => {
			const snapshot = complete();
			snapshot.projection.payable_settlement_complete = flag;
			snapshot.projection.days["2026-10-24"].settlement = "Settled";
			const day = render("Settled", "green", snapshot);
			expect(day.classes().join(" ")).not.toMatch(/settlement-(red|orange|green)/);
			expect(day.text()).toContain("Unavailable");
		}
	);
	it("colours known payments when cash, minimum or receivables are unavailable", () => {
		const snapshot = complete();
		snapshot.projection.payable_settlement_complete = true;
		snapshot.projection.forecast_available = false;
		snapshot.projection.scope_reasons = [
			"bank_unavailable",
			"floor_unconfigured",
			"receivables_unavailable",
		];
		snapshot.unavailable = { Receivable: "forbidden" };
		expect(render("Unpaid", null, snapshot).classes()).toContain("settlement-red");
	});
	it("distinguishes a zero settled date from a zero no-dues date", async () => {
		const day = render("Settled");
		const snapshot = wrapper.props("snapshot");
		snapshot.projection.days["2026-10-24"].payable_remaining = "0.00";
		snapshot.projection.days["2026-10-25"].settlement = "No dues";
		snapshot.projection.days["2026-10-25"].payable_remaining = "0.00";
		await wrapper.setProps({ snapshot: { ...snapshot } });
		expect(day.classes()).toContain("settlement-green");
		expect(wrapper.get('[data-date="2026-10-25"]').classes().join(" ")).not.toMatch(
			/settlement-(red|orange|green)/
		);
	});
	it("colours known historical settlement and removes calendar cash labels and counts", async () => {
		render("Unpaid");
		const snapshot = wrapper.props("snapshot");
		snapshot.projection.days["2026-10-23"] = {
			...snapshot.projection.days["2026-10-24"],
			settlement: "Settled",
			risk: null,
		};
		await wrapper.setProps({ snapshot: { ...snapshot } });
		expect(wrapper.get('[data-date="2026-10-23"]').classes()).toContain("settlement-green");
		expect(wrapper.text()).not.toMatch(
			/Cash OK|Short|Low|minimum|projected closing cash|invoice rows/
		);
		expect(wrapper.find(".calendar-count").exists()).toBe(false);
	});
	it("uses the same settlement colours and exact accessible amounts in list mode", () => {
		const day = render("Unpaid", "green", { props: { mode: "list" } });
		expect(day.classes()).toContain("settlement-red");
		expect(day.attributes("aria-label")).toContain("₹10.00");
		expect(day.text()).not.toMatch(/minimum|Forecast/);
	});
	it("keeps unavailable payables neutral even if an inconsistent flag says complete", () => {
		expect(
			render("Settled", "green", { unavailable: { Payable: "forbidden" } })
				.classes()
				.join(" ")
		).not.toMatch(/settlement-(red|orange|green)/);
	});
	it("keeps identical payment status unchanged when cash risk changes", async () => {
		const day = render("Unpaid", "red");
		const snapshot = wrapper.props("snapshot");
		for (const risk of ["orange", "green", null]) {
			snapshot.projection.days["2026-10-24"].risk = risk;
			await wrapper.setProps({ snapshot: { ...snapshot } });
			expect(day.classes()).toContain("settlement-red");
			expect(day.attributes("aria-label")).toContain("Unpaid");
		}
	});
	it("does not certify a missing reconciliation or an out-of-range date", async () => {
		render("Settled");
		const snapshot = wrapper.props("snapshot");
		snapshot.projection.reconciliation.Payable = null;
		await wrapper.setProps({ snapshot: { ...snapshot } });
		expect(wrapper.get('[data-date="2026-10-24"]').classes().join(" ")).not.toMatch(
			/settlement-(red|orange|green)/
		);
		snapshot.projection.reconciliation.Payable = {};
		snapshot.projection.days["2027-10-01"] = {
			settlement: "Settled",
			payable_remaining: "0.00",
		};
		await wrapper.setProps({ snapshot: { ...snapshot }, month: "2027-09" });
		const outside = wrapper.get('[data-date="2027-10-01"]');
		expect(outside.attributes("disabled")).toBeDefined();
		expect(outside.classes().join(" ")).not.toMatch(/settlement-(red|orange|green)/);
	});
});
