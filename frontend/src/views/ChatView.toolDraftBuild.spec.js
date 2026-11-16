// The REAL `buildDraftModel` of ChatView.vue, cut out of the source and executed, against a small
// form meta shaped like the server's `get_doctype_form_meta`. Pins how a server draft card's
// action (marked FROM_CARD) is resolved to form fields, and that a typed action still resolves by
// label: the two paths share this one builder, so a text pin on the call would not catch a change
// in what it does.
import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";
import { panelField } from "../lib/docFields.js";
import { checkToYesNo } from "../lib/draftApply.js";
import { buildDraftTable } from "../lib/draftTables.js";
import { FROM_CARD, resolveDraftField } from "../lib/toolDraftPanel.js";

const src = fs.readFileSync(path.resolve(__dirname, "ChatView.vue"), "utf8");
function cut(from, to) {
	const a = src.indexOf(from);
	expect(a, `not found: ${from}`).toBeGreaterThan(-1);
	const b = src.indexOf(to, a + from.length);
	expect(b, `not found: ${to}`).toBeGreaterThan(a);
	return src.slice(a, b);
}

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

// buildDraftModel with its collaborators handed in: the real helpers, a fixed form meta.
function builder({ loadDoc } = {}) {
	const code = [
		cut("function _normKey(s) {", "// --- Record draft panel"),
		cut("async function buildDraftModel(a) {", "\n// Open the editable panel"),
		"return buildDraftModel;",
	].join("\n");
	const meta = structuredClone(META);
	const norm = (s) =>
		String(s || "")
			.toLowerCase()
			.replace(/[^a-z0-9]/g, "");
	for (const f of meta.fields) {
		f._kl = norm(f.label);
		f._kf = norm(f.fieldname);
	}
	const scope = {
		_formMeta: async () => meta,
		api: { loadDocForEdit: loadDoc || (async () => ({ values: {}, tables: {} })) },
		_panelField: panelField,
		buildDraftTable,
		checkToYesNo,
		resolveDraftField,
		FROM_CARD,
		fillRuleValues: async () => {},
		RULE_VALUES_WAIT_MS: 5,
		parentDefaults: () => ({}),
		setTimeout,
	};
	return new Function(...Object.keys(scope), code)(...Object.values(scope));
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
