import { describe, expect, it, vi } from "vitest";
vi.mock("@/utils/datetime", () => ({ siteTimezone: () => "Asia/Kolkata" }));
import { mount } from "@vue/test-utils";
import DayLedger from "./DayLedger.vue";
import { scenario } from "../../../tests/payment-calendar/templateFixtures";

const row = (over = {}) => ({
	id: "PINV-1-1",
	party_name: "Cedar Trading Co.",
	party: "SUP-1",
	voucher_no: "PINV-2026-0142",
	voucher_type: "Purchase Invoice",
	due_date: "2026-11-11",
	original: "20000.00",
	remaining: "18500.00",
	settlement: "Partially paid",
	...over,
});
const area = (over = {}) => ({
	rows: [row()],
	total: 3,
	total_remaining: "42048.00",
	has_more: false,
	loading: false,
	error: null,
	unavailable: false,
	...over,
});
function render(props = {}) {
	return mount(DayLedger, {
		props: {
			snapshot: scenario("default"),
			scope: "date",
			side: "Payable",
			date: "2026-11-11",
			panel: area(),
			...props,
		},
	});
}

describe("DayLedger heading", () => {
	it("is a polite live region with the selected-date heading", () => {
		const wrapper = render();
		const panel = wrapper.get("#day-panel");
		expect(panel.attributes("aria-live")).toBe("polite");
		expect(panel.text()).toContain("SELECTED DATE");
		const heading = wrapper.get("#ledger-heading");
		expect(heading.element.tagName).toBe("H2");
		expect(heading.attributes("tabindex")).toBe("-1");
		expect(heading.text()).toBe("Wednesday, 11 Nov");
		expect(panel.text()).toContain("Current remaining balances");
		expect(wrapper.find("button.ledger-back").exists()).toBe(false);
	});
	it("names an obligation scope and offers Back to date", async () => {
		const wrapper = render({ scope: "immediate", panel: area() });
		expect(wrapper.text()).toContain("CURRENT OBLIGATIONS");
		expect(wrapper.get("#ledger-heading").text()).toBe("Immediate dues · Overdue & undated");
		expect(wrapper.text()).toContain("Snapshot 11 Nov, 10:42 IST");
		await wrapper.get(".ledger-back").trigger("click");
		expect(wrapper.get(".ledger-back").text()).toBe("Back to date");
		expect(wrapper.get(".ledger-back").attributes("id")).toBe("back-to-date");
		expect(wrapper.emitted("back")).toHaveLength(1);
	});
	it("has no side tabs for an obligation scope", () => {
		expect(render({ scope: "overdue" }).find(".ledger-tabs").exists()).toBe(false);
	});
});

describe("DayLedger tabs", () => {
	it("shows the server counts and totals and marks the active side", () => {
		const wrapper = render();
		const [payable, receivable] = wrapper.findAll("[data-panel-side]");
		expect(payable.text()).toContain("Payables");
		expect(payable.text()).toContain("3");
		expect(payable.text()).toContain("₹42,048.00");
		expect(payable.attributes("aria-pressed")).toBe("true");
		expect(receivable.text()).toContain("₹86,250.00");
		expect(receivable.attributes("aria-pressed")).toBe("false");
		expect(wrapper.get(".ledger-tabs").attributes("aria-label")).toBe("Invoice side");
	});
	it("emits the side that was clicked", async () => {
		const wrapper = render();
		await wrapper.get('[data-panel-side="Receivable"]').trigger("click");
		expect(wrapper.emitted("side")).toEqual([["Receivable"]]);
	});
	it("uses month_totals for the month scope", () => {
		const wrapper = render({ scope: "month" });
		expect(wrapper.get("#ledger-heading").text()).toBe("November 2026 · Invoices");
		expect(wrapper.get('[data-panel-side="Payable"]').text()).toContain(
			String(scenario("default").projection.month_totals.payable_rows)
		);
		expect(wrapper.get('[data-panel-side="Payable"]').text()).toContain("₹6,00,898.00");
	});
	it("shows a dash and Unavailable for a date the server has no figures for", () => {
		const wrapper = render({ date: "2027-01-01", panel: area({ rows: [], total: null }) });
		const tab = wrapper.get('[data-panel-side="Payable"]');
		expect(tab.text()).toContain("—");
		expect(tab.text()).toContain("Unavailable");
		expect(wrapper.text()).not.toContain("₹0.00");
	});
});

