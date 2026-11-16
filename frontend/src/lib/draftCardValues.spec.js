// What the edit form of a server draft card sends: only what the person changed.
//
// Models are built with the real panelField / buildDraftTable (what ChatView's
// buildDraftModel calls), and each case edits the model the way the form does, then
// compares with an expected payload written out by hand.
//
// MODEL SHAPE ASSUMED (the host's buildDraftModel result, minimum):
//   { verb, doctype, fields: [panelField(...) + proposed], tables: [buildDraftTable(...)] }
// a table's rows carry the `__key` newRowKey() minted, `__name` on a saved row, the cells as
// strings, and `__rule` / `__ruleSrc` / `__open` / `__missing` as display state.
import { describe, expect, it } from "vitest";
import { applyRuleValues, blankRow, buildDraftTable } from "./draftTables";
import { panelField } from "./docFields";
import {
	draftCardDirty,
	draftCardValues,
	snapshotDraftModel,
	snapshotMatchesCard,
} from "./draftCardValues";
import CONTRACT from "./draftCardContractCases.json";

const F = (fieldname, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});

// A Timesheet-like record: main fields of every kind the compare treats differently.
const MAIN = [
	F("title"),
	F("qty", "Float"),
	F("due", "Date"),
	F("at", "Datetime"),
	F("urgent", "Check"),
	F("customer", "Link", { options: "Customer" }),
	F("territory", "Link", { options: "Territory" }),
	F("notes", "Small Text"),
	F("total", "Currency", { read_only: 1 }),
	F("attachment", "Attach"),
	F("secret", "Password"),
];
const LOGS = {
	label: "Logs",
	child_doctype: "Log",
	columns: [
		F("activity", "Link", { options: "Activity" }),
		F("hours", "Float"),
		F("from_time", "Datetime"),
		F("billable", "Check"),
		F("note"),
		F("amount", "Currency", { read_only: 1 }),
	],
	extra_columns: [],
	form_fields: [],
};
const WITH_RULE = {
	...LOGS,
	rule_fields: [{ fieldname: "is_fixed", fetch_from: "activity.is_fixed", default: 0 }],
};

const DRAFT = {
	title: "Hello",
	qty: "3",
	due: "2026-11-01",
	at: "2026-11-01 09:30:00",
	urgent: 1,
	customer: "C-1",
	notes: "first line",
	total: 10,
	attachment: "/files/a.pdf",
	secret: "pw",
};
const ROWS = () => [
	{ activity: "A1", hours: 1.5, from_time: "2026-11-01 09:00:00", billable: 1, amount: 15 },
	{ activity: "A2", hours: 2, note: "two" },
	{ activity: "A3", hours: "3" },
];

// A create draft: `values` is the card's draft.values; the model is built from it as the
// host builds it (every drafted field shown and proposed, the table from the drafted rows).
function createModel(values, { spec = LOGS, extra = [] } = {}) {
	const fields = MAIN.filter((m) => m.fieldname in values).map((m) => ({
		...panelField(m, values[m.fieldname]),
		proposed: true,
	}));
	for (const name of extra)
		fields.push(
			panelField(
				MAIN.find((m) => m.fieldname === name),
				""
			)
		);
	const table = buildDraftTable({
		fieldname: "logs",
		spec,
		required: true,
		proposed: values.logs,
		verb: "create",
	});
	const card = { draft: { verb: "create", doctype: "Timesheet", values } };
	return { model: { verb: "create", doctype: "Timesheet", fields, tables: [table] }, card };
}

const SAVED = () => [
	{
		name: "tl-1",
		activity: "A1",
		hours: 1,
		from_time: "2026-11-01 09:00:00",
		billable: 0,
		amount: 10,
	},
	{
		name: "tl-2",
		activity: "A2",
		hours: 2,
		from_time: "2026-11-01 11:00:00",
		billable: 1,
		amount: 20,
	},
	{
		name: "tl-3",
		activity: "A3",
		hours: 3,
		from_time: "2026-11-01 13:00:00",
		billable: 0,
		amount: 30,
	},
];
// An update draft: `values` are the drafted changes; the table shows the draft's final rows
// laid over the stored ones (the builder does that), or the stored rows when the draft has none.
function updateModel(values, saved = SAVED()) {
	const fields = MAIN.filter((m) => m.fieldname in values).map((m) => ({
		...panelField(m, values[m.fieldname]),
		proposed: true,
	}));
	const table = buildDraftTable({
		fieldname: "logs",
		spec: LOGS,
		required: false,
		proposed: values.logs,
		saved,
		verb: "update",
	});
	const card = { draft: { verb: "update", doctype: "Timesheet", name: "TS-1", values } };
	return { model: { verb: "update", doctype: "Timesheet", fields, tables: [table] }, card };
}

