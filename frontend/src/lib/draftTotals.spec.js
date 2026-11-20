// The edit form's totals row, moved out of ChatView into a pure helper that the desktop
// panel and the phone sheet both call. The figures are what a user reads before saving, so
// each case here pins a number the old inline computed produced.
import { describe, expect, it } from "vitest";
import { draftTotals } from "./draftTotals";

const col = (fieldname) => ({ fieldname, label: fieldname, fieldtype: "Float" });
const table = (columns, rows, formFields = []) => ({
	columns: columns.map(col),
	formFields: formFields.map(col),
	rows,
});

describe("draftTotals", () => {
	it("sums qty, and qty x rate in en-IN format, across every table with a qty column", () => {
		const model = {
			tables: [
				table(
					["qty", "rate"],
					[
						{ qty: "2", rate: "1500" },
						{ qty: "3", rate: "99.5" },
					]
				),
				table(["qty", "rate"], [{ qty: "1000", rate: "100" }]),
				table(["note"], [{ note: "no qty here" }]),
			],
		};
		expect(draftTotals(model)).toEqual({ qty: "1,005", amount: "1,03,298.50", hasAmt: true });
	});

	it("has no amount when no table has a rate column", () => {
		const out = draftTotals({ tables: [table(["qty"], [{ qty: "4" }, { qty: "x" }])] });
		expect(out).toMatchObject({ qty: "4", hasAmt: false });
	});

	it("reads qty and rate from the row form's fields, not only the grid's columns", () => {
		const out = draftTotals({
			tables: [table(["item"], [{ item: "A", qty: "2", rate: "10" }], ["qty", "rate"])],
		});
		expect(out).toEqual({ qty: "2", amount: "20.00", hasAmt: true });
	});

	it("is null when there is no model or nothing to total", () => {
		expect(draftTotals(null)).toBeNull();
		expect(draftTotals({ tables: [] })).toBeNull();
		expect(draftTotals({ tables: [table(["note"], [{ note: "x" }])] })).toBeNull();
	});
});
