import { describe, it, expect } from "vitest";
import { deskNewUrl, errorsByKey, fieldKey, patchOf, stillMissing } from "./heldEdit";

describe("heldEdit", () => {
	it("keys a field or a child cell", () => {
		expect(fieldKey({ fieldname: "tax_id" })).toBe("tax_id");
		expect(fieldKey({ fieldname: "company", parentfield: "companies", idx: 2 })).toBe(
			"companies.2.company"
		);
	});

	it("lists the missing fields of one record that are still empty", () => {
		const needs = [
			{ doc_index: 0, fieldname: "supplier_type" },
			{ doc_index: 0, fieldname: "company", parentfield: "companies", idx: 1 },
			{ doc_index: 1, fieldname: "supplier_type" },
		];
		expect(stillMissing(needs, 0, { supplier_type: "", companies: [{}] })).toHaveLength(2);
		expect(
			stillMissing(needs, 0, { supplier_type: "Company", companies: [{ company: "C" }] })
		).toEqual([]);
		expect(stillMissing(needs, 1, { supplier_type: " " })).toHaveLength(1);
	});

	it("maps server errors by key for one record", () => {
		const errors = [
			{ doc_index: 0, fieldname: "supplier_name", message: "locked" },
			{ doc_index: 1, fieldname: "x", message: "other" },
			{
				doc_index: 0,
				fieldname: "company",
				parentfield: "companies",
				idx: 1,
				message: "bad",
			},
		];
		expect(errorsByKey(errors, 0)).toEqual({
			supplier_name: "locked",
			"companies.1.company": "bad",
		});
	});

	it("sends only what changed", () => {
		const original = {
			supplier_name: "Acme",
			supplier_type: "",
			companies: [{}, { company: "A" }],
		};
		const current = {
			supplier_name: "Acme",
			supplier_type: "Company",
			companies: [{ company: "B" }, { company: "A" }],
		};
		expect(patchOf(original, current)).toEqual({
			supplier_type: "Company",
			companies: [{ company: "B" }, {}],
		});
		expect(patchOf(original, original)).toEqual({});
	});

	it("builds the Desk new-form link from non-secret scalars", () => {
		const url = deskNewUrl(
			"Purchase Invoice",
			{ supplier: "A & B", api_key: "s3cret", items: [{}], note: "", qty: 2 },
			["api_key"]
		);
		expect(url).toBe("/app/purchase-invoice/new?supplier=A+%26+B&qty=2");
		expect(deskNewUrl("Supplier", {})).toBe("/app/supplier/new");
	});
});