const field = (m, name) => m.fields.find((f) => f.fieldname === name);
const logs = (m) => m.tables[0];

// Run one case: build, snapshot, let the case edit like the form does, read the payload.
function run(build, edit) {
	const { model, card } = build();
	const snap = snapshotDraftModel(model, card);
	edit(model);
	return { values: draftCardValues(model, snap, card), model, snap, card };
}

describe("main fields", () => {
	const create = () => createModel({ ...DRAFT, logs: ROWS() });
	const cases = [
		["nothing touched", () => {}, {}],
		["one field edited", (m) => (field(m, "title").value = "Bye"), { title: "Bye" }],
		["a field cleared is sent as ''", (m) => (field(m, "notes").value = ""), { notes: "" }],
		[
			"blanks of any kind clear alike",
			(m) => (field(m, "title").value = "   "),
			{ title: "" },
		],
		[
			"cleared, then retyped to the original",
			(m) => {
				field(m, "title").value = "";
				field(m, "title").value = "Hello";
			},
			{},
		],
		["'3' and 3 are the same number", (m) => (field(m, "qty").value = 3), {}],
		["'3.0' is the same number too", (m) => (field(m, "qty").value = "3.0"), {}],
		[
			"a number really changed goes out as a number",
			(m) => (field(m, "qty").value = "4.5"),
			{ qty: 4.5 },
		],
		["a number cleared is '' (not 0)", (m) => (field(m, "qty").value = ""), { qty: "" }],
		["0 is a change from 3, not a blank", (m) => (field(m, "qty").value = "0"), { qty: 0 }],
		["a date the control rewrote", (m) => (field(m, "due").value = "2026-11-01 00:00:00"), {}],
		[
			"a date in dd-mm-yyyy is the same day",
			(m) => (field(m, "due").value = "01-11-2026"),
			{},
		],
		[
			"a date really changed",
			(m) => (field(m, "due").value = "2026-12-24"),
			{ due: "2026-12-24" },
		],
		[
			"a datetime reformatted by the control",
			(m) => (field(m, "at").value = "2026-11-01T09:30"),
			{},
		],
		[
			"a datetime really changed",
			(m) => (field(m, "at").value = "2026-11-01T10:00"),
			{ at: "2026-11-01T10:00" },
		],
		[
			"a checkbox toggled and toggled back",
			(m) => {
				field(m, "urgent").value = "No";
				field(m, "urgent").value = "Yes";
			},
			{},
		],
		[
			"a checkbox toggled off is 0, not ''",
			(m) => (field(m, "urgent").value = "No"),
			{ urgent: 0 },
		],
		["a link picked", (m) => (field(m, "customer").value = "C-2"), { customer: "C-2" }],
		[
			"a link picked, then cleared",
			(m) => {
				field(m, "customer").value = "C-2";
				field(m, "customer").value = "";
			},
			{ customer: "" },
		],
		[
			"a link blank at the start, picked then cleared",
			(m) => {
				m.fields.push(
					panelField(
						MAIN.find((f) => f.fieldname === "territory"),
						""
					)
				);
				m.fields.at(-1).value = "T-1";
				m.fields.at(-1).value = "";
			},
			{},
		],
		["a read-only field is never sent", (m) => (field(m, "total").value = 99), {}],
		[
			"an attachment is never sent",
			(m) => (field(m, "attachment").value = "/files/b.pdf"),
			{},
		],
		["a secret is never sent", (m) => (field(m, "secret").value = "new"), {}],
	];
	for (const [name, edit, expected] of cases) {
		it(`create: ${name}`, () => {
			expect(run(create, edit).values).toEqual(expected);
		});
	}

	it("a field the failed-save marks appended (not in the snapshot) counts as blank before the edit", () => {
		const build = () => createModel({ title: "Hello", logs: ROWS() });
		const untouched = run(build, (m) => m.fields.push(panelField(MAIN[7], "")));
		expect(untouched.values).toEqual({});
		const filled = run(build, (m) => m.fields.push(panelField(MAIN[7], "typed")));
		expect(filled.values).toEqual({ notes: "typed" });
	});

	it("an update: a changed field is sent, a read-only one is not even when proposed", () => {
		const build = () => updateModel({ title: "Hello", total: 5 }, SAVED());
		const { values } = run(build, (m) => {
			field(m, "title").value = "Bye";
			field(m, "total").value = 6;
		});
		expect(values).toEqual({ title: "Bye" });
	});
});

