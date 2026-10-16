import { describe, expect, it } from "vitest";
import {
	blankRow,
	buildDraftTable,
	markMissingRows,
	parentDefaults,
	savedRowFor,
} from "./draftTables";

const F = (fieldname, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
const spec = {
	label: "Items",
	child_doctype: "Sales Order Item",
	columns: [F("item_code", "Link"), F("qty", "Float")],
	extra_columns: [F("discount_percentage", "Percent"), F("warehouse", "Link")],
	form_fields: [
		F("item_code", "Link"),
		F("qty", "Float"),
		{ fieldname: "", fieldtype: "Section Break", label: "More" },
		F("discount_percentage", "Percent"),
		F("warehouse", "Link"),
	],
};

describe("buildDraftTable: one child table of the card", () => {
	it("is left out when not required, not proposed and empty", () => {
		expect(
			buildDraftTable({ fieldname: "items", spec, required: false, verb: "create" })
		).toBeNull();
	});

	it("a saved row carries its values outside the grid (the reported bug)", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec,
			saved: [
				{
					name: "r1",
					item_code: "I",
					qty: 2,
					discount_percentage: 5,
					warehouse: "Stores",
				},
			],
			verb: "update",
		});
		expect(t.columns.map((c) => c.fieldname)).toEqual(["item_code", "qty"]);
		expect(t.formFields).toBe(spec.form_fields);
		expect(t.rows[0]).toMatchObject({
			__name: "r1",
			discount_percentage: "5",
			warehouse: "Stores",
		});
		expect(JSON.parse(t.origJson)[0].name).toBe("r1");
	});

	it("a key the agent proposed outside the grid becomes a typed column (#655)", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec,
			proposed: [{ item_code: "I", discount_percentage: 10 }],
			verb: "create",
		});
		const col = t.columns.find((c) => c.fieldname === "discount_percentage");
		expect(col.fieldtype).toBe("Percent");
		expect(t.rows[0].warehouse).toBe(""); // every form field is on the row
	});

	it("an empty required table starts with one blank row with every form field", () => {
		const t = buildDraftTable({ fieldname: "items", spec, required: true, verb: "create" });
		expect(t.rows).toEqual([
			{ item_code: "", qty: "", discount_percentage: "", warehouse: "" },
		]);
		expect(blankRow(t)).toEqual(t.rows[0]);
	});

	it("works without form fields (an older server): rows carry the columns only", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec: { ...spec, form_fields: undefined },
			required: true,
			verb: "create",
		});
		expect(t.formFields).toEqual([]);
		expect(Object.keys(t.rows[0])).toEqual(["item_code", "qty"]);
	});
});

describe("savedRowFor / parentDefaults / markMissingRows", () => {
	const t = buildDraftTable({
		fieldname: "items",
		spec,
		saved: [
			{ name: "r1", item_code: "I", qty: 2 },
			{ name: "r2", item_code: "J", qty: 1 },
		],
		verb: "update",
	});

	it("finds a row's saved row by name, and none for a new row", () => {
		expect(savedRowFor(t, t.rows[1]).item_code).toBe("J");
		expect(savedRowFor(t, { item_code: "new" })).toBeNull();
	});

	it("takes the plain defaults of the record's own fields, never a table", () => {
		expect(
			parentDefaults([
				{ fieldname: "update_stock", fieldtype: "Check", default: "1" },
				{ fieldname: "is_return", fieldtype: "Check", default: "0" },
				{ fieldname: "currency", fieldtype: "Link", default: "INR" },
				{ fieldname: "items", fieldtype: "Table", default: "x" },
				{ fieldname: "title", fieldtype: "Data", default: "" },
			])
		).toEqual({ update_stock: 1, is_return: 0, currency: "INR" }); // a "0" is falsy, as in Desk
	});

	it("counts only the rows the save sent: a blank new row is skipped", () => {
		const create = buildDraftTable({
			fieldname: "items",
			spec,
			proposed: [{ item_code: "" }, { item_code: "A" }, { item_code: "B" }],
			verb: "create",
		});
		create.rows[0] = { item_code: "", qty: "", discount_percentage: "", warehouse: "" };
		markMissingRows({ verb: "create", tables: [create] }, [
			{ parentfield: "items", idx: 2, fieldname: "warehouse" },
		]);
		expect(create.rows[2].__missing).toEqual(["warehouse"]); // the 2nd SENT row is panel row 3
		expect(create.rows[1].__missing).toBeUndefined();
	});

	it("marks and opens the rows a failed save names, and clears old marks", () => {
		const model = { verb: "update", tables: [t] };
		t.rows[0].__missing = ["stale"];
		const n = markMissingRows(model, [
			{ parentfield: "items", idx: 2, fieldname: "warehouse" },
			{ fieldname: "customer" }, // a main field: not a row
			{ parentfield: "items", idx: 9, fieldname: "x" }, // no such row
		]);
		expect(n).toBe(1);
		expect(t.rows[0].__missing).toBeUndefined();
		expect(t.rows[1].__missing).toEqual(["warehouse"]);
		expect(t.rows[1].__open).toBe(true);
	});
});
