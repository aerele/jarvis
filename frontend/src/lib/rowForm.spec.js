import { afterEach, describe, expect, it, vi } from "vitest";
import { allRowFields as rowFields } from "./draftApply";
import { buildDraftTable } from "./draftTables";
import {
	changeDisplay,
	ruleHolds,
	rowChangedSet,
	rowExtraValues,
	rowFormLayout,
	rowStatus,
	typedRow,
} from "./rowForm";

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

describe("ruleHolds: a broken rule is compiled and warned once, and says where it is", () => {
	afterEach(() => vi.restoreAllMocks());
	it("a rule that cannot compile is not compiled again on every evaluation", () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		const compile = vi.spyOn(globalThis, "Function");
		const rule = "eval:doc.((compile-once";
		for (let i = 0; i < 5; i++) expect(ruleHolds(rule, {}, {})).toBe(true);
		for (let i = 0; i < 5; i++) expect(ruleHolds(rule, {}, {}, false)).toBe(false);
		expect(compile).toHaveBeenCalledTimes(1);
		expect(warn).toHaveBeenCalledTimes(1);
	});
	it("a form-script fn: rule warns once, answers the fallback, and is named", () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		for (let i = 0; i < 3; i++)
			expect(ruleHolds("fn:warn_once_method", {}, {}, true, "Item.x depends_on")).toBe(true);
		expect(ruleHolds("fn:warn_once_method", {}, {}, false, "Item.x depends_on")).toBe(false);
		expect(warn).toHaveBeenCalledTimes(1);
		expect(String(warn.mock.calls[0].join(" "))).toContain("fn:warn_once_method");
		expect(String(warn.mock.calls[0].join(" "))).toContain("Item.x depends_on");
	});
	it("the warning names the label it was given, and runs without one", () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		ruleHolds("eval:doc.(label-a", {}, {}, true, "Sales Item.rate read_only");
		expect(warn.mock.calls[0].join(" ")).toContain("Sales Item.rate read_only");
		ruleHolds("eval:doc.(label-b", {}, {});
		expect(warn).toHaveBeenCalledTimes(2);
	});
	it("the layout names the child doctype, the field and the rule kind", () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		const t = {
			child: "Wired Item",
			columns: [],
			formFields: [
				F("a", "Data", { depends_on: "eval:doc.(wired-dep" }),
				F("b", "Data", { mandatory_depends_on: "eval:doc.(wired-mand" }),
				F("c", "Data", { read_only_depends_on: "eval:doc.(wired-ro" }),
			],
		};
		rowFormLayout(t, { a: "1", b: "1", c: "1" }, {});
		const said = warn.mock.calls.map((c) => c.join(" "));
		expect(said.find((m) => m.includes("wired-dep"))).toContain("Wired Item.a depends_on");
		expect(said.find((m) => m.includes("wired-mand"))).toContain("Wired Item.b mandatory");
		expect(said.find((m) => m.includes("wired-ro"))).toContain("Wired Item.c read_only");
	});
	it("a table without a child name is named by its fieldname", () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		const t = {
			fieldname: "items",
			columns: [],
			formFields: [F("a", "Data", { depends_on: "eval:doc.(wired-fieldname" })],
		};
		rowFormLayout(t, { a: "1" }, {});
		expect(warn.mock.calls[0].join(" ")).toContain("items.a depends_on");
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
	it("a rule-hidden Check proposed as false is not 'filled', so it stays hidden", () => {
		const t = {
			columns: [],
			formFields: [F("flag", "Check", { depends_on: "eval:0" }), F("note", "Data")],
		};
		const names = (v) =>
			rowFormLayout(t, { flag: v, note: "" }, {}).flatMap((sec) =>
				sec.columns.flat().map((i) => i.field.fieldname)
			);
		for (const off of ["false", "No", false]) expect(names(off)).toEqual(["note"]);
		expect(names("Yes")).toEqual(["flag", "note"]);
	});
	it("a field a failed save named is laid out even when a rule hides it", () => {
		const names = (r, parent) =>
			rowFormLayout(table, r, parent).flatMap((sec) =>
				sec.columns.flat().map((i) => i.field.fieldname)
			);
		// free_reason is hidden by depends_on, serial_no sits in a section a rule hides
		const hidden = row();
		expect(names(hidden, { update_stock: 0 })).not.toContain("free_reason");
		expect(names(hidden, { update_stock: 0 })).not.toContain("warehouse");
		const named = { ...hidden, __missing: ["free_reason", "warehouse"] };
		const got = names(named, { update_stock: 0 });
		expect(got).toContain("free_reason");
		expect(got).toContain("warehouse"); // its section is hidden too
		// only the named ones come in, not their siblings
		expect(got).not.toContain("serial_no");
		// and with a saved row the same holds
		const saved = { ...row(), name: "r1" };
		const withSaved = rowFormLayout(
			table,
			{ ...named, __name: "r1" },
			{ update_stock: 0 },
			{
				savedRow: saved,
			}
		).flatMap((sec) => sec.columns.flat().map((i) => i.field.fieldname));
		expect(withSaved).toContain("free_reason");
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
	it("reads a Check value as Desk does: false, No, 0 and unticked list nowhere", () => {
		const listed = (v) =>
			rowExtraValues(table, row({ is_free_item: v }), {}).some(
				(x) => x.fieldname === "is_free_item"
			);
		for (const off of [false, "false", "False", "No", "no", 0, "0", ""])
			expect(listed(off)).toBe(false);
		for (const on of [true, "true", "Yes", "yes", 1, "1"]) expect(listed(on)).toBe(true);
		const out = rowExtraValues(table, row({ is_free_item: "true" }), {});
		expect(out.find((x) => x.fieldname === "is_free_item").value).toBe("Yes");
	});
	it("still lists a filled field its rule would hide, so nothing saved is out of sight", () => {
		const out = rowExtraValues(table, row({ free_reason: "promo" }), {});
		expect(out.map((x) => x.fieldname)).toContain("free_reason");
	});
});

describe("rules see what Desk's doc and parent hold", () => {
	it("a row carries docstatus, idx, name and __islocal", () => {
		const fresh = typedRow([], {}, { index: 2, docstatus: 1 });
		expect(fresh).toEqual({
			docstatus: 1,
			idx: 3,
			name: "",
			__islocal: 1,
			parenttype: "",
			parentfield: "",
			parent: "",
		});
		expect(typedRow([], { __name: "r1" }).__islocal).toBe(0);
	});
	it("a row carries parenttype, parentfield and parent, as Desk's child doc does", () => {
		const doc = typedRow(
			[],
			{},
			{ parenttype: "Sales Order", parentfield: "items", parent: "SO-1" }
		);
		expect(doc).toMatchObject({
			parenttype: "Sales Order",
			parentfield: "items",
			parent: "SO-1",
		});
		// rowFormLayout takes them from the record and the table
		const t = {
			fieldname: "items",
			columns: [],
			formFields: [
				F("a", "Data", {
					depends_on: 'eval:doc.parenttype=="Sales Order" && doc.parentfield=="items"',
				}),
				F("b", "Data", { depends_on: 'eval:doc.parent=="SO-1"' }),
				F("c", "Data", { depends_on: 'eval:doc.parent==""' }),
			],
		};
		const shown = (p) =>
			rowFormLayout(t, {}, p).flatMap((sec) =>
				sec.columns.flat().map((i) => i.field.fieldname)
			);
		expect(shown({ doctype: "Sales Order", name: "SO-1" })).toEqual(["a", "b"]);
		expect(shown({ doctype: "Sales Order" })).toEqual(["a", "c"]); // a new record: no name
		expect(shown({})).toEqual(["c"]);
		// the caller's ctx wins
		expect(
			rowFormLayout(t, {}, {}, { parenttype: "Sales Order", parentfield: "items" }).length
		).toBe(1);
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

describe("rowFormLayout gives each section a stable id", () => {
	const t = {
		columns: [],
		formFields: [
			F("lead"),
			BREAK("Section Break", "Stock", {
				fieldname: "stock_sec",
				depends_on: "eval:parent.update_stock",
			}),
			F("bin"),
			BREAK("Section Break", "Other"), // a break with no fieldname
			F("note"),
		],
	};
	const ids = (p) => rowFormLayout(t, { lead: "x", bin: "", note: "x" }, p).map((s) => s.id);
	it("is the section break's fieldname, else sec-<first fieldname>", () => {
		expect(ids({ update_stock: 1 })).toEqual(["sec-lead", "stock_sec", "sec-note"]);
	});
	it("does not move when a section above it is hidden", () => {
		expect(ids({ update_stock: 0 })).toEqual(["sec-lead", "sec-note"]);
	});
	it("stays the same when the first field of the section is rule-hidden for now", () => {
		const u = {
			columns: [],
			formFields: [
				BREAK("Section Break", "More"),
				F("a", "Data", { depends_on: "eval:doc.b" }),
				F("b"),
			],
		};
		expect(rowFormLayout(u, { a: "", b: "" }, {})[0].id).toBe("sec-a");
		expect(rowFormLayout(u, { a: "", b: "1" }, {})[0].id).toBe("sec-a");
	});
});

describe("rowStatus", () => {
	it("counts only fields a failed save named, while they are still blank", () => {
		// Required and blank (warehouse, because qty > 0) but no save has failed on it:
		// ERPNext may fill it on save, so the row is not flagged.
		const s = rowStatus(table, row({ qty: "3", warehouse: "" }), { update_stock: 1 }, null);
		expect(s.missing).toBe(0);
		const named = rowStatus(
			table,
			{ ...row({ qty: "0" }), __missing: ["serial_no"] },
			{ update_stock: 1 },
			null
		);
		expect(named.missing).toBe(1);
		// Once the user fills it, it is no longer missing.
		const filled = rowStatus(
			table,
			{ ...row({ qty: "0", serial_no: "SN-1" }), __missing: ["serial_no"] },
			{ update_stock: 1 },
			null
		);
		expect(filled.missing).toBe(0);
	});
	it("counts a name a failed save asked for that the form has no field for", () => {
		const r = (missing) => ({ ...row({ qty: "0" }), __missing: missing });
		const status = (missing) => rowStatus(table, r(missing), { update_stock: 1 }, null);
		expect(status(["tax_template"]).missing).toBe(1); // no such field in the form
		expect(status(["tax_template", "tax_template"]).missing).toBe(1); // once
		// a form field is counted by the layout, not twice
		expect(status(["serial_no", "tax_template"]).missing).toBe(2);
		expect(status([]).missing).toBe(0);
	});
	it("a name with no field in the form counts only while the row still has no value for it", () => {
		const r = (extra) => ({ ...row({ qty: "0" }), __missing: ["tax_template"], ...extra });
		const status = (extra) => rowStatus(table, r(extra), { update_stock: 1 }, null);
		expect(status({}).missing).toBe(1);
		expect(status({ tax_template: "  " }).missing).toBe(1); // blank is blank
		expect(status({ tax_template: "GST 18" }).missing).toBe(0);
		expect(status({ tax_template: 0 }).missing).toBe(0);
	});
	it("a named field the form lays out read-only (static or by rule) counts while blank", () => {
		const t = {
			columns: [],
			formFields: [
				F("locked", "Data", { read_only: 1 }),
				F("ruled", "Data", { read_only_depends_on: "eval:1" }),
				F("plain", "Data"),
			],
		};
		const status = (row) =>
			rowStatus(t, { locked: "", ruled: "", plain: "", ...row }, {}, null);
		expect(status({ __missing: ["locked", "ruled"] }).missing).toBe(2);
		expect(status({ __missing: ["locked", "ruled", "plain"] }).missing).toBe(3);
		// once a value is there (a refetch filled it) it no longer counts
		expect(status({ __missing: ["locked", "ruled"], locked: "x", ruled: "y" }).missing).toBe(
			0
		);
		// a read-only field nobody named never counts
		expect(status({}).missing).toBe(0);
	});
	it("a blank Read Only field is no demand on the user unless a failed save named it", () => {
		const t = { columns: [], formFields: [F("kind", "Read Only", { reqd: 1 })] };
		expect(rowStatus(t, { kind: "" }, {}, null).missing).toBe(0);
		expect(rowStatus(t, { kind: "", __missing: ["kind"] }, {}, null).missing).toBe(1);
	});
	it("on an update, finds what differs from the saved row and counts non-grid changes", () => {
		// A saved row has every field of the form (load_doc returns them all).
		const saved = { ...row({ qty: "2", discount_percentage: "0" }), name: "r1" };
		const r = { ...row(), __name: "r1", qty: "5", discount_percentage: "15" };
		const s = rowStatus(table, r, { update_stock: 1 }, saved);
		expect([...s.changedFields].sort()).toEqual(["discount_percentage", "qty"]);
		expect(s.changed).toBe(1); // qty is a grid column; only the discount counts
	});
	it("compares a Check as Yes/No on both sides: a proposed Yes over a saved 1 is unchanged", () => {
		const saved = { ...row({ is_free_item: "1" }), name: "r1" };
		const status = (v, sv = "1") =>
			rowStatus(
				table,
				{ ...row({ is_free_item: v }), __name: "r1" },
				{},
				{ ...saved, is_free_item: sv }
			);
		for (const same of ["Yes", "yes", "true", true, 1, "1"])
			expect([...status(same).changedFields]).toEqual([]);
		for (const off of ["No", "false", false, 0, "0"])
			expect([...status(off, "0").changedFields]).toEqual([]);
		// a real change still shows, both ways
		expect([...status("No").changedFields]).toEqual(["is_free_item"]);
		expect([...status("Yes", "0").changedFields]).toEqual(["is_free_item"]);
		expect(status("false").changed).toBe(1);
	});
	it("a blank required Check a failed save named is missing until it has a value", () => {
		const t = { columns: [], formFields: [F("flag", "Check", { reqd: 1 })] };
		expect(rowStatus(t, { flag: "" }, {}, null).missing).toBe(0);
		expect(rowStatus(t, { flag: "", __missing: ["flag"] }, {}, null).missing).toBe(1);
		expect(rowStatus(t, { flag: "No", __missing: ["flag"] }, {}, null).missing).toBe(0);
	});
});

// Purchase Invoice Item-like: is_fixed_asset is hidden (so not a form field) but the
// rules of Asset Category and Cost Center read it, and Desk fetches it from the item.
describe("rules read the hidden fields they reference", () => {
	const ruleField = {
		fieldname: "is_fixed_asset",
		fieldtype: "Check",
		default: "0",
		fetch_from: "item_code.is_fixed_asset",
	};
	const spec = {
		label: "Items",
		child_doctype: "Purchase Invoice Item",
		columns: [F("item_code", "Link"), F("qty", "Float")],
		extra_columns: [],
		form_fields: [
			F("item_code", "Link"),
			F("qty", "Float"),
			F("asset_category", "Link", { depends_on: "is_fixed_asset" }),
			F("cost_center", "Link", { depends_on: "eval:!doc.is_fixed_asset" }),
		],
		rule_fields: [ruleField],
	};
	const savedRow = (over = {}) => ({
		name: "r1",
		item_code: "ASSET-ITEM",
		qty: 1,
		asset_category: "",
		cost_center: "",
		is_fixed_asset: 1,
		...over,
	});
	const build = (saved, proposed) =>
		buildDraftTable({
			fieldname: "items",
			spec,
			proposed,
			saved: [saved],
			verb: "update",
		});
	const names = (t, row = t.rows[0]) =>
		rowFormLayout(t, row, { doctype: "Purchase Invoice", name: "PINV-1" }).flatMap((sec) =>
			sec.columns.flat().map((i) => i.field.fieldname)
		);

	it("a saved fixed asset shows Asset Category and hides Cost Center", () => {
		expect(names(build(savedRow()))).toEqual(["item_code", "qty", "asset_category"]);
	});
	it("a saved non-asset is the reverse", () => {
		expect(names(build(savedRow({ is_fixed_asset: 0 })))).toEqual([
			"item_code",
			"qty",
			"cost_center",
		]);
	});
	it("the rule field is typed like any field: a Check is 0/1 on doc", () => {
		const t = build(savedRow({ is_fixed_asset: "1" }));
		const doc = typedRow(rowFields(t), t.rows[0], { ruleFields: t.ruleFields });
		expect(doc.is_fixed_asset).toBe(1);
		t.rows[0].__rule.is_fixed_asset = "No";
		expect(
			typedRow(rowFields(t), t.rows[0], { ruleFields: t.ruleFields }).is_fixed_asset
		).toBe(0);
	});
	it("another item makes the rule read the default, not the stale saved value", () => {
		// the agent changes item_code: Desk would fetch is_fixed_asset from the new item
		const t = build(savedRow(), [{ name: "r1", item_code: "PLAIN-ITEM" }]);
		expect(t.rows[0].__rule.is_fixed_asset).toBe("0");
		expect(names(t)).toEqual(["item_code", "qty", "cost_center"]);
		// the user changes it in the grid afterwards
		const u = build(savedRow());
		expect(names(u)).toContain("asset_category");
		u.rows[0].item_code = "PLAIN-ITEM";
		expect(names(u)).toEqual(["item_code", "qty", "cost_center"]);
		// ... and back to the saved item
		u.rows[0].item_code = "ASSET-ITEM";
		expect(names(u)).toContain("asset_category");
	});
	it("a new row reads the default (no saved row, nothing fetched)", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec,
			required: true,
			proposed: [{ item_code: "ASSET-ITEM" }],
			verb: "create",
		});
		expect(t.rows[0].__rule).toEqual({ is_fixed_asset: "0" });
		expect(names(t)).toEqual(["item_code", "qty", "cost_center"]);
	});
	it("the rule field is not a form field, so it is never laid out, listed or counted", () => {
		const t = build(savedRow());
		expect(rowFields(t).map((f) => f.fieldname)).not.toContain("is_fixed_asset");
		expect(rowExtraValues(t, t.rows[0], {}).map((x) => x.fieldname)).not.toContain(
			"is_fixed_asset"
		);
		expect(rowStatus(t, t.rows[0], {}, savedRow()).changed).toBe(0);
	});
});

describe("End to end, from a saved row shaped as load_doc returns it", () => {
	// load_doc's row: name, then every grid column, form field AND rule field as plain
	// keys (a Check is 1/0, a blank link is null). Nothing is injected anywhere else.
	const spec = {
		label: "Items",
		child_doctype: "Purchase Invoice Item",
		columns: [F("item_code", "Link"), F("qty", "Float")],
		extra_columns: [],
		form_fields: [
			F("item_code", "Link"),
			F("qty", "Float"),
			F("asset_category", "Link", { depends_on: "is_fixed_asset" }),
			F("cost_center", "Link", { depends_on: "eval:!doc.is_fixed_asset" }),
		],
		rule_fields: [
			{
				fieldname: "is_fixed_asset",
				fieldtype: "Check",
				default: "0",
				fetch_from: "item_code.is_fixed_asset",
			},
		],
	};
	const loaded = (flag) => [
		{
			name: "r1",
			item_code: "ASSET-ITEM",
			qty: 1,
			asset_category: null,
			cost_center: null,
			is_fixed_asset: flag,
		},
	];
	const shown = (t) =>
		rowFormLayout(t, t.rows[0], { doctype: "Purchase Invoice", name: "PINV-1" }).flatMap((s) =>
			s.columns.flat().map((i) => i.field.fieldname)
		);
	// the agent proposes only a quantity change on the saved row
	const build = (flag) =>
		buildDraftTable({
			fieldname: "items",
			spec,
			proposed: [{ name: "r1", qty: 3 }],
			saved: loaded(flag),
			verb: "update",
		});

	it("a saved fixed asset (is_fixed_asset 1) shows Asset Category and hides Cost Center", () => {
		const t = build(1);
		expect(t.rows[0].__rule).toEqual({ is_fixed_asset: 1 });
		expect(shown(t)).toEqual(["item_code", "qty", "asset_category"]);
	});
	it("a saved non-asset (0) is the reverse", () => {
		expect(shown(build(0))).toEqual(["item_code", "qty", "cost_center"]);
	});
	it("the table the card is built from has no rule value without the saved key", () => {
		// what the card saw before load_doc returned rule values: the default wins
		const [row] = loaded(1);
		delete row.is_fixed_asset;
		const t = buildDraftTable({ fieldname: "items", spec, saved: [row], verb: "update" });
		expect(shown(t)).toEqual(["item_code", "qty", "cost_center"]);
	});
});

describe("Desk's rule helpers", () => {
	// Stock Entry Detail, from erpnext
	const purposes = '["Subcontracting Delivery", "Return Raw Material to Customer"]';
	const sWarehouse = F("s_warehouse", "Link", {
		read_only_depends_on: `eval:in_list(${purposes}, parent.purpose)`,
	});
	const t = { columns: [], formFields: [sWarehouse] };
	const state = (purpose) => rowFormLayout(t, { s_warehouse: "" }, { purpose })[0].columns[0][0];

	it("in_list: a rule that lists values locks the field only for those", () => {
		expect(state("Subcontracting Delivery").readOnly).toBe(true);
		expect(state("Return Raw Material to Customer").readOnly).toBe(true);
		expect(state("Material Transfer").readOnly).toBe(false);
		expect(state(undefined).readOnly).toBe(false);
	});
	it("in_list in depends_on (Putaway Rule)", () => {
		const putaway = {
			columns: [],
			formFields: [
				F("putaway_rule", "Link", {
					depends_on:
						'eval:in_list(["Material Transfer", "Material Receipt"], parent.purpose)',
				}),
			],
		};
		const shown = (purpose) => rowFormLayout(putaway, {}, { purpose }).length;
		expect(shown("Material Receipt")).toBe(1);
		expect(shown("Manufacture")).toBe(0);
	});
	it("cint reads a field of the row as a whole number, as Desk does", () => {
		const c = {
			columns: [],
			formFields: [
				F("qty", "Float"),
				F("x", "Data", { depends_on: "eval:cint(doc.qty) > 1" }),
			],
		};
		const shown = (qty) =>
			rowFormLayout(c, { qty }, {}).flatMap((sec) =>
				sec.columns.flat().map((i) => i.field.fieldname)
			);
		expect(shown("2.7")).toEqual(["qty", "x"]);
		expect(shown("1.9")).toEqual(["qty"]);
		expect(shown("")).toEqual(["qty"]);
	});
	it("cint, flt and cstr match Desk's own semantics", () => {
		const ev = (js) => ruleHolds(`eval:${js}`, {}, {}, false);
		expect(ev("cint('007') === 7 && cint('0') === 0 && cint('') === 0")).toBe(true);
		expect(ev("cint(true) === 1 && cint(false) === 0 && cint(null) === 0")).toBe(true);
		expect(ev("cint('abc', 5) === 5 && cint('0x10') === 0 && cint(3.7) === 3")).toBe(true);
		expect(ev("flt('1,250.5') === 1250.5 && flt('') === 0 && flt(null) === 0")).toBe(true);
		expect(ev("flt('x') === 0 && flt(2.345, 2) === 2.35 && flt(7) === 7")).toBe(true);
		expect(ev("cstr(null) === '' && cstr(5) === '5' && cstr(undefined) === ''")).toBe(true);
		expect(ev("in_list(['a', 'b'], 'b') && !in_list(['a'], 'c')")).toBe(true);
	});
	it("a rule using erpnext.* still falls back instead of breaking the form", () => {
		const cost = F("cost_center", "Link", {
			depends_on: "eval:cint(erpnext.is_perpetual_inventory_enabled(parent.company))",
		});
		expect(rowFormLayout({ columns: [], formFields: [cost] }, {}, {})).toHaveLength(1);
	});
});

// The one "changed" set: what Save will send for a row, worked out before the layout.
describe("rowChangedSet: exactly what tableRowsPayload would send", () => {
	const saved = { ...row({ qty: "2", discount_percentage: "10" }), name: "r1" };
	it("a row with no saved match counts every non-blank cell", () => {
		const s = rowChangedSet(table, row({ discount_percentage: "0", warehouse: "" }), null);
		expect([...s].sort()).toEqual(
			["discount_percentage", "is_free_item", "item_code", "qty", "rate"].sort()
		);
	});
	it("a saved row counts only the cells that differ from it", () => {
		const r = { ...row({ qty: "2", discount_percentage: "0" }), __name: "r1" };
		expect([...rowChangedSet(table, r, saved)]).toEqual(["discount_percentage"]);
		expect([
			...rowChangedSet(
				table,
				{ ...row(), __name: "r1", qty: "2", discount_percentage: "10" },
				saved
			),
		]).toEqual([]);
	});
	it("a field a rule makes read-only still counts when it will be sent", () => {
		const t = {
			columns: [],
			formFields: [
				F("a", "Data", { read_only_depends_on: "eval:1" }),
				F("b", "Data", { read_only: 1 }),
			],
		};
		const s = rowChangedSet(t, { a: "x", b: "y", __name: "r1" }, { name: "r1", a: "", b: "" });
		expect([...s]).toEqual(["a"]); // a static read-only field is never sent
	});
	it("a Check compares as Yes/No, so a proposed Yes over a saved 1 is not a change", () => {
		const s = rowChangedSet(
			table,
			{ ...row({ is_free_item: "Yes" }), __name: "r1" },
			{ ...row({ is_free_item: "1" }), name: "r1" }
		);
		expect(s.has("is_free_item")).toBe(false);
	});
});

describe("rowExtraValues on an update", () => {
	const saved = { ...row({ discount_percentage: "10", warehouse: "A" }), name: "r1" };
	it("lists the changed fields first, with was/now, then the other filled values", () => {
		const r = {
			...row({ discount_percentage: "0", warehouse: "A", is_free_item: "1" }),
			__name: "r1",
		};
		const out = rowExtraValues(table, r, { update_stock: 1 }, { savedRow: saved });
		expect(out.map((x) => x.fieldname)).toEqual([
			"discount_percentage",
			"is_free_item",
			"warehouse",
		]);
		expect(out[0]).toEqual({
			fieldname: "discount_percentage",
			label: "discount percentage",
			was: "10",
			now: "0", // a new 0 is listed, not skipped as "empty"
			changed: true,
		});
		expect(out[1]).toMatchObject({ was: "No", now: "Yes", changed: true });
		expect(out[2]).toEqual({ fieldname: "warehouse", label: "warehouse", value: "A" });
	});
	it("a field cleared or unticked is a change too", () => {
		const s = { ...saved, is_free_item: 1 };
		const r = {
			...row({ discount_percentage: "10", warehouse: "", is_free_item: "0" }),
			__name: "r1",
		};
		const out = rowExtraValues(table, r, { update_stock: 1 }, { savedRow: s });
		expect(out.map((x) => [x.fieldname, x.was, x.now, x.value])).toEqual([
			["is_free_item", "Yes", "No", undefined],
			["warehouse", "A", "(empty)", undefined],
			["discount_percentage", undefined, undefined, "10"],
		]);
	});
});

describe("changeDisplay: one formatter for 'was' and 'now'", () => {
	it("blank reads (empty), zero reads 0, a Check reads Yes/No", () => {
		expect(changeDisplay(F("a"), "")).toBe("(empty)");
		expect(changeDisplay(F("a"), null)).toBe("(empty)");
		expect(changeDisplay(F("d", "Percent"), "0")).toBe("0");
		expect(changeDisplay(F("d", "Percent"), 0)).toBe("0");
		expect(changeDisplay(F("c", "Check"), "1")).toBe("Yes");
		expect(changeDisplay(F("c", "Check"), "false")).toBe("No");
		expect(changeDisplay(F("c", "Check"), "")).toBe("No");
		expect(changeDisplay(F("a"), "Stores")).toBe("Stores");
	});
});

// on a row with a matching saved row, a rule-hidden field is hidden unless it changed.
describe("an unchanged saved value a rule hides stays hidden", () => {
	const spec = {
		label: "Items",
		child_doctype: "Purchase Invoice Item",
		columns: [F("item_code", "Link"), F("qty", "Float")],
		extra_columns: [],
		form_fields: [
			F("item_code", "Link"),
			F("qty", "Float"),
			F("asset_category", "Link", { depends_on: "is_fixed_asset" }),
			F("cost_center", "Link", { depends_on: "eval:!doc.is_fixed_asset" }),
			F("discount_percentage", "Percent", { depends_on: "eval:doc.qty > 5" }),
		],
		rule_fields: [{ fieldname: "is_fixed_asset", fieldtype: "Check", default: "0" }],
	};
	const savedRow = (over = {}) => ({
		name: "r1",
		item_code: "A",
		qty: 1,
		asset_category: "",
		cost_center: "CC-1",
		discount_percentage: 10,
		is_fixed_asset: 1,
		...over,
	});
	const build = (verb, proposed) =>
		buildDraftTable({
			fieldname: "items",
			spec,
			proposed,
			required: true,
			saved: verb === "update" ? [savedRow()] : [],
			verb,
		});
	const shownFor = (t, r, savedR) =>
		rowFormLayout(t, r, { doctype: "Purchase Invoice" }, { savedRow: savedR }).flatMap((s) =>
			s.columns.flat().map((i) => i.field.fieldname)
		);

	it("an unchanged saved Cost Center under is_fixed_asset=1 is hidden, though it holds a value", () => {
		const t = build("update");
		const names = shownFor(t, t.rows[0], savedRow());
		expect(names).toContain("asset_category");
		expect(names).not.toContain("cost_center");
		// the same value is visible for a row with no saved match (the old rule)
		expect(shownFor(t, { ...t.rows[0], __name: undefined }, null)).toContain("cost_center");
	});
	it("a changed Cost Center is laid out", () => {
		const t = build("update");
		t.rows[0].cost_center = "CC-2";
		expect(shownFor(t, t.rows[0], savedRow())).toContain("cost_center");
	});
	it("on a new row (create) the Cost Center shows", () => {
		const t = build("create", [{ item_code: "A", cost_center: "CC-9" }]);
		expect(t.rows[0].__name).toBeUndefined();
		expect(shownFor(t, t.rows[0], null)).toContain("cost_center");
	});
	it("a rule-hidden field changed from 10 to 0 is laid out and shown (not hidden as zero)", () => {
		const t = build("update", [{ name: "r1", discount_percentage: 0 }]);
		expect(t.rows[0].discount_percentage).toBe("0");
		const names = shownFor(t, t.rows[0], savedRow());
		expect(names).toContain("discount_percentage"); // qty 1: its rule hides it, but it changed
		// and unchanged (10 = 10) it is hidden
		const same = build("update");
		expect(shownFor(same, same.rows[0], savedRow())).not.toContain("discount_percentage");
	});
	it("rowStatus gives the layout the same set it counts: no circularity", () => {
		const t = build("update");
		Object.assign(t.rows[0], { discount_percentage: "0", cost_center: "CC-2" });
		const s = rowStatus(t, t.rows[0], { doctype: "Purchase Invoice" }, savedRow());
		expect([...s.changedFields].sort()).toEqual(["cost_center", "discount_percentage"]);
		expect(s.changed).toBe(2);
		const u = build("update");
		expect(rowStatus(u, u.rows[0], {}, savedRow()).changed).toBe(0);
	});
	it("a field a rule makes read-only counts as changed if it will be sent", () => {
		const t = {
			columns: [F("qty", "Float")],
			formFields: [
				F("qty", "Float"),
				F("note", "Data", { read_only_depends_on: "eval:doc.qty > 0" }),
			],
		};
		const s = rowStatus(
			t,
			{ qty: "1", note: "x", __name: "r1" },
			{},
			{ name: "r1", qty: 1, note: "" }
		);
		expect(s.changedFields.has("note")).toBe(true);
		expect(s.changed).toBe(1);
	});
});
