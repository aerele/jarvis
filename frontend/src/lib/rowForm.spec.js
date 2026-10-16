import { describe, expect, it } from "vitest";
import { allRowFields as rowFields } from "./draftApply";
import { ruleHolds, rowExtraValues, rowFormLayout, rowStatus, typedRow } from "./rowForm";

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
			{ fieldname: "discount_percentage", label: "discount percentage", value: "10" },
		]);
	});
	it("skips zeros and an unticked Check, and reads a ticked Check as Yes", () => {
		const out = rowExtraValues(
			table,
			row({ discount_percentage: "0", is_free_item: "1" }),
			{}
		);
		expect(out).toEqual([{ fieldname: "is_free_item", label: "is free item", value: "Yes" }]);
	});
	it("still lists a filled field its rule would hide, so nothing saved is out of sight", () => {
		const out = rowExtraValues(table, row({ free_reason: "promo" }), {});
		expect(out.map((x) => x.fieldname)).toContain("free_reason");
	});
});

describe("rules see what Desk's doc and parent hold", () => {
	it("a row carries docstatus, idx, name and __islocal", () => {
		const fresh = typedRow([], {}, { index: 2, docstatus: 1 });
		expect(fresh).toEqual({ docstatus: 1, idx: 3, name: "", __islocal: 1 });
		expect(typedRow([], { __name: "r1" }).__islocal).toBe(0);
	});
	it("keeps a field whose rule reads them, as Desk does", () => {
		const t = {
			columns: [],
			formFields: [
				F("a", "Data", { depends_on: "eval:doc.docstatus==0" }),
				F("b", "Data", { depends_on: "eval:!doc.__islocal" }),
			],
		};
		const shown = (r, p) =>
			rowFormLayout(t, r, p).flatMap((s) => s.columns.flat().map((i) => i.field.fieldname));
		expect(shown({}, {})).toEqual(["a"]); // a new row: __islocal hides b
		expect(shown({ __name: "r1" }, {})).toEqual(["a", "b"]);
		expect(shown({ __name: "r1" }, { docstatus: 1 })).toEqual(["b"]);
	});
	it("a broken mandatory or read-only rule neither demands a value nor locks the field", () => {
		const t = {
			columns: [],
			formFields: [
				F("x", "Data", {
					mandatory_depends_on: "eval:doc.nope.deeper",
					read_only_depends_on: "fn:some_method",
				}),
			],
		};
		const [item] = rowFormLayout(t, { x: "" }, {})[0].columns[0];
		expect(item.reqd).toBe(false);
		expect(item.readOnly).toBe(false);
		expect(ruleHolds("eval:doc.(", {}, {}, false)).toBe(false);
	});
	it("a tab's rule hides the sections inside it", () => {
		const t = {
			columns: [],
			formFields: [
				BREAK("Tab Break", "Stock", { depends_on: "eval:parent.update_stock" }),
				BREAK("Section Break", "Bins"),
				F("bin"),
				BREAK("Tab Break", "Other"),
				F("note"),
			],
		};
		const labels = (p) => rowFormLayout(t, { bin: "", note: "" }, p).map((s) => s.label);
		expect(labels({ update_stock: 0 })).toEqual(["Other"]);
		expect(labels({ update_stock: 1 })).toEqual(["Bins", "Other"]);
	});
});

describe("rowStatus", () => {
	it("counts blank required fields, including ones a failed save named", () => {
		const s = rowStatus(table, row({ qty: "3", warehouse: "" }), { update_stock: 1 }, null);
		expect(s.missing).toBe(1); // warehouse, required because qty > 0
		const named = rowStatus(
			table,
			{ ...row({ qty: "0" }), __missing: ["serial_no"] },
			{ update_stock: 1 },
			null
		);
		expect(named.missing).toBe(1);
	});
	it("on an update, finds what differs from the saved row and counts non-grid changes", () => {
		// A saved row has every field of the form (load_doc returns them all).
		const saved = { ...row({ qty: "2", discount_percentage: "0" }), name: "r1" };
		const r = { ...row(), __name: "r1", qty: "5", discount_percentage: "15" };
		const s = rowStatus(table, r, { update_stock: 1 }, saved);
		expect([...s.changedFields].sort()).toEqual(["discount_percentage", "qty"]);
		expect(s.changed).toBe(1); // qty is a grid column; only the discount counts
	});
});