describe("DayLedger rows", () => {
	it("lists party, plain-text number, due date, remaining and status", () => {
		const wrapper = render({
			panel: area({
				rows: [
					row(),
					row({
						id: "2",
						voucher_no: "PINV-2026-0150",
						party_name: "Zeta",
						due_date: null,
						remaining: "0.00",
						settlement: "Settled",
					}),
				],
			}),
		});
		const [first, second] = wrapper.findAll(".ledger-row");
		expect(first.get(".invoice-party").text()).toBe("Cedar Trading Co.");
		expect(first.get(".invoice-reference").text()).toBe("PINV-2026-0142");
		expect(first.text()).toContain("Due 11 Nov");
		expect(first.get(".invoice-balance strong").text()).toBe("₹18,500.00");
		expect(first.get(".invoice-balance small").text()).toBe("Partially paid");
		expect(second.text()).toContain("No due date");
		expect(second.get(".invoice-balance strong").text()).toBe("₹0.00");
		expect(second.get(".invoice-balance small").text()).toBe("Settled");
	});
	it("renders an invoice number as plain text, never as a link", () => {
		const wrapper = render({ panel: area({ rows: [row({ source_link: "/app/x" })] }) });
		expect(wrapper.find("a").exists()).toBe(false);
		expect(wrapper.find(".invoice-reference").element.tagName).toBe("SPAN");
	});
	it("escapes party names and falls back to the party id", () => {
		const hostile = '<img src=x onerror="alert(1)">';
		const wrapper = render({
			panel: area({
				rows: [
					row({ party_name: hostile }),
					row({ id: "2", party_name: "", party: "SUP-9" }),
				],
			}),
		});
		expect(wrapper.find("img").exists()).toBe(false);
		expect(wrapper.findAll(".invoice-party")[0].text()).toBe(hostile);
		expect(wrapper.findAll(".invoice-party")[1].text()).toBe("SUP-9");
	});
});

describe("DayLedger states", () => {
	it("says which side is empty", () => {
		const empty = area({ rows: [], total: 0 });
		expect(render({ panel: empty }).text()).toContain(
			"No supplier invoices for this selection."
		);
		expect(render({ side: "Receivable", panel: empty }).text()).toContain(
			"No customer invoices for this selection."
		);
	});
	it("says the data is unavailable, never an empty list", () => {
		const wrapper = render({ panel: area({ rows: [], total: null, unavailable: true }) });
		expect(wrapper.text()).toContain("Invoice data is unavailable for this month.");
		expect(wrapper.text()).not.toContain("No supplier invoices");
		expect(wrapper.get("#panel-view-all").attributes("disabled")).toBeDefined();
	});
	it("shows a loading line and no stale rows", () => {
		const wrapper = render({ panel: area({ rows: [], total: null, loading: true }) });
		expect(wrapper.get("[role=status]").text()).toBe("Loading invoices…");
		expect(wrapper.find(".ledger-row").exists()).toBe(false);
		expect(wrapper.text()).not.toContain("No supplier invoices");
	});
	it("shows the error with Retry and keeps the date", async () => {
		const wrapper = render({
			panel: area({
				rows: [],
				total: null,
				error: { code: "network_error", state: "failed", retryable: true },
			}),
		});
		expect(wrapper.get("[role=alert]").text()).toContain("The server could not be reached");
		expect(wrapper.get("#ledger-heading").text()).toBe("Wednesday, 11 Nov");
		await wrapper.get(".ledger-retry").trigger("click");
		expect(wrapper.emitted("retry")).toHaveLength(1);
	});
});

describe("DayLedger View all", () => {
	it("counts the records from the server total and opens the dialog", async () => {
		const wrapper = render();
		const button = wrapper.get("#panel-view-all");
		expect(button.text()).toBe("View all 3 records");
		expect(button.attributes("disabled")).toBeUndefined();
		await button.trigger("click");
		expect(wrapper.emitted("view-all")).toHaveLength(1);
	});
	it("ends with the template's arrow icon, not a text glyph", () => {
		const wrapper = render();
		const icon = wrapper.get("#panel-view-all svg");
		expect(icon.attributes("viewBox")).toBe("0 0 24 24");
		expect(icon.attributes("aria-hidden")).toBe("true");
		expect(icon.get("path").attributes("d")).toBe("M5 12h14m-5-5 5 5-5 5");
		expect(wrapper.get("#panel-view-all").text()).not.toContain("→");
	});
	it("falls back to the tab count while the rows load", () => {
		const wrapper = render({ panel: area({ rows: [], total: null, loading: true }) });
		expect(wrapper.get("#panel-view-all").text()).toBe("View all 3 records");
	});
	it("is disabled and has no number when the count is unknown", () => {
		const wrapper = render({
			date: "2027-01-01",
			panel: area({ rows: [], total: null, unavailable: true }),
		});
		expect(wrapper.get("#panel-view-all").text()).toBe("View all records");
		expect(wrapper.get("#panel-view-all").attributes("disabled")).toBeDefined();
	});
});