describe("child rows of a create draft (3 drafted rows)", () => {
	const build = () => createModel({ title: "Hello", logs: ROWS() });
	const filled = (t, cells) => Object.assign(blankRow(t), cells);
	const cases = [
		["nothing touched: the table is absent", () => {}, {}],
		[
			"one cell edited in row 2 of 3: all three rows, only row 2 carries the cell",
			(m) => (logs(m).rows[1].hours = "5"),
			{ logs: [{ _draft_row: 0 }, { _draft_row: 1, hours: 5 }, { _draft_row: 2 }] },
		],
		[
			"a cell the person emptied is sent as ''",
			(m) => (logs(m).rows[1].note = ""),
			{ logs: [{ _draft_row: 0 }, { _draft_row: 1, note: "" }, { _draft_row: 2 }] },
		],
		[
			"a cell edited then restored: the table is absent",
			(m) => {
				logs(m).rows[1].hours = "5";
				logs(m).rows[1].hours = 2;
			},
			{},
		],
		["'3.0' where the draft said '3'", (m) => (logs(m).rows[2].hours = "3.0"), {}],
		[
			"a datetime cell rewritten by the control",
			(m) => (logs(m).rows[0].from_time = "2026-11-01T09:00"),
			{},
		],
		[
			"a check cell written as 'Yes' where the draft said 1",
			(m) => (logs(m).rows[0].billable = "Yes"),
			{},
		],
		[
			"a check cell turned off is 0",
			(m) => (logs(m).rows[0].billable = "0"),
			{ logs: [{ _draft_row: 0, billable: 0 }, { _draft_row: 1 }, { _draft_row: 2 }] },
		],
		["a read-only cell is never sent", (m) => (logs(m).rows[0].amount = "999"), {}],
		[
			"a row added at the end has no _draft_row and all its filled cells",
			(m) =>
				logs(m).rows.push(filled(logs(m), { activity: "A4", hours: "2", billable: "0" })),
			{
				logs: [
					{ _draft_row: 0 },
					{ _draft_row: 1 },
					{ _draft_row: 2 },
					{ activity: "A4", hours: 2, billable: 0 },
				],
			},
		],
		[
			"a blank row added and left alone is nothing",
			(m) => logs(m).rows.push(blankRow(logs(m))),
			{},
		],
		[
			"a row inserted in the middle has no _draft_row; the rows after it keep their original index",
			(m) => logs(m).rows.splice(1, 0, filled(logs(m), { activity: "NEW" })),
			{
				logs: [
					{ _draft_row: 0 },
					{ activity: "NEW" },
					{ _draft_row: 1 },
					{ _draft_row: 2 },
				],
			},
		],
		[
			"a row removed: the others keep their original index",
			(m) => logs(m).rows.splice(1, 1),
			{ logs: [{ _draft_row: 0 }, { _draft_row: 2 }] },
		],
		[
			"the first row removed and one added: the added row never takes index 0",
			(m) => {
				logs(m).rows.splice(0, 1);
				logs(m).rows.push(filled(logs(m), { activity: "NEW" }));
			},
			{ logs: [{ _draft_row: 1 }, { _draft_row: 2 }, { activity: "NEW" }] },
		],
		[
			"rows swapped: the new order, each with its original index",
			(m) => {
				const r = logs(m).rows;
				[r[0], r[2]] = [r[2], r[0]];
			},
			{ logs: [{ _draft_row: 2 }, { _draft_row: 1 }, { _draft_row: 0 }] },
		],
		[
			"a row added then removed again: the table is absent",
			(m) => {
				logs(m).rows.push(filled(logs(m), { activity: "A4" }));
				logs(m).rows.pop();
			},
			{},
		],
		["every row removed: the empty list", (m) => logs(m).rows.splice(0, 3), { logs: [] }],
		[
			"a drafted row whose every cell was emptied is left out of the list",
			(m) => {
				const r = logs(m).rows[2];
				r.activity = "";
				r.hours = "";
			},
			{ logs: [{ _draft_row: 0 }, { _draft_row: 1 }] },
		],
		[
			"display state is never sent",
			(m) => {
				logs(m).rows[1].__open = true;
				logs(m).rows[1].__missing = ["hours"];
				logs(m).rows[1].__ruleSrc = { activity: "A2" };
				logs(m).rows[1].hours = "7";
			},
			{ logs: [{ _draft_row: 0 }, { _draft_row: 1, hours: 7 }, { _draft_row: 2 }] },
		],
	];
	for (const [name, edit, expected] of cases) {
		it(name, () => {
			expect(run(build, edit).values).toEqual(expected);
		});
	}

	it("a table the draft did not list: a filled row has no identity, an untouched blank row is nothing", () => {
		const bare = () => createModel({ title: "Hello" });
		expect(run(bare, () => {}).values).toEqual({});
		expect(
			run(bare, (m) => Object.assign(logs(m).rows[0], { activity: "X", hours: "1" })).values
		).toEqual({
			logs: [{ activity: "X", hours: 1 }],
		});
	});

	it("rows that do not line up with the draft's list are all sent whole, with no identity", () => {
		// the builder dropped a row before the snapshot: row i is no longer draft row i
		const build2 = () => {
			const built = createModel({ title: "Hello", logs: ROWS() });
			built.model.tables[0].rows.splice(0, 1);
			return built;
		};
		const { values } = run(build2, (m) => (logs(m).rows[0].hours = "9"));
		expect(values).toEqual({
			logs: [
				{ activity: "A2", hours: 9, note: "two" },
				{ activity: "A3", hours: 3 },
			],
		});
	});

	it("never carries name on a create row or a private key anywhere in the payload", () => {
		const named = () =>
			createModel({ title: "Hello", logs: [{ name: "x-1", activity: "A1", hours: 1 }] });
		const { values } = run(named, (m) => {
			logs(m).rows[0].hours = "2";
			logs(m).rows.push(Object.assign(blankRow(logs(m)), { activity: "A9" }));
		});
		expect(values).toEqual({ logs: [{ _draft_row: 0, hours: 2 }, { activity: "A9" }] });
		expect(JSON.stringify(values)).not.toMatch(/__|"name"/);
	});
});

