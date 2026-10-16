import { describe, expect, it } from "vitest";
import { ruleHolds, rowExtraValues, rowFields, rowFormLayout, typedRow } from "./rowForm";

const F = (fieldname, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label: fieldname.replace(/_/g, " "),
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
const BREAK = (fieldtype, label = "", extra = {}) => ({
	fieldname: "",
	fieldtype,
	label,
	...extra,
});

// A Sales Invoice Item-like child: three grid columns, the rest only in its form.
const table = {
	columns: [F("item_code", "Link"), F("qty", "Float"), F("rate", "Currency")],
	formFields: [
		F("item_code", "Link"),
		F("qty", "Float"),
		F("rate", "Currency"),
		BREAK("Section Break", "Discount"),
		F("discount_percentage", "Percent"),
		BREAK("Column Break"),
		F("is_free_item", "Check"),
		F("free_reason", "Data", { depends_on: "eval:doc.is_free_item" }),
		BREAK("Section Break", "Accounting", { depends_on: "eval:parent.update_stock" }),
		F("warehouse", "Link", { mandatory_depends_on: "eval:doc.qty > 0" }),
		F("serial_no", "Small Text", { read_only_depends_on: "has_serial" }),
	],
};
const row = (over = {}) => ({
	item_code: "ITEM-1",
	qty: "2",
	rate: "100",
	discount_percentage: "",
	is_free_item: "0",
	free_reason: "",
	warehouse: "",
	serial_no: "",
	...over,
});

describe("rowFields", () => {
	it("is the grid columns, then every other form field once, without breaks", () => {
		expect(rowFields(table).map((f) => f.fieldname)).toEqual([
			"item_code",
			"qty",
			"rate",
			"discount_percentage",
			"is_free_item",
			"free_reason",
			"warehouse",
			"serial_no",
		]);
	});
	it("is just the columns for a table without form fields (the PWA, an older server)", () => {
		expect(rowFields({ columns: [F("a")] }).map((f) => f.fieldname)).toEqual(["a"]);
	});
});

describe("typedRow", () => {
	it("types cells the way Desk's doc holds them", () => {
		const doc = typedRow(rowFields(table), row({ is_free_item: "0", qty: "2.5" }));
		expect(doc.is_free_item).toBe(0);
		expect(doc.qty).toBe(2.5);
		expect(typedRow(rowFields(table), row({ is_free_item: "1" })).is_free_item).toBe(1);
		expect(typedRow(rowFields(table), row({ qty: "" })).qty).toBe(0);
	});
});

describe("ruleHolds", () => {
	it("follows Desk: empty yes, eval with doc and parent, bare fieldname = has a value", () => {
		expect(ruleHolds("", {}, {})).toBe(true);
		expect(ruleHolds("eval:doc.qty > 1", { qty: 2 }, {})).toBe(true);
		expect(ruleHolds("eval:doc.qty > 1", { qty: 1 }, {})).toBe(false);
		expect(ruleHolds("eval:parent.update_stock", {}, { update_stock: 1 })).toBe(true);
		expect(ruleHolds("has_serial", { has_serial: 1 }, {})).toBe(true);
		expect(ruleHolds("has_serial", { has_serial: 0 }, {})).toBe(false);
		expect(ruleHolds("has_serial", { has_serial: "" }, {})).toBe(false);
	});
	it("never hides a field because a rule is broken or needs a form script", () => {
		expect(ruleHolds("eval:doc.(", {}, {})).toBe(true);
		expect(ruleHolds("eval:nope.x.y", {}, {})).toBe(true);
		expect(ruleHolds("fn:some_method", {}, {})).toBe(true);
	});
});

describe("rowFormLayout", () => {
	const names = (sections) =>
		sections.map((s) => s.columns.map((c) => c.map((i) => i.field.fieldname)));

	it("lays the form out in sections and columns, in form order", () => {
		const s = rowFormLayout(table, row(), { update_stock: 1 });
		expect(s.map((x) => x.label)).toEqual(["", "Discount", "Accounting"]);
		expect(names(s)).toEqual([
			[["item_code", "qty", "rate"]],
			[["discount_percentage"], ["is_free_item"]],
			[["warehouse", "serial_no"]],
		]);
	});
	it("hides a field whose depends_on is false, and shows it once the row meets it", () => {
		expect(names(rowFormLayout(table, row(), {}))[1][1]).toEqual(["is_free_item"]);
		expect(names(rowFormLayout(table, row({ is_free_item: "1" }), {}))[1][1]).toEqual([
			"is_free_item",
			"free_reason",
		]);
	});
	it("hides a whole section whose depends_on reads the parent", () => {
		expect(rowFormLayout(table, row(), { update_stock: 0 }).map((x) => x.label)).toEqual([
			"",
			"Discount",
		]);
	});
	it("marks a field required or read-only by its rules", () => {
		const items = rowFormLayout(table, row({ qty: "3" }), { update_stock: 1 })[2].columns[0];
		expect(items.find((i) => i.field.fieldname === "warehouse").reqd).toBe(true);
		expect(items.find((i) => i.field.fieldname === "serial_no").readOnly).toBe(false);
		const zero = rowFormLayout(
			table,
			{ ...row({ qty: "0" }), has_serial: "1" },
			{ update_stock: 1 }
		);
		expect(zero[2].columns[0].find((i) => i.field.fieldname === "warehouse").reqd).toBe(false);
	});
	it("is empty for a table without form fields", () => {
		expect(rowFormLayout({ columns: [F("a")] }, { a: "1" }, {})).toEqual([]);
	});
});

describe("rowExtraValues", () => {
	it("lists the filled fields the grid does not show, never a grid column", () => {
		const out = rowExtraValues(table, row({ discount_percentage: "10", rate: "5" }), {});
		expect(out).toEqual([
			{ label: "discount percentage", value: "10" },
			{ label: "is free item", value: "0" },
		]);
	});
	it("leaves out a field its depends_on hides", () => {
		const out = rowExtraValues(table, row({ free_reason: "promo" }), {});
		expect(out.map((x) => x.label)).not.toContain("free reason");
		const shown = rowExtraValues(table, row({ free_reason: "promo", is_free_item: "1" }), {});
		expect(shown.map((x) => x.label)).toContain("free reason");
	});
});
