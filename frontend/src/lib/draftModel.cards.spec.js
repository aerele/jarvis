// The shared form builder (lib/draftModel.js), run for a server draft card's action (marked
// FROM_CARD) and for a typed action, against a small form meta shaped like the server's
// `get_doctype_form_meta`, with expectations written out by hand. Pins how a card's key is resolved
// to a form field, and that a typed action still resolves by label: the two paths share this one
// builder. draftModel.spec.js holds the models the old in-view builder built for a wider set of
// inputs; these say what each case MUST be, so a regenerated golden cannot bless a wrong one.
import { describe, expect, it } from "vitest";
import { FROM_CARD } from "./toolDraftPanel.js";
import { makeDraftModelBuilder } from "./draftModel.js";

const F = (fieldname, label, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
const META = {
	ok: true,
	title_field: "",
	is_submittable: 0,
	fields: [
		F("posting_date", "Date", "Date"),
		F("date", "Posting Date", "Date"),
		F("subject", "Subject"),
		F("description", "Description", "Small Text"),
		F("project", "Project", "Link", { options: "Project" }),
	],
	tables: {},
};

// The builder with a fixed form meta and no rule values.
function builder({ loadDoc } = {}) {
	return makeDraftModelBuilder({
		getDoctypeFormMeta: async () => structuredClone(META),
		loadDocForEdit: loadDoc || (async () => ({ values: {}, tables: {} })),
		ruleValues: async () => ({ ok: false }),
	}).buildDraftModel;
}
const action = (over = {}) => ({
	kind: "doc",
	verb: "create",
	doctype: "Timesheet",
	fields: [],
	tables: [],
	...over,
});
const valuesOf = (model) => Object.fromEntries(model.fields.map((f) => [f.fieldname, f.value]));

describe("buildDraftModel for a server draft card (FROM_CARD)", () => {
	// Break: a drafted key lands on the field whose LABEL looks like it ("Date" is posting_date's
	// label, "date" is the fieldname of "Posting Date"): the value is shown, and saved, on the wrong field.
	it("resolves a drafted key to the field of that exact fieldname", async () => {
		const model = await builder()({
			...action({
				fields: [
					{ fieldname: "date", label: "date", value: "2026-12-09" },
					{ fieldname: "posting_date", label: "posting_date", value: "2026-12-08" },
				],
			}),
			[FROM_CARD]: true,
		});
		expect(valuesOf(model)).toEqual({ posting_date: "2026-12-08", date: "2026-12-09" });
	});

	// Break: a key the form does not hold is guessed at (a fuzzy label match fills "subject").
	it("leaves out a fieldname the form does not hold", async () => {
		const model = await builder()({
			...action({
				fields: [
					{ fieldname: "subj", label: "subj", value: "x" },
					{ fieldname: "project", label: "project", value: "PROJ-1" },
				],
			}),
			[FROM_CARD]: true,
		});
		expect(valuesOf(model)).toEqual({ project: "PROJ-1" });
	});

	// Break: an update whose saved record cannot be read is refused with a typed error, or
	// opens a form that does not say it shows only the draft.
	it("an update whose record cannot be loaded opens, marked storedUnavailable", async () => {
		const model = await builder({
			loadDoc: async () => {
				throw new Error("no permission");
			},
		})({
			...action({
				verb: "update",
				name: "TS-1",
				fields: [{ fieldname: "subject", label: "subject", value: "New" }],
				tables: [{ fieldname: "time_logs", rows: [{ hours: 1 }] }],
			}),
			[FROM_CARD]: true,
		});
		expect([model.storedUnavailable, valuesOf(model)]).toEqual([true, { subject: "New" }]);
	});
});

describe("buildDraftModel for a typed action (no FROM_CARD): unchanged", () => {
	// Break: a typed action honours a `fieldname` key (a model-written field with both would land on
	// the field its fieldname names instead of the one its label names).
	it("still resolves by label, whatever fieldname key the field carries", async () => {
		const model = await builder()(
			action({ fields: [{ label: "Subject", fieldname: "description", value: "Hi" }] })
		);
		expect(valuesOf(model)).toEqual({ subject: "Hi" });
	});

	// Break: the typed path stops refusing an update it cannot show the saved rows of (a table
	// would be applied blind) or starts marking storedUnavailable.
	it("refuses an update with tables when the record cannot be loaded", async () => {
		const build = builder({
			loadDoc: async () => {
				throw new Error("no permission");
			},
		});
		await expect(
			build(
				action({
					verb: "update",
					name: "TS-1",
					fields: [{ label: "Subject", value: "New" }],
					tables: [{ fieldname: "time_logs", rows: [{ hours: 1 }] }],
				})
			)
		).rejects.toThrow("Could not load this record's saved rows");
		const model = await build(
			action({ verb: "update", name: "TS-1", fields: [{ label: "Subject", value: "New" }] })
		);
		expect("storedUnavailable" in model).toBe(false);
	});
});

describe("fillRuleValues (the form's link pick asks again with its own isCurrent)", () => {
	const ORDER = {
		ok: true,
		title_field: "",
		is_submittable: 0,
		fields: [F("customer", "Customer"), F("items", "Items", "Table", { reqd: 1 })],
		tables: {
			items: {
				label: "Items",
				child_doctype: "Order Item",
				columns: [F("item_code", "Item Code", "Link"), F("qty", "Qty", "Float")],
				extra_columns: [],
				form_fields: [],
				rule_fields: [
					{
						fieldname: "is_fixed_asset",
						fieldtype: "Check",
						default: "0",
						fetch_from: "item_code.is_fixed_asset",
					},
				],
			},
		},
	};
	async function modelWithAnswer() {
		const asked = [];
		const built = makeDraftModelBuilder({
			getDoctypeFormMeta: async () => structuredClone(ORDER),
			loadDocForEdit: async () => ({ values: {}, tables: {} }),
			ruleValues: async (doctype, table, links) => {
				asked.push([doctype, table, links]);
				return { ok: true, values: { item_code: { A: { is_fixed_asset: 1 } } } };
			},
		});
		const model = await built.buildDraftModel({
			kind: "doc",
			verb: "create",
			doctype: "Order",
			fields: [{ label: "Customer", value: "C" }],
			tables: [{ fieldname: "items", rows: [{ item_code: "A" }] }],
		});
		return { built, model, asked };
	}

	// Break: an answer that lands after its panel was closed or replaced still writes into the
	// model (the next draft's rows would carry the old draft's rule values).
	it("writes the answer only while isCurrent says the panel is still the one asked for", async () => {
		const { built, model, asked } = await modelWithAnswer();
		// the build itself already applied the answer; start again from the defaults
		model.tables[0].rows[0].__rule = { is_fixed_asset: "0" };
		delete model.tables[0].rows[0].__ruleSrc;
		await built.fillRuleValues("Order", model.tables, () => false);
		expect(model.tables[0].rows[0].__rule).toEqual({ is_fixed_asset: "0" });
		await built.fillRuleValues("Order", model.tables, () => true);
		expect(model.tables[0].rows[0].__rule).toEqual({ is_fixed_asset: 1 });
		expect(asked.at(-1)).toEqual(["Order", "items", { item_code: ["A"] }]);
	});

	// Break: a table whose rows need nothing still costs a request, or the form-meta cache is
	// shared by every builder (each builder keeps its own, one per mounted view, as the closure did).
	it("asks nothing for a table with no rule fields, and each builder caches its own form meta", async () => {
		const { built, asked } = await modelWithAnswer();
		await built.fillRuleValues("Order", [{ fieldname: "plain", rows: [], ruleFields: [] }]);
		expect(asked.length).toBe(1); // only the build's own request
		expect(Object.keys(built.formMetaCache)).toEqual(["Order"]);
		const other = makeDraftModelBuilder({
			getDoctypeFormMeta: async () => structuredClone(ORDER),
			loadDocForEdit: async () => ({ values: {}, tables: {} }),
			ruleValues: async () => ({ ok: false }),
		});
		expect(other.formMetaCache).toEqual({});
	});
});