describe("child rows of an update draft (identity: the row's name, or the index of a row the draft added)", () => {
	// the draft sets hours on tl-1 and adds a new row of its own
	const draftRows = () => [
		{ name: "tl-1", hours: 2 },
		{ name: "tl-2" },
		{ name: "tl-3" },
		{ activity: "A9", hours: 9 },
	];
	const build = () => updateModel({ title: "Hello", logs: draftRows() });
	const filled = (t, cells) => Object.assign(blankRow(t), cells);
	const cases = [
		["nothing touched: the table is absent", () => {}, {}],
		[
			"one cell edited in row 2: saved rows by name, the draft's own new row by its index in the draft",
			(m) => (logs(m).rows[1].note = "late"),
			{
				logs: [
					{ name: "tl-1" },
					{ name: "tl-2", note: "late" },
					{ name: "tl-3" },
					{ _draft_row: 3 },
				],
			},
		],
		[
			"a cell the person emptied is ''",
			(m) => (logs(m).rows[2].activity = ""),
			{
				logs: [
					{ name: "tl-1" },
					{ name: "tl-2" },
					{ name: "tl-3", activity: "" },
					{ _draft_row: 3 },
				],
			},
		],
		[
			"edited then restored",
			(m) => {
				logs(m).rows[1].hours = "8";
				logs(m).rows[1].hours = 2;
			},
			{},
		],
		[
			"a new row has no name",
			(m) => logs(m).rows.push(filled(logs(m), { activity: "N" })),
			{
				logs: [
					{ name: "tl-1" },
					{ name: "tl-2" },
					{ name: "tl-3" },
					{ _draft_row: 3 },
					{ activity: "N" },
				],
			},
		],
		[
			"a saved row removed is simply absent",
			(m) => logs(m).rows.splice(1, 1),
			{ logs: [{ name: "tl-1" }, { name: "tl-3" }, { _draft_row: 3 }] },
		],
		[
			"the draft's own new row removed",
			(m) => logs(m).rows.pop(),
			{ logs: [{ name: "tl-1" }, { name: "tl-2" }, { name: "tl-3" }] },
		],
		["a read-only cell is never sent", (m) => (logs(m).rows[0].amount = "1"), {}],
	];
	for (const [name, edit, expected] of cases) {
		it(name, () => {
			expect(run(build, edit).values).toEqual(expected);
		});
	}

	it("a table the draft did not list shows the stored rows and is absent until touched", () => {
		const stored = () => updateModel({ title: "Hello" });
		expect(run(stored, () => {}).values).toEqual({});
		expect(run(stored, (m) => (logs(m).rows[2].hours = "4")).values).toEqual({
			logs: [{ name: "tl-1" }, { name: "tl-2" }, { name: "tl-3", hours: 4 }],
		});
	});

	it("a saved row the person emptied completely stays, by its name (only a removal deletes it)", () => {
		const bare = () =>
			updateModel({ title: "Hello" }, [
				{ name: "tl-1", activity: "A1", hours: 1 },
				{ name: "tl-2", activity: "A2", hours: 2 },
			]);
		const { values } = run(bare, (m) => {
			logs(m).rows[1].activity = "";
			logs(m).rows[1].hours = "";
		});
		expect(values).toEqual({
			logs: [{ name: "tl-1" }, { name: "tl-2", activity: "", hours: "" }],
		});
	});

	it("a named row never carries _draft_row, the draft's nameless row never carries name", () => {
		const { values } = run(build, (m) => {
			logs(m).rows[0].hours = "5";
			logs(m).rows[3].note = "x";
		});
		expect(values).toEqual({
			logs: [
				{ name: "tl-1", hours: 5 },
				{ name: "tl-2" },
				{ name: "tl-3" },
				{ _draft_row: 3, note: "x" },
			],
		});
	});
});

