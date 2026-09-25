import { describe, it, expect } from "vitest";
import { controlFor, normDateVal, panelField } from "./docFields";

describe("docFields", () => {
	it("maps a fieldtype to its control", () => {
		expect(controlFor("Link", "Supplier Group")).toEqual(["link", "Supplier Group"]);
		expect(controlFor("Select", "Company\n Individual ")).toEqual([
			"select",
			["Company", "Individual"],
		]);
		expect(controlFor("Check")).toEqual(["check", ""]);
		expect(controlFor("Currency")).toEqual(["number", ""]);
		expect(controlFor("Small Text")).toEqual(["text", ""]);
		expect(controlFor("Datetime")).toEqual(["datetime", ""]);
		expect(controlFor("Data")).toEqual(["data", ""]);
	});

	it("normalises dates for the native pickers", () => {
		expect(normDateVal("Date", "2026-07-10 00:00:00")).toBe("2026-07-10");
		expect(normDateVal("Date", "10/07/2026")).toBe("2026-07-10");
		expect(normDateVal("Datetime", "2026-07-10 09:30:00")).toBe("2026-07-10T09:30");
		expect(normDateVal("Datetime", "2026-07-10")).toBe("2026-07-10T00:00");
		expect(normDateVal("Time", "09:30:15")).toBe("09:30");
		expect(normDateVal("Date", "")).toBe("");
		expect(normDateVal("Data", "keep me")).toBe("keep me");
	});

	it("builds a panel field: check as Yes/No, an off-list select value kept", () => {
		const check = panelField(
			{ fieldname: "disabled", label: "Disabled", fieldtype: "Check" },
			1
		);
		expect([check.control, check.value, check.orig]).toEqual(["check", "Yes", "Yes"]);
		const sel = panelField(
			{ fieldname: "t", label: "Type", fieldtype: "Select", options: "A\nB" },
			"Z"
		);
		expect(sel.options).toEqual(["Z", "A", "B"]);
		const date = panelField(
			{ fieldname: "d", label: "D", fieldtype: "Date" },
			"2026-01-02 00:00"
		);
		expect(date.value).toBe("2026-01-02");
		expect(panelField({ fieldname: "x", label: "X", fieldtype: "Data" }, null).value).toBe("");
	});
});
