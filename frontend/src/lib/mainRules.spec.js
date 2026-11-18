// A main field's mandatory_depends_on in the draft form: which fields the build puts on the form
// for it, and what the live form calls required or "may be required" (lib/mainRules.js).
import { describe, expect, it } from "vitest";
import { draftValues, isFieldMissing } from "./draftApply.js";
import { draftCardDirty, draftCardValues, snapshotDraftModel } from "./draftCardValues.js";
import { makeDraftModelBuilder } from "./draftModel.js";
import { mainRuleStates, revealRuleFields } from "./mainRules.js";

const F = (fieldname, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label: fieldname.replace(/_/g, " "),
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
const NAME = F("item_name", "Data", { reqd: 1 });
const SALES = F("is_sales_item", "Check", { default: "1" });
const HSN = F("hsn_code", "Data", { mandatory_depends_on: "eval:doc.is_sales_item" });
const STATUS = F("status", "Select", { options: "Open\nCompleted\nCancelled" });
const DONE_ON = F("completed_on", "Date", {
	mandatory_depends_on: 'eval:doc.status=="Completed"',
});
const GST = F("gst_code", "Data", {
	mandatory_depends_on: "eval:gst_settings.validate_hsn_code && doc.is_sales_item",
});

const LINKS = {
	label: "Links",
	child_doctype: "Dynamic Link",
	columns: [F("link_name")],
	extra_columns: [],
	form_fields: [],
};

async function build(fields, stored, { proposed = [], tables = [], metaTables = {} } = {}) {
	const deps = {
		getDoctypeFormMeta: async () => ({
			ok: true,
			fields: JSON.parse(JSON.stringify(fields)),
			tables: JSON.parse(JSON.stringify(metaTables)),
		}),
		loadDocForEdit: async () => ({ values: stored || {}, tables: {}, docstatus: 0 }),
		ruleValues: async () => ({ ok: true, values: {} }),
	};
	const a = { kind: "doc", doctype: "Item", verb: stored ? "update" : "create" };
	a.fields = [{ label: "item_name", value: "Pen" }, ...proposed];
	if (tables.length) a.tables = tables;
	if (stored) a.name = "ITM-1";
	return makeDraftModelBuilder(deps).buildDraftModel(a);
}
const field = (model, name) => model.fields.find((f) => f.fieldname === name);
// What the form shows for a field: required means the star and the missing cue.
const required = (model, f) => mainRuleStates(model)[f.fieldname] === true;
const cue = (model, f) => isFieldMissing(f, required(model, f));

describe("a main field with mandatory_depends_on", () => {
	// Break: a field the rule makes required is left off the form because it is neither proposed
	// nor statically required; or its star stays after the person changes the value the rule
	// reads; or the rule cannot read a meta default that is not on the form.
	it("is put on the form from a meta default, and stops being required when the value changes", async () => {
		const model = await build([NAME, SALES, HSN]);
		const hsn = field(model, "hsn_code");
		expect(hsn).toBeTruthy();
		expect(hsn.proposed).toBe(false);
		expect(field(model, "is_sales_item")).toBeUndefined();
		expect([required(model, hsn), cue(model, hsn)]).toEqual([true, true]);
		hsn.value = "1234";
		expect([required(model, hsn), cue(model, hsn)]).toEqual([true, false]);
		hsn.value = "";
		// The person turns the field the rule reads off: it is a field of the form once proposed,
		// so the live doc reads the form's value (a Check as 1 or 0), not the default.
		model.fields.push({ ...SALES, control: "check", value: "No", orig: "No" });
		expect([required(model, hsn), cue(model, hsn)]).toEqual([false, false]);
	});

	// Break: a stored value is ignored, so an update shows a field the saved record does not need.
	it("reads the stored record over the default on an update", async () => {
		const model = await build([NAME, SALES, HSN], { is_sales_item: 0 });
		expect(field(model, "hsn_code")).toBeUndefined();
	});

	// Break: an unreadable rule counts as required (a star and a cue for a field that may not be),
	// reads as not required with no word on the form, or keeps the field off it.
	it("an unreadable rule is on the form as 'may be required', not missing", async () => {
		const model = await build([NAME, SALES, GST]);
		const gst = field(model, "gst_code");
		expect(gst).toBeTruthy();
		expect(mainRuleStates(model).gst_code).toBeNull();
		expect([required(model, gst), cue(model, gst)]).toEqual([false, false]);
	});

	// Break: a read_only field is added for its rule, though the person cannot fill it.
	it("never adds a read_only field for its rule", async () => {
		const ro = F("hsn_code", "Data", { read_only: 1, mandatory_depends_on: "eval:1" });
		expect(field(await build([NAME, ro]), "hsn_code")).toBeUndefined();
	});
});

describe("a field shown only for its rule is never sent unchosen", () => {
	const STOCK = F("is_stock", "Check", { default: "1", mandatory_depends_on: "eval:1" });

	// Break: the older create form sends every non-blank field, so an untouched rule-added Check
	// ("No" on the form) is written as 0 over the schema default of 1.
	it("typed-card create leaves out an untouched rule-added Check and sends it once changed", async () => {
		const model = await build([NAME, STOCK]);
		const stock = field(model, "is_stock");
		expect(stock.ruleAdded).toBe(true);
		expect(draftValues(model)).toEqual({ item_name: "Pen" });
		stock.value = "Yes";
		expect(draftValues(model)).toEqual({ item_name: "Pen", is_stock: 1 });
	});

	// Break: a field revealed after the snapshot reads as a change against a missing baseline, so
	// the server draft's form is dirty and sends it untouched.
	it("a revealed field on a server draft's form is not dirty and not sent until edited", async () => {
		const model = await build([NAME, STOCK, DONE_ON, STATUS], undefined, {
			proposed: [{ label: "status", value: "Open" }],
		});
		model.tool = true;
		model.snapshot = snapshotDraftModel(model, {});
		model.fields.find((f) => f.fieldname === "status").value = "Completed";
		model.snapshot.fields.status = "Completed";
		expect(revealRuleFields(model)).toBe(1);
		expect(draftCardDirty(model, model.snapshot, {})).toBe(false);
		expect(draftCardValues(model, model.snapshot, {})).toEqual({});
		field(model, "completed_on").value = "2026-12-01";
		expect(draftCardValues(model, model.snapshot, {})).toEqual({ completed_on: "2026-12-01" });
	});
});

describe("a rule that turns true after the build", () => {
	// Break: the field is only ever added at build, so an edit that makes its rule hold shows
	// nothing; or a revealed field leaves again when its rule turns false.
	it("reveals the field on the edit, once, and keeps it", async () => {
		const model = await build([NAME, STATUS, DONE_ON], undefined, {
			proposed: [{ label: "status", value: "Open" }],
		});
		expect(field(model, "completed_on")).toBeUndefined();
		expect(revealRuleFields(model)).toBe(0);
		field(model, "status").value = "Completed";
		expect(revealRuleFields(model)).toBe(1);
		const done = field(model, "completed_on");
		expect([done.ruleAdded, required(model, done)]).toEqual([true, true]);
		expect(model.ruleFields).toEqual([]);
		field(model, "status").value = "Open";
		expect(revealRuleFields(model)).toBe(0);
		expect(field(model, "completed_on")).toBe(done);
	});

	// Break: a field Desk hides (its depends_on is false) is added for its mandatory rule.
	it("adds nothing, at build or on an edit, while depends_on is false", async () => {
		const hidden = {
			...DONE_ON,
			mandatory_depends_on: 'eval:doc.status!="Open"',
			depends_on: 'eval:doc.status=="Cancelled"',
		};
		const model = await build([NAME, STATUS, hidden], undefined, {
			proposed: [{ label: "status", value: "Completed" }],
		});
		expect(field(model, "completed_on")).toBeUndefined();
		expect(revealRuleFields(model)).toBe(0);
		field(model, "status").value = "Cancelled";
		expect(revealRuleFields(model)).toBe(1);
	});
});

describe("a rule that reads a table", () => {
	const ADDR = F("first_name", "Data", { mandatory_depends_on: "eval:!doc.links?.length" });
	const LINKS_F = F("links", "Table", { options: "Dynamic Link" });
	const meta = { metaTables: { links: LINKS } };
	const withRows = (rows) => ({ ...meta, tables: [{ fieldname: "links", rows }] });

	// Break: the build reads the agent's raw rows and the live form the grid's, so the same rows
	// answer differently; or a blank placeholder grid row counts as a row; or a row is not a row
	// until it has a value in it.
	it("counts only real rows, the same at build and after an edit", async () => {
		const none = await build([NAME, ADDR, LINKS_F], undefined, meta);
		expect(mainRuleStates(none).first_name).toBe(true);
		// the grid's own blank placeholder row (buildDraftTable adds one) is not a row
		const blank = await build([NAME, ADDR, LINKS_F], undefined, withRows([]));
		expect(blank.tables[0].rows).toHaveLength(1);
		expect(field(blank, "first_name")).toBeTruthy();
		expect(mainRuleStates(blank).first_name).toBe(true);
		const one = await build([NAME, ADDR, LINKS_F], undefined, withRows([{ link_name: "X" }]));
		expect(field(one, "first_name")).toBeUndefined();
		// the person fills the placeholder row: the live answer is the one the build gives
		blank.tables[0].rows[0].link_name = "X";
		expect(mainRuleStates(blank).first_name).toBe(false);
	});

	// Break: every form walks its tables for every rule, or a table is put in the doc that no rule
	// names.
	it("keeps the table names on the model only when a rule names one", async () => {
		const named = await build([NAME, ADDR, LINKS_F], undefined, meta);
		expect(named.ruleTables).toEqual(["links"]);
		const other = await build([NAME, SALES, HSN, LINKS_F], undefined, meta);
		expect(other.ruleTables).toBeUndefined();
	});
});