describe("update rows the draft itself added: identity is their index in the draft's list", () => {
	// the draft's list counts named rows too: X is index 0 and Y index 2
	const draftRows = () => [
		{ activity: "X", hours: 1 },
		{ name: "tl-1" },
		{ activity: "Y", hours: 2 },
		{ name: "tl-2" },
	];
	const build = () => updateModel({ logs: draftRows() });

	it("each carries its index in the FULL drafted list, not its place among the new rows", () => {
		const { values } = run(build, (m) => (logs(m).rows[2].activity = "Z"));
		expect(values).toEqual({
			logs: [
				{ _draft_row: 0 },
				{ name: "tl-1" },
				{ _draft_row: 2, activity: "Z" },
				{ name: "tl-2" },
			],
		});
	});

	it("a row the person adds has no identity; the drafted one keeps its own", () => {
		const { values } = run(build, (m) => {
			logs(m).rows.splice(1, 0, Object.assign(blankRow(logs(m)), { activity: "Mine" }));
		});
		expect(values.logs).toEqual([
			{ _draft_row: 0 },
			{ activity: "Mine" },
			{ name: "tl-1" },
			{ _draft_row: 2 },
			{ name: "tl-2" },
		]);
	});

	it("no origin when the new rows at snapshot time are not the draft's (every one is sent whole)", () => {
		// a person-added row is already in the grid when the snapshot is taken: 3 nameless rows
		// against the draft's 2, so none can be matched
		const extra = () => {
			const built = build();
			logs(built.model).rows.push(
				Object.assign(blankRow(logs(built.model)), { activity: "Q" })
			);
			return built;
		};
		const { values } = run(extra, (m) => (logs(m).rows[1].hours = "8"));
		expect(values.logs).toEqual([
			{ activity: "X", hours: 1 },
			{ name: "tl-1", hours: 8 },
			{ activity: "Y", hours: 2 },
			{ name: "tl-2" },
			{ activity: "Q" },
		]);
	});

	it("no origin when the saved rows are not in the draft's order at snapshot time", () => {
		// same count of new rows, but the grid is not laid out as the draft: do not guess
		const swapped = () => {
			const built = build();
			const rows = logs(built.model).rows;
			[rows[1], rows[3]] = [rows[3], rows[1]];
			return built;
		};
		const { values } = run(swapped, (m) => (logs(m).rows[0].hours = "5"));
		expect(values.logs[0]).toEqual({ activity: "X", hours: 5 });
		expect(JSON.stringify(values)).not.toContain("_draft_row");
	});

	it("a column called name or _draft_row never overwrites a row's identity", () => {
		const spec = { ...LOGS, columns: [...LOGS.columns, F("name"), F("_draft_row", "Int")] };
		const make = (verb) => () => {
			const rows =
				verb === "update" ? [{ name: "tl-1" }, { activity: "Y" }] : [{ hours: 1 }];
			const table = buildDraftTable({
				fieldname: "logs",
				spec,
				required: true,
				proposed: rows,
				saved: verb === "update" ? SAVED() : [],
				verb,
			});
			const card = { draft: { verb, doctype: "T", values: { logs: rows } } };
			return { model: { verb, doctype: "T", fields: [], tables: [table] }, card };
		};
		const edit = (m) => {
			const [r] = logs(m).rows;
			r.name = "evil";
			r._draft_row = "9";
			r.hours = "5";
		};
		expect(run(make("update"), edit).values).toEqual({
			logs: [{ name: "tl-1", hours: 5 }, { _draft_row: 1 }],
		});
		expect(run(make("create"), edit).values).toEqual({ logs: [{ _draft_row: 0, hours: 5 }] });
	});
});

