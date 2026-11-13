import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import CashDetails from "./CashDetails.vue";
import { modernStatus } from "../../../tests/payment-calendar/headlineFixtures";
import { complete } from "../../../tests/payment-calendar/fixtures";

function render(snapshot, props = {}) {
	return mount(CashDetails, {
		props: {
			snapshot,
			selected: "2026-11-11",
			month: "2026-11",
			scope: "day",
			timezone: "Asia/Kolkata",
			...props,
		},
	});
}
const text = (wrapper, selector) => wrapper.get(selector).text();

describe("CashDetails: disclosure and snapshot card", () => {
	it("is closed by default, with the template summary and the selected date", () => {
		const wrapper = render(modernStatus());
		const details = wrapper.get("details#financial-details");
		expect(details.attributes("open")).toBeUndefined();
		expect(details.get("summary").text()).toBe(
			"Selected date & cash details" + "Wednesday, 11 Nov"
		);
		expect(text(wrapper, "#details-summary-date")).toBe("Wednesday, 11 Nov");
	});

	it("shows the server's snapshot figures and never changes them with the date", async () => {
		const wrapper = render(modernStatus());
		expect(text(wrapper, ".snapshot-heading h2")).toBe("Current balances");
		expect(text(wrapper, ".snapshot-heading span")).toMatch(/^As of 11 Nov, \d\d:\d\d \w+/);
		expect(text(wrapper, "#book-cash")).toBe("₹2,46,850.00");
		expect(text(wrapper, "#total-payable")).toBe("₹69,548.00 →");
		const rows = wrapper.findAll(".obligation-breakdown button").map((b) => b.text());
		expect(rows).toEqual([
			"Due today · 11 Nov₹42,048.00 →",
			"Overdue₹22,500.00 →",
			"No due date₹5,000.00 →",
		]);
		await wrapper.setProps({ selected: "2026-11-18" });
		expect(text(wrapper, "#total-payable")).toBe("₹69,548.00 →");
	});

	it("emits balances, an obligation scope per button, and balance details", async () => {
		const wrapper = render(modernStatus());
		expect(wrapper.get("#book-cash").attributes("aria-label")).toBe(
			"View bank and cash account balance breakdown"
		);
		await wrapper.get("#book-cash").trigger("click");
		expect(wrapper.emitted("balances")).toHaveLength(1);
		await wrapper.get("#total-payable").trigger("click");
		for (const scope of ["today", "overdue", "undated"])
			await wrapper.get(`[data-obligation="${scope}"]`).trigger("click");
		expect(wrapper.emitted("obligation")).toEqual([
			["now"],
			["today"],
			["overdue"],
			["undated"],
		]);
		await wrapper.get("#balances-button").trigger("click");
		expect(wrapper.emitted("balance-details")).toHaveLength(1);
	});

	it("reads Unavailable for book cash with no accounts", () => {
		const wrapper = render(modernStatus("accounts_none"));
		expect(text(wrapper, "#book-cash")).toBe("Unavailable");
	});
});

