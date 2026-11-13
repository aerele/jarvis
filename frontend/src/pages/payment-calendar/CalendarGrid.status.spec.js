import { afterEach } from "vitest";
import { mountGrid } from "../../../tests/payment-calendar/gridHarness";
import { complete } from "../../../tests/payment-calendar/fixtures";

// Payment status shows as a dot and a word on a neutral cell; it never colours the cell.
describe("Calendar payment-status marker", () => {
	let wrapper;
	afterEach(() => wrapper?.unmount());
	function render(status, risk = "green", extra = {}) {
		const { props, ...snapshotExtra } = extra;
		const snapshot = complete();
		snapshot.projection.payable_settlement_complete = true;
		Object.assign(snapshot.projection.days["2026-10-24"], { settlement: status, risk });
		Object.assign(snapshot, snapshotExtra);
		wrapper = mountGrid({
			props: { snapshot, selected: "2026-10-24", month: "2026-10", ...props },
		});
		return wrapper.find(`[data-cell-date="2026-10-24"]`);
	}
	const cell = (date) => wrapper.get(`[data-cell-date="${date}"]`);
	const marker = (date) => cell(date).find(".status");
	const isNeutral = (date) => {
		const status = marker(date);
		return (
			!status.exists() ||
			(!status.find("i").exists() &&
				!/settled|partial|unpaid/.test(status.classes().join(" ")))
		);
	};

	it.each([
		["Paid", "settled", "Settled"],
		["Settled", "settled", "Settled"],
		["Partially paid", "partial", "Partial"],
		["Partially settled", "partial", "Partial"],
		["Unpaid", "unpaid", "Unpaid"],
	])("marks %s by settlement despite the opposite cash risk", (status, kind, word) => {
		const day = render(status, kind === "settled" ? "red" : "green");
		const dot = day.get(".status");
		expect(dot.classes()).toContain(kind);
		expect(dot.text()).toBe(word);
		expect(dot.find("i").exists()).toBe(true);
		const label = day.get("[data-date]").attributes("aria-label");
		expect(label).not.toMatch(/Below zero|minimum|Forecast/);
	});
	it.each(["No dues", "Unavailable", "Unknown", undefined])("keeps %s neutral", (status) => {
		const day = render(status);
		expect(isNeutral("2026-10-24")).toBe(true);
		expect(day.classes().join(" ")).not.toMatch(/settlement|risk|unpaid|partial|settled/);
	});
	it("hides the marker for No dues and names unverified data Unavailable", () => {
		render("No dues");
		expect(marker("2026-10-24").exists()).toBe(false);
		wrapper.unmount();
		render("Unknown");
		expect(marker("2026-10-24").text()).toBe("Unavailable");
	});
	it.each([false, undefined, "true", 1])(
		"refuses unverified payable completeness %s",
		(flag) => {
			const snapshot = complete();
			snapshot.projection.payable_settlement_complete = flag;
			snapshot.projection.days["2026-10-24"].settlement = "Settled";
			const day = render("Settled", "green", snapshot);
			expect(isNeutral("2026-10-24")).toBe(true);
			expect(day.text()).toContain("Unavailable");
		}
	);
	it("marks known payments when cash, minimum or receivables are unavailable", () => {
		const snapshot = complete();
		snapshot.projection.payable_settlement_complete = true;
		snapshot.projection.forecast_available = false;
		snapshot.projection.scope_reasons = [
			"bank_unavailable",
			"floor_unconfigured",
			"receivables_unavailable",
		];
		snapshot.unavailable = { Receivable: "forbidden" };
		expect(render("Unpaid", null, snapshot).get(".status").classes()).toContain("unpaid");
	});
	it("distinguishes a zero settled date from a zero no-dues date", async () => {
		render("Settled");
		const snapshot = wrapper.props("snapshot");
		snapshot.projection.days["2026-10-24"].payable_remaining = "0.00";
		snapshot.projection.days["2026-10-25"].settlement = "No dues";
		snapshot.projection.days["2026-10-25"].payable_remaining = "0.00";
		await wrapper.setProps({ snapshot: { ...snapshot } });
		expect(marker("2026-10-24").classes()).toContain("settled");
		expect(marker("2026-10-25").exists()).toBe(false);
	});
	it("marks known historical settlement and removes calendar cash labels and counts", async () => {
		render("Unpaid");
		const snapshot = wrapper.props("snapshot");
		snapshot.projection.days["2026-10-23"] = {
			...snapshot.projection.days["2026-10-24"],
			settlement: "Settled",
			risk: null,
		};
		await wrapper.setProps({ snapshot: { ...snapshot } });
		expect(marker("2026-10-23").classes()).toContain("settled");
		expect(wrapper.text()).not.toMatch(
			/Cash OK|Short|Low|minimum|projected closing cash|invoice rows/
		);
		expect(wrapper.find(".calendar-count").exists()).toBe(false);
	});
	it("uses the same status words and exact accessible amounts in list mode", () => {
		render("Unpaid", "green", { props: { mode: "list" } });
		const row = wrapper.get('[data-list-date="2026-10-24"]');
		expect(row.get(".date-row-date").text()).toContain("Unpaid");
		expect(row.get(".amount-line").attributes("aria-label")).toContain("₹10.00");
		expect(row.text()).not.toMatch(/minimum|Forecast/);
	});
	it("keeps unavailable payables neutral even if an inconsistent flag says complete", () => {
		render("Settled", "green", { unavailable: { Payable: "forbidden" } });
		expect(isNeutral("2026-10-24")).toBe(true);
	});
	it("keeps identical payment status unchanged when cash risk changes", async () => {
		render("Unpaid", "red");
		const snapshot = wrapper.props("snapshot");
		for (const risk of ["orange", "green", null]) {
			snapshot.projection.days["2026-10-24"].risk = risk;
			await wrapper.setProps({ snapshot: { ...snapshot } });
			expect(marker("2026-10-24").classes()).toContain("unpaid");
			expect(marker("2026-10-24").text()).toBe("Unpaid");
		}
	});
	it("never announces a liquidity label for unavailable data", () => {
		const snapshot = complete();
		snapshot.projection.days["2026-10-24"].risk = null;
		wrapper = mountGrid({ props: { snapshot, selected: "2026-10-24", month: "2026-10" } });
		expect(wrapper.get('[data-date="2026-10-24"]').attributes("aria-label")).not.toContain(
			"At or above minimum"
		);
	});
	it("does not certify a missing reconciliation or an out-of-range date", async () => {
		render("Settled");
		const snapshot = wrapper.props("snapshot");
		snapshot.projection.reconciliation.Payable = null;
		await wrapper.setProps({ snapshot: { ...snapshot } });
		expect(isNeutral("2026-10-24")).toBe(true);
		snapshot.projection.reconciliation.Payable = {};
		snapshot.projection.days["2027-10-01"] = {
			settlement: "Settled",
			payable_remaining: "0.00",
		};
		await wrapper.setProps({ snapshot: { ...snapshot }, month: "2027-09" });
		const outside = wrapper.get('[data-date="2027-10-01"]');
		expect(outside.attributes("disabled")).toBeDefined();
		expect(isNeutral("2027-10-01")).toBe(true);
	});
});