// The shared cases (draftCardContractCases.json): the server test turns each `values` into its
// `written`; here the form must produce that same `values` from the same sealed call.
describe("the shared values contract cases", () => {
	const COLUMNS = [
		F("item"),
		F("qty", "Float"),
		F("note"),
		F("so_detail", "Data", { read_only: 1 }),
	];
	const SPEC = { label: "Rows", child_doctype: "Row", columns: COLUMNS, extra_columns: [] };
	function build(c) {
		const update = c.verb === "update";
		const fields =
			"title" in c.sealed
				? [{ ...panelField(F("title"), c.sealed.title), proposed: true }]
				: [];
		const table = buildDraftTable({
			fieldname: "rows",
			spec: SPEC,
			required: !update,
			proposed: c.sealed.rows,
			saved: update ? c.savedRows || [] : [],
			verb: c.verb,
		});
		const card = { draft: { verb: c.verb, doctype: "T", values: c.sealed } };
		return { model: { verb: c.verb, doctype: "T", fields, tables: [table] }, card };
	}
	function replay(model, ops) {
		const t = model.tables[0];
		for (const o of ops) {
			if (o.op === "set") t.rows[o.row][o.cell] = o.value;
			else if (o.op === "remove") t.rows.splice(o.row, 1);
			else if (o.op === "insert")
				t.rows.splice(o.row, 0, Object.assign(blankRow(t), o.cells));
			else if (o.op === "append") t.rows.push(Object.assign(blankRow(t), o.cells));
			else throw new Error(`unknown op ${o.op}`);
		}
	}
	const both = CONTRACT.cases.filter((c) => c.side !== "server");

	it("covers every verb and the cases that matter, and marks the server-only ones", () => {
		expect(CONTRACT.cases.map((c) => c.id)).toEqual(
			expect.arrayContaining([
				"update-drafted-new-row-one-edited-cell",
				"update-drafted-new-row-removed",
				"update-person-added-row",
			])
		);
		expect(both.length).toBeGreaterThanOrEqual(11);
		for (const c of CONTRACT.cases.filter((c) => c.side)) expect(c.why).toBeTruthy();
	});
	for (const c of both) {
		it(`${c.id}: the form sends the payload the server test writes from`, () => {
			const { model, card } = build(c);
			const snap = snapshotDraftModel(model, card);
			replay(model, c.form);
			expect(draftCardValues(model, snap, card)).toEqual(c.values);
		});
	}
});