describe("CashDetails: day view", () => {
	it("shows the template's figures for 11 Nov", () => {
		const wrapper = render(modernStatus());
		expect(text(wrapper, "#selected-date-heading")).toBe("Wednesday, 11 Nov");
		expect(text(wrapper, "#scope-caption")).toBe("Day view · Scheduled amounts on this date");
		expect(text(wrapper, "#payable-label")).toBe("Due on 11 Nov");
		expect(text(wrapper, "#stat-payable")).toBe("₹42,048.00");
		expect(text(wrapper, "#receivable-label")).toBe("Expected on 11 Nov");
		expect(text(wrapper, "#stat-receivable")).toBe("₹86,250.00");
		expect(wrapper.findAll(".stat-note").map((n) => n.text())).toEqual([
			"Supplier invoices →",
			"Customer invoices · not cash received →",
			"Minimum ₹50,000.00 · 11 Nov",
		]);
		expect(text(wrapper, "#closing-label")).toBe("Projected closing");
		expect(text(wrapper, "#stat-closing")).toBe("₹2,63,552.00");
		expect(text(wrapper, "#closing-note")).toBe("Assumes ₹86,250.00 is collected today.");
		expect(text(wrapper, "#floor-margin")).toBe("₹2,13,552.00 above minimum");
		expect(text(wrapper, "#floor-context")).toBe("Minimum ₹50,000.00 · 11 Nov");
		expect(wrapper.get("#stat-closing").attributes("data-tone")).toBe("positive");
		expect(wrapper.get("#floor-margin").classes()).not.toContain("below");
	});

	it("says collected by the date after the snapshot day", () => {
		const wrapper = render(modernStatus(), { selected: "2026-11-18" });
		expect(text(wrapper, "#closing-note")).toBe(
			"Assumes ₹2,82,950.00 is collected by 18 Nov."
		);
		expect(text(wrapper, "#stat-closing")).toBe("₹2,69,410.00");
		expect(text(wrapper, "#floor-context")).toBe("Minimum ₹50,000.00 · 18 Nov");
	});

	it("colours the closing from the signs of the server's margin and closing", () => {
		const below = render(modernStatus("floor_200000"), { selected: "2026-11-19" });
		expect(text(below, "#floor-margin")).toBe("₹32,826.00 below minimum");
		expect(below.get("#floor-margin").classes()).toContain("below");
		expect(below.get("#stat-closing").attributes("data-tone")).toBe("caution");

		const status = modernStatus();
		status.projection.days["2026-11-11"].projected_closing = "-100.00";
		status.projection.days["2026-11-11"].margin = "-50100.00";
		const negative = render(status);
		expect(negative.get("#stat-closing").attributes("data-tone")).toBe("negative");
		expect(text(negative, "#stat-closing")).toBe("−₹100.00");
	});

	it("never does arithmetic: the closing, margin and note are the server's strings", () => {
		const status = modernStatus();
		const day = status.projection.days["2026-11-11"];
		day.projected_closing = "111.11";
		day.margin = "22.22";
		day.cumulative_receivable = "33.33";
		const wrapper = render(status);
		expect(text(wrapper, "#stat-closing")).toBe("₹111.11");
		expect(text(wrapper, "#floor-margin")).toBe("₹22.22 above minimum");
		expect(text(wrapper, "#closing-note")).toBe("Assumes ₹33.33 is collected today.");
	});

	it("shows the past-date reason before the snapshot day", () => {
		const wrapper = render(modernStatus(), { selected: "2026-11-10" });
		expect(text(wrapper, "#stat-closing")).toBe("No forecast available");
		expect(wrapper.get("#stat-closing").attributes("data-tone")).toBe("muted");
		expect(text(wrapper, "#closing-note")).toBe(
			"Forecast starts on 11 Nov. Past dates show current remaining dues."
		);
		expect(wrapper.find("#floor-margin").text()).toBe("");
		expect(wrapper.find("#floor-context").text()).toBe("");
	});

	it("asks for an account when none is selected", () => {
		const wrapper = render(modernStatus("accounts_none"));
		expect(text(wrapper, "#stat-closing")).toBe("No forecast available");
		expect(text(wrapper, "#closing-note")).toBe(
			"Select a bank or cash account to see the forecast."
		);
		// invoice amounts still work
		expect(text(wrapper, "#stat-payable")).toBe("₹42,048.00");
		expect(wrapper.get("#stat-payable").attributes("disabled")).toBeUndefined();
	});

	it("names the loaded-through date for a date beyond the calendar range", () => {
		const wrapper = render(modernStatus(), { selected: "2027-11-05" });
		expect(text(wrapper, "#closing-note")).toBe(
			"Forecast data is loaded through 31 Oct. Later dates are unavailable."
		);
		expect(text(wrapper, "#stat-payable")).toBe("Unavailable");
		expect(wrapper.get("#stat-payable").attributes("disabled")).toBeDefined();
		expect(wrapper.get("#selected-invoices").attributes("disabled")).toBeDefined();
	});

	it("explains a server reason when accounts exist but the forecast is unavailable", () => {
		const status = modernStatus();
		status.projection.forecast_available = false;
		status.projection.scope_reasons = ["payables_unavailable"];
		for (const day of Object.values(status.projection.days)) day.projected_closing = null;
		const wrapper = render(status);
		expect(text(wrapper, "#closing-note")).toBe(
			"Supplier dues are unavailable for this scope."
		);
	});

	it("emits invoices for the clicked side, or for the scope with the primary button", async () => {
		const wrapper = render(modernStatus());
		await wrapper.get("#stat-payable").trigger("click");
		await wrapper.get("#stat-receivable").trigger("click");
		await wrapper.get("#selected-invoices").trigger("click");
		expect(text(wrapper, "#selected-invoices")).toBe("View invoices");
		expect(wrapper.find("#selected-invoices svg.pcal-arrow").exists()).toBe(true);
		expect(wrapper.emitted("invoices")).toEqual([["Payable"], ["Receivable"], [undefined]]);
	});
});

