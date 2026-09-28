import { describe, it, expect } from "vitest";
import { controlFor, markMissing, normDateVal, panelField } from "./docFields";

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

// #603: a failed create names the empty required fields; the edit panel marks them.
describe("markMissing", () => {
	const meta = [
		{ fieldname: "account_manager", label: "Account Manager", fieldtype: "Data", reqd: 1 },
		{ fieldname: "gstin", label: "GSTIN", fieldtype: "Data", reqd: 0 },
	];

	it("marks a field already on the panel", () => {
		const model = { fields: [panelField(meta[0], "")] };
		markMissing(model, [{ fieldname: "account_manager" }], meta);
		expect(model.fields[0].serverMissing).toBe(true);
	});

	it("adds a field meta does not mark required, so it can be filled", () => {
		const model = { fields: [] };
		markMissing(model, [{ fieldname: "gstin" }], meta);
		expect(model.fields.map((f) => [f.fieldname, f.serverMissing])).toEqual([["gstin", true]]);
	});

	it("leaves child-row and unknown fields to the message", () => {
		const model = { fields: [] };
		markMissing(
			model,
			[{ fieldname: "qty", parentfield: "items" }, { fieldname: "nope" }],
			meta
		);
		expect(model.fields).toEqual([]);
	});

	it("tolerates an error with no named fields", () => {
		const model = { fields: [] };
		markMissing(model, undefined, meta);
		expect(model.fields).toEqual([]);
	});
});