describe("writes that are not the person's edits", () => {
	const withRule = () =>
		createModel(
			{
				title: "Hello",
				logs: [
					{ activity: "A1", hours: 1 },
					{ activity: "A2", hours: 2 },
				],
			},
			{ spec: WITH_RULE }
		);
	const fill = (m, row) =>
		applyRuleValues(
			logs(m),
			{ activity: { [row.activity]: { is_fixed: 1 } } },
			{ activity: [row.activity] }
		);

	it("a rule-values fill on its own changes nothing", () => {
		const { values, model } = run(withRule, (m) => fill(m, logs(m).rows[0]));
		expect(logs(model).rows[0].__rule).toEqual({ is_fixed: 1 }); // the writer did run
		expect(values).toEqual({});
	});

	it("a link pick followed by the rule-values fill sends only the picked cell", () => {
		const { values, model } = run(withRule, (m) => {
			logs(m).rows[1].activity = "A7";
			fill(m, logs(m).rows[1]);
		});
		expect(logs(model).rows[1].__ruleSrc).toEqual({ activity: "A7" });
		expect(values).toEqual({ logs: [{ _draft_row: 0 }, { _draft_row: 1, activity: "A7" }] });
	});

	it("a main link pick followed by a fill sends only the picked field", () => {
		const { values } = run(
			() => createModel({ ...DRAFT, logs: ROWS() }, { spec: WITH_RULE }),
			(m) => {
				field(m, "customer").value = "C-9";
				fill(m, logs(m).rows[0]);
			}
		);
		expect(values).toEqual({ customer: "C-9" });
	});

	it("a snapshot taken after the fill is the same baseline as one taken before it", () => {
		const { model, card } = withRule();
		const before = snapshotDraftModel(model, card);
		fill(model, logs(model).rows[0]);
		const after = snapshotDraftModel(model, card);
		expect(draftCardValues(model, before, card)).toEqual({});
		expect(draftCardValues(model, after, card)).toEqual({});
		expect(after).toEqual(before);
	});
});

describe("draftCardDirty and robustness", () => {
	it("is true exactly when the payload would not be empty", () => {
		const built = createModel({ title: "Hello", logs: ROWS() });
		const snap = snapshotDraftModel(built.model, built.card);
		expect(draftCardDirty(built.model, snap, built.card)).toBe(false);
		field(built.model, "title").value = "Bye";
		expect(draftCardDirty(built.model, snap, built.card)).toBe(true);
		field(built.model, "title").value = "Hello";
		expect(draftCardDirty(built.model, snap, built.card)).toBe(false);
		logs(built.model).rows.splice(0, 1);
		expect(draftCardDirty(built.model, snap, built.card)).toBe(true);
	});

	it("a snapshot survives a JSON round trip (the model may be stored reactive)", () => {
		const built = createModel({ title: "Hello", logs: ROWS() });
		const snap = JSON.parse(JSON.stringify(snapshotDraftModel(built.model, built.card)));
		logs(built.model).rows[1].hours = "5";
		expect(draftCardValues(built.model, snap, built.card)).toEqual({
			logs: [{ _draft_row: 0 }, { _draft_row: 1, hours: 5 }, { _draft_row: 2 }],
		});
	});

	it("no model or no snapshot never throws: nothing, or everything filled", () => {
		expect(draftCardValues(null, null, null)).toEqual({});
		expect(draftCardDirty(undefined, undefined, undefined)).toBe(false);
		const built = createModel({ title: "Hello" });
		expect(draftCardValues(built.model, null, built.card)).toEqual({ title: "Hello" });
	});
});