describe("CashDetails: monthly aggregate", () => {
	const month = () => render(modernStatus(), { scope: "month" });

	it("reads the month totals and the month-end closing", () => {
		const wrapper = month();
		expect(text(wrapper, "#selected-date-heading")).toBe("November 2026");
		expect(text(wrapper, "#scope-caption")).toBe(
			"Monthly aggregate · All dated remaining balances"
		);
		expect(text(wrapper, "#payable-label")).toBe("Due in November");
		expect(text(wrapper, "#stat-payable")).toBe("₹6,00,898.00");
		expect(text(wrapper, "#receivable-label")).toBe("Expected in November");
		expect(text(wrapper, "#stat-receivable")).toBe("₹9,30,600.00");
		expect(text(wrapper, "#closing-label")).toBe("Projected month-end closing");
		expect(text(wrapper, "#stat-closing")).toBe("₹2,62,052.00");
		expect(text(wrapper, "#closing-note")).toBe(
			"Assumes ₹6,21,100.00 is collected by 30 Nov."
		);
		expect(text(wrapper, "#floor-margin")).toBe("₹2,12,052.00 above minimum");
		expect(text(wrapper, "#floor-context")).toBe("Minimum ₹50,000.00 · 30 Nov");
	});

	it("reads Unavailable when the month totals are not complete", () => {
		const status = modernStatus();
		status.projection.month_totals = {
			complete: false,
			net: null,
			payable: null,
			receivable: null,
			payable_rows: null,
			receivable_rows: null,
		};
		const wrapper = render(status, { scope: "month" });
		expect(text(wrapper, "#stat-payable")).toBe("Unavailable");
		expect(wrapper.get("#stat-payable").attributes("disabled")).toBeDefined();
		expect(wrapper.get("#selected-invoices").attributes("disabled")).toBeDefined();
	});

	it("uses the month-end past-date reason for a month before the snapshot day", () => {
		const status = modernStatus();
		status.scope.month = "2026-10";
		status.scope.window_end = "2026-10-31";
		const wrapper = render(status, { scope: "month", month: "2026-10" });
		expect(text(wrapper, "#closing-note")).toBe(
			"Forecast starts on 11 Nov. Past dates show current remaining dues."
		);
	});

	it("switches scope through the segmented control, with aria-pressed", async () => {
		const wrapper = render(modernStatus());
		const [day, monthly] = wrapper.findAll(".segmented button");
		expect(wrapper.get(".segmented").attributes("aria-label")).toBe("Summary scope");
		expect([day.text(), monthly.text()]).toEqual(["Day view", "Monthly aggregate"]);
		expect(day.attributes("aria-pressed")).toBe("true");
		expect(monthly.attributes("aria-pressed")).toBe("false");
		await monthly.trigger("click");
		expect(wrapper.emitted("update:scope")).toEqual([["month"]]);
		await wrapper.setProps({ scope: "month" });
		expect(day.attributes("aria-pressed")).toBe("false");
		expect(monthly.attributes("aria-pressed")).toBe("true");
	});
});

describe("CashDetails: risk alert", () => {
	it("is hidden when no date falls below the minimum", () => {
		expect(render(modernStatus()).find("#risk-alert").exists()).toBe(false);
	});

	it("names the first date below the minimum and jumps to it in day scope", async () => {
		const wrapper = render(modernStatus("floor_200000"), { scope: "month" });
		const alert = wrapper.get("#risk-alert");
		expect(alert.attributes("role")).toBe("status");
		expect(alert.text()).toContain("Cash balance falls below the minimum on 19 Nov.");
		expect(text(wrapper, "#risk-jump")).toBe("View 19 Nov");
		await wrapper.get("#risk-jump").trigger("click");
		expect(wrapper.emitted("update:scope")).toEqual([["day"]]);
		expect(wrapper.emitted("jump")).toEqual([["2026-11-19"]]);
	});

	it("shows only for the displayed month", () => {
		const status = modernStatus("floor_200000");
		expect(render(status, { month: "2026-12" }).find("#risk-alert").exists()).toBe(false);
		expect(render(status, { month: "2026-11" }).find("#risk-alert").exists()).toBe(true);
	});

	// Only per-day server margins decide: the horizon covers 30 days, a month can lie beyond it.
	const withDays = (status, days) => {
		for (const [date, margin] of Object.entries(days))
			status.projection.days[date] = { date, margin };
		return status;
	};

	it("reads the first negative margin of a month beyond the horizon", async () => {
		const status = withDays(modernStatus(), {
			"2026-12-03": "1000.00",
			"2026-12-05": "-0.01",
			"2026-12-09": "-5000.00",
		});
		status.horizon.first_below_floor = null;
		status.scope.month = "2026-12";
		const wrapper = render(status, { month: "2026-12" });
		expect(text(wrapper, "#risk-message")).toBe(
			"Cash balance falls below the minimum on 5 Dec."
		);
		await wrapper.get("#risk-jump").trigger("click");
		expect(wrapper.emitted("jump")).toEqual([["2026-12-05"]]);
	});

	it("does not follow the horizon's date into another month", () => {
		const status = withDays(modernStatus("floor_200000"), { "2026-12-05": "-10.00" });
		status.scope.month = "2026-12";
		expect(text(render(status, { month: "2026-12" }), "#risk-jump")).toBe("View 5 Dec");
	});

	it("ignores days before the snapshot day, and days without a margin", () => {
		const status = withDays(modernStatus(), {
			"2026-11-05": "-100.00",
			"2026-11-20": null,
			"2026-11-21": "0.00",
		});
		status.horizon.first_below_floor = "2026-11-05";
		expect(render(status).find("#risk-alert").exists()).toBe(false);
	});
});

