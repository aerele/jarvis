import { describe, expect, it, vi } from "vitest";
vi.mock("@/utils/datetime", () => ({ siteTimezone: () => "Asia/Kolkata" }));
import { dialogContext, dueLabel, panelSubline, scopeTitle, sideTabs } from "./ledgerCopy";
import { scenario } from "../../../tests/payment-calendar/templateFixtures";

describe("scopeTitle", () => {
	const at = { date: "2026-11-11", anchor: "2026-11-11" };
	it.each([
		["date", "Wednesday, 11 Nov"],
		["immediate", "Immediate dues · Overdue & undated"],
		["now", "Total payable now"],
		["today", "Due today · 11 Nov"],
		["overdue", "Overdue supplier invoices"],
		["undated", "Supplier invoices with no due date"],
		["month", "November 2026 · Invoices"],
	])("%s reads %s", (scope, title) => {
		expect(scopeTitle(scope, at)).toBe(title);
	});
	it("takes Today from the snapshot anchor, not from the selected date", () => {
		expect(scopeTitle("today", { date: "2026-11-23", anchor: "2026-11-11" })).toBe(
			"Due today · 11 Nov"
		);
	});
});

describe("sideTabs", () => {
	const snapshot = scenario("default");
	const tab = (tabs, side) => tabs.find((item) => item.side === side);

	it("reads the date counts and totals from the server day fields", () => {
		const tabs = sideTabs(snapshot, "date", "2026-11-11");
		expect(tab(tabs, "Payable")).toMatchObject({
			label: "Payables",
			count: "3",
			total: "₹42,048.00",
		});
		expect(tab(tabs, "Receivable")).toMatchObject({
			label: "Receivables",
			count: "2",
			total: "₹86,250.00",
		});
	});
	it("reads the month counts and totals from month_totals", () => {
		const tabs = sideTabs(snapshot, "month", "2026-11-11");
		expect(tab(tabs, "Payable")).toMatchObject({
			count: String(snapshot.projection.month_totals.payable_rows),
			total: "₹6,00,898.00",
		});
		expect(tab(tabs, "Receivable")).toMatchObject({ count: "59", total: "₹9,30,600.00" });
	});
	it("is unknown for a date without a server day", () => {
		for (const item of sideTabs(snapshot, "date", "2027-01-01")) {
			expect(item).toMatchObject({ count: "—", total: "Unavailable", known: false });
		}
	});
	it("is unknown for a side the snapshot marks unavailable", () => {
		snapshot.unavailable = { Receivable: "receivables_unavailable" };
		const tabs = sideTabs(snapshot, "date", "2026-11-11");
		expect(tab(tabs, "Receivable")).toMatchObject({ count: "—", total: "Unavailable" });
		expect(tab(tabs, "Payable").known).toBe(true);
	});
	it("is unknown for the month scope when month_totals is missing", () => {
		snapshot.projection.month_totals = null;
		for (const item of sideTabs(snapshot, "month", "2026-11-11")) {
			expect(item.known).toBe(false);
		}
	});
	it("has no tabs for an obligation scope", () => {
		expect(sideTabs(snapshot, "overdue", "2026-11-11")).toEqual([]);
	});
});

describe("snapshot lines", () => {
	const generated = "2026-11-11T05:12:00+00:00";
	it("says the balances are current for the date scope", () => {
		expect(panelSubline("date", generated)).toBe("Current remaining balances");
		expect(panelSubline("immediate", generated)).toBe("Snapshot 11 Nov, 10:42 IST");
	});
	it("gives the dialog its as-of context", () => {
		expect(dialogContext("date", generated)).toBe(
			"Remaining balances as of 11 Nov, 10:42 IST"
		);
		expect(dialogContext("month", generated)).toBe(
			"Remaining balances as of 11 Nov, 10:42 IST"
		);
		expect(dialogContext("overdue", generated)).toBe("Snapshot 11 Nov, 10:42 IST");
	});
});

describe("dueLabel", () => {
	it("is the template day and month inside the snapshot year", () => {
		expect(dueLabel("2026-11-11", "2026-11-11")).toBe("11 Nov");
	});
	it("adds the year for another year, so an old overdue date is clear", () => {
		expect(dueLabel("2025-12-30", "2026-11-11")).toBe("30 Dec 2025");
	});
	it("says No due date for a missing date", () => {
		expect(dueLabel(null, "2026-11-11")).toBe("No due date");
	});
});