describe("snapshotMatchesCard: is the baseline the card's own", () => {
	const ok = (r) => expect(r).toEqual({ ok: true, why: "" });
	const creating = () => createModel({ ...DRAFT, logs: ROWS() });
	const updating = () =>
		updateModel({
			title: "Hello",
			logs: [{ name: "tl-1", hours: 2 }, { name: "tl-2" }, { activity: "A9", hours: 9 }],
		});
	// the model after `edit`, snapshotted then: a late snapshot
	const late = (build, edit) => {
		const { model, card } = build();
		edit(model);
		return snapshotMatchesCard(snapshotDraftModel(model, card), card);
	};

	it("a snapshot of the untouched form is ok, create and update", () => {
		for (const build of [creating, updating]) {
			const { model, card } = build();
			ok(snapshotMatchesCard(snapshotDraftModel(model, card), card));
		}
	});

	it("every golden case's untouched form is ok (real models, drafted new rows included)", () => {
		for (const c of CONTRACT.cases.filter((c) => c.side !== "server")) {
			const update = c.verb === "update";
			const table = buildDraftTable({
				fieldname: "rows",
				spec: {
					label: "Rows",
					child_doctype: "Row",
					columns: [
						F("item"),
						F("qty", "Float"),
						F("note"),
						F("so_detail", "Data", { read_only: 1 }),
					],
					extra_columns: [],
				},
				required: !update,
				proposed: c.sealed.rows,
				saved: update ? c.savedRows || [] : [],
				verb: c.verb,
			});
			const card = { draft: { verb: c.verb, doctype: "T", values: c.sealed } };
			const model = { verb: c.verb, doctype: "T", fields: [], tables: [table] };
			expect([c.id, snapshotMatchesCard(snapshotDraftModel(model, card), card).ok]).toEqual([
				c.id,
				true,
			]);
		}
	});

	it("after an edit to a main field it is not ok and names the field", () => {
		const r = late(creating, (m) => (field(m, "title").value = "Changed"));
		expect(r.ok).toBe(false);
		expect(r.why).toContain("title");
	});

	it("after an edit to a create row cell it is not ok and names the table, row and cell", () => {
		const r = late(creating, (m) => (logs(m).rows[1].hours = "9"));
		expect(r.ok).toBe(false);
		expect(r.why).toContain("logs row 2 hours");
	});

	it("after an edit to a saved row cell of an update draft it is not ok", () => {
		const r = late(updating, (m) => (logs(m).rows[0].hours = "8"));
		expect(r.ok).toBe(false);
		expect(r.why).toContain("logs row tl-1 hours");
	});

	it("rows the draft names in another order than the snapshot's are not ok", () => {
		const r = late(updating, (m) => {
			const rows = logs(m).rows;
			[rows[0], rows[1]] = [rows[1], rows[0]];
		});
		expect(r.ok).toBe(false);
		expect(r.why).toContain("order");
	});

	it("a snapshot taken before the drafted values were applied is not ok", () => {
		// create: the form is still blank when the card already holds the draft
		const { card } = creating();
		const blankModel = createModel({}).model;
		blankModel.fields = MAIN.map((m) => panelField(m, ""));
		const r = snapshotMatchesCard(snapshotDraftModel(blankModel, card), card);
		expect(r.ok).toBe(false);
		// update: only the stored rows are in the grid (the draft's overlay has not run)
		const upd = updating();
		const stored = updateModel({ title: "Hello" });
		const early = snapshotMatchesCard(snapshotDraftModel(stored.model, upd.card), upd.card);
		expect(early.ok).toBe(false);
		expect(early.why).toContain("logs");
	});

	it("a default the form filled for a field or cell the draft does not hold is not a failure", () => {
		const { model, card } = creating();
		model.fields.push(panelField(F("priority"), "Medium"));
		logs(model).rows[0].note = "form default";
		ok(snapshotMatchesCard(snapshotDraftModel(model, card), card));
	});

	it("a control's own formatting is not a failure; an object value is compared as JSON text", () => {
		const { model, card } = creating();
		card.draft.values.qty = 3; // the form shows "3"
		card.draft.values.at = "2026-11-01T09:30:00"; // the form shows the minutes only
		card.draft.values.logs[2].hours = 3.0;
		model.fields.push(panelField(F("meta", "JSON"), '{"a":1}'));
		card.draft.values.meta = { a: 1 };
		ok(snapshotMatchesCard(snapshotDraftModel(model, card), card));
	});

	it("never throws: an unreadable snapshot or card is simply not ok", () => {
		const { model, card } = creating();
		const snap = snapshotDraftModel(model, card);
		const hostile = {
			get draft() {
				throw new Error("boom");
			},
		};
		const inputs = [
			[null, card],
			[undefined, undefined],
			[snap, null],
			[snap, {}],
			[snap, { draft: { values: [] } }],
			[{ fields: 1, tables: 2 }, card],
			[{ fields: {}, tables: { logs: { keys: "x", rows: 3 } } }, card],
			[snap, hostile],
		];
		for (const [s, c] of inputs) {
			const r = snapshotMatchesCard(s, c);
			expect(r.ok).toBe(false);
			expect(typeof r.why).toBe("string");
		}
	});
});