describe("CashDetails: states without a modern snapshot", () => {
	it("does not crash on an old-shape result and never shows ₹0 for missing fields", () => {
		const old = complete();
		delete old.projection.due_now.total;
		const wrapper = render(old, { selected: "2026-10-24", month: "2026-10" });
		expect(text(wrapper, "#stat-closing")).toBe("No forecast available");
		expect(wrapper.find("#risk-alert").exists()).toBe(false);
		expect(text(wrapper, "#total-payable")).toBe("Unavailable →");
		expect(wrapper.get("#total-payable").attributes("disabled")).toBeDefined();
	});

	it("shows loading values while the snapshot has no id", () => {
		const status = modernStatus();
		status.snapshot_id = null;
		status.projection.days = {};
		const wrapper = render(status);
		expect(text(wrapper, "#closing-note")).toBe("Loading values…");
		expect(wrapper.get("#stat-payable").attributes("disabled")).toBeDefined();
		expect(wrapper.get("#selected-invoices").attributes("disabled")).toBeDefined();
	});
});

describe("CashDetails: a snapshot of another month than the displayed one", () => {
	// browseMonth keeps the old snapshot until the new month's one arrives.
	const stale = { month: "2026-12", selected: "2026-12-01" };

	it("shows loading, never the old month's totals, in month scope", async () => {
		const wrapper = render(modernStatus(), { ...stale, scope: "month" });
		expect(text(wrapper, "#selected-date-heading")).toBe("December 2026");
		expect(text(wrapper, "#payable-label")).toBe("Due in December");
		expect(text(wrapper, "#stat-payable")).toBe("");
		expect(text(wrapper, "#stat-receivable")).toBe("");
		expect(text(wrapper, "#stat-closing")).toBe("");
		expect(text(wrapper, "#closing-note")).toBe("Loading values…");
		expect(wrapper.get("#stat-payable").attributes("disabled")).toBeDefined();
		expect(wrapper.get("#selected-invoices").attributes("disabled")).toBeDefined();
		expect(wrapper.find("#risk-alert").exists()).toBe(false);
		await wrapper.setProps({ snapshot: modernStatus("month_dec") });
		expect(text(wrapper, "#stat-payable")).toBe("₹1,17,000.00");
		expect(text(wrapper, "#stat-receivable")).toBe("₹1,12,000.00");
		expect(text(wrapper, "#closing-note")).toMatch(/^Assumes ₹.* is collected by 31 Dec\.$/);
	});

	it("raises no risk alert while loading, whatever the held days say", () => {
		const status = modernStatus();
		status.projection.days["2026-12-05"] = { date: "2026-12-05", margin: "-10.00" };
		const wrapper = render(status, stale);
		expect(wrapper.find("#risk-alert").exists()).toBe(false);
	});

	it("shows loading instead of an unavailable forecast in day scope", async () => {
		const wrapper = render(modernStatus(), stale);
		expect(text(wrapper, "#closing-note")).toBe("Loading values…");
		expect(text(wrapper, "#stat-closing")).toBe("");
		expect(text(wrapper, "#stat-payable")).toBe("");
		await wrapper.setProps({ snapshot: modernStatus("month_dec") });
		expect(text(wrapper, "#stat-closing")).toMatch(/^₹/);
		expect(text(wrapper, "#closing-note")).toMatch(/^Assumes ₹/);
	});
});
