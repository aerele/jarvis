// The pure parts of the desktop's edit form and preview for a server draft card
// (lib/toolDraftPanel.js). Models are built with the real panelField / buildDraftTable, as
// ChatView's buildDraftModel builds them; expectations are written out by hand.
import { describe, expect, it, vi } from "vitest";
import { blankRow, buildDraftTable } from "./draftTables";
import { panelField } from "./docFields";
import { draftCardDirty, draftCardValues } from "./draftCardValues";
import { draftIsDirty } from "./draftSave";
import { markMissing } from "./docFields";
import { markMissingRows } from "./draftTables";
import {
	CANT_EDIT_TEXT,
	cardDatesIntoPreview,
	computedFromCard,
	fillCalculatedCells,
	firstServerMark,
	focusFirstMark,
	prepareToolModel,
	resolveDraftField,
	settleCalculatedCells,
} from "./toolDraftPanel";
import { lineItemSummary } from "./actionSummary";
import { timesheetCard, timesheetModel } from "./timesheetDraftFixture";

const F = (fieldname, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});

describe("resolveDraftField: fieldname first, never fuzzy", () => {
	// Two fields whose labels look like each other's names: a label match would swap them.
	const META = [
		F("posting_date", "Date", { label: "Date" }),
		F("date", "Date", { label: "Posting Date" }),
		F("items", "Table"),
		F("title"),
	];
	const never = vi.fn();
	// Break: a drafted key lands on another field (the one its label resembles), a key the form
	// does not hold is guessed at, or a table field takes a main value.
	const table = [
		[
			"a fieldname is that field",
			{ fieldname: "posting_date", label: "posting_date" },
			"posting_date",
		],
		["a similar label does not win", { fieldname: "date", label: "date" }, "date"],
		[
			"a fieldname the form does not hold is left out",
			{ fieldname: "dat", label: "dat" },
			null,
		],
		["a table field is not a main field", { fieldname: "items", label: "items" }, null],
		["an entry with no fieldname is left out", { label: "title" }, null],
	];
	for (const [name, entry, want] of table)
		it(`exact: ${name}`, () => {
			const m = resolveDraftField(entry, META, never, true);
			expect(m ? m.fieldname : null).toBe(want);
			expect(never).not.toHaveBeenCalled();
		});
});

// A create draft the host would build: two main fields, a three-row table.
const LOGS = {
	label: "Logs",
	child_doctype: "Log",
	columns: [F("activity"), F("hours", "Float"), F("amount", "Currency", { read_only: 1 })],
	extra_columns: [],
	form_fields: [],
};
const CARD = () => ({
	kind: "create",
	draft: {
		verb: "create",
		doctype: "Timesheet",
		values: {
			title: "Hello",
			qty: "3",
			logs: [
				{ activity: "A1", hours: 1 },
				{ activity: "A2", hours: 2 },
				{ activity: "A3", hours: 3 },
			],
		},
	},
	tables: [
		{
			label: "Logs",
			count: 3,
			columns: ["Activity", "Hours", "Amount"],
			fieldnames: ["activity", "hours", "amount"],
			rows: [
				{ cells: ["A1", "1", "15.00"] },
				{ cells: ["A2", "2", "30.00"] },
				{ cells: ["A3", "3", ""] },
			],
		},
	],
});
function built(card = CARD()) {
	const v = card.draft.values;
	const model = {
		verb: "create",
		doctype: "Timesheet",
		fields: [
			{ ...panelField(F("title"), v.title), proposed: true },
			{ ...panelField(F("qty", "Float"), v.qty), proposed: true },
		],
		tables: [
			buildDraftTable({
				fieldname: "logs",
				spec: LOGS,
				required: true,
				proposed: v.logs,
				verb: "create",
			}),
		],
	};
	return { model, card };
}
const ident = { token: "tok-1", conversation: "c1" };

describe("prepareToolModel: the stamp, the snapshot and the guard", () => {
	it("stamps the form of one card and leaves Save on for a healthy form", () => {
		const { model, card } = built();
		const { guard } = prepareToolModel(model, { ...ident, card });
		expect(guard).toEqual({ ok: true, why: "" });
		expect([
			model.tool,
			model.token,
			model.conversation,
			model.actionKey,
			model.saveBlocked,
		]).toEqual([true, "tok-1", "c1", "tool:tok-1", ""]);
	});

	// Break: the snapshot is taken after the person's first change (or after a row was added), so
	// the diff reads the change as the baseline and Save sends {} or the wrong rows.
	it("a model that no longer matches its card is blocked, with the card's line", () => {
		const { model, card } = built();
		model.tables[0].rows.push(blankRow(model.tables[0])); // a row appeared before the snapshot
		const { guard } = prepareToolModel(model, { ...ident, card });
		expect(guard.ok).toBe(false);
		expect(model.saveBlocked).toBe(CANT_EDIT_TEXT);
	});

	// Break: the form keeps a live reference to the list item's card, so a later change to the
	// item changes what counts as "unchanged".
	it("keeps a copy of the card", () => {
		const { model, card } = built();
		prepareToolModel(model, { ...ident, card });
		card.draft.values.title = "Changed later";
		expect(model.card.draft.values.title).toBe("Hello");
	});

	// Break: the snapshot is not the form's own baseline: an edit after it must show in the payload.
	it("the snapshot is the baseline of the diff: untouched sends nothing, an edit is sent", () => {
		const { model, card } = built();
		prepareToolModel(model, { ...ident, card });
		expect(draftCardValues(model, model.snapshot, model.card)).toEqual({});
		model.fields[0].value = "Edited";
		model.tables[0].rows[1].hours = "9";
		expect(draftCardValues(model, model.snapshot, model.card)).toEqual({
			title: "Edited",
			logs: [{ _draft_row: 0 }, { _draft_row: 1, hours: 9 }, { _draft_row: 2 }],
		});
	});

	// Break: a dirty form closes on a backdrop click (the person's edits lost), or a clean one stays.
	it("draftIsDirty reads a server draft's form through the snapshot", () => {
		const { model, card } = built();
		prepareToolModel(model, { ...ident, card });
		expect(draftIsDirty(model)).toBe(false);
		model.fields[0].value = "Edited";
		expect(draftIsDirty(model)).toBe(true);
		expect(draftCardDirty(model, model.snapshot, model.card)).toBe(true);
		model.fields[0].value = "Hello";
		expect(draftIsDirty(model)).toBe(false);
	});
});

describe("computedFromCard: Preview shows the card's own numbers", () => {
	const modelFor = (card) => built(card).model;
	// Break: a calculated cell is taken from the wrong row or column, or guessed.
	it("maps each card cell to its row and its column fieldname", () => {
		const card = CARD();
		expect(computedFromCard(modelFor(card), card)).toEqual({
			logs: [
				{ activity: "A1", hours: "1", amount: "15.00" },
				{ activity: "A2", hours: "2", amount: "30.00" },
				{ activity: "A3", hours: "3" },
			],
		});
	});

	// Break: a card table is matched to the wrong model table (or to two), or rows that do not
	// line up are drawn anyway: Preview would then disagree with the card.
	const doubt = [
		["no tables on the card (an update card)", (c) => delete c.tables],
		["a different label", (c) => (c.tables[0].label = "Other")],
		["two card tables with that label", (c) => c.tables.push({ ...c.tables[0] })],
		["the trial added a row (count differs)", (c) => (c.tables[0].count = 4)],
		["no column fieldnames", (c) => delete c.tables[0].fieldnames],
	];
	for (const [name, edit] of doubt)
		it(`leaves Preview blank when in doubt: ${name}`, () => {
			const card = CARD();
			const model = modelFor(card);
			edit(card);
			expect(computedFromCard(model, card)).toEqual({});
		});

	// Break: two model tables with one label are told apart by guessing, so each could draw the
	// other's numbers.
	it("leaves Preview blank when two model tables share the label", () => {
		const card = CARD();
		const model = modelFor(card);
		model.tables.push({ ...model.tables[0], fieldname: "logs2" });
		expect(computedFromCard(model, card)).toEqual({});
	});

	it("a card that shows fewer rows than it counts fills the rows it shows", () => {
		const card = CARD();
		card.tables[0].rows = card.tables[0].rows.slice(0, 2);
		const out = computedFromCard(modelFor(card), card);
		expect(out.logs.map((r) => (r ? r.amount : undefined))).toEqual([
			"15.00",
			"30.00",
			undefined,
		]);
	});

	it("never throws on a hostile card", () => {
		for (const card of [
			null,
			undefined,
			{},
			{ tables: "x" },
			{ tables: [null, 5, { rows: 3 }] },
		])
			expect(computedFromCard(built().model, card)).toEqual({});
		expect(computedFromCard(null, CARD())).toEqual({});
	});
});

// ---- cells the card shows and the draft left blank: a real-shaped Timesheet draft ----------------

describe("the card's own numbers in the Preview (a Timesheet: Hrs and Is Billable are not read-only)", () => {
	const previewed = () => {
		const card = timesheetCard();
		const model = timesheetModel();
		prepareToolModel(model, { ...ident, card, fill: { readOnly: false } });
		model.computed = computedFromCard(model, card);
		cardDatesIntoPreview(model, card);
		return { model, card };
	};
	// Break: the Preview shows a blank Hrs or Is Billable where the card shows 3 / 1.50 / 2 and
	// "No" (the cause: a calculated cell was filled only in a read-only column, and these are not).
	it("shows, for every column the card shows, the card's cell", () => {
		const { model, card } = previewed();
		const view = lineItemSummary(model.tables[0], model.computed.time_logs);
		const shown = (fieldname) => {
			const i = model.tables[0].columns.findIndex((c) => c.fieldname === fieldname);
			return view.rows.map((r) => r.cells[i]);
		};
		const cardCol = (fieldname) => {
			const i = card.tables[0].fieldnames.indexOf(fieldname);
			return card.tables[0].rows.map((r) => r.cells[i]);
		};
		for (const fieldname of card.tables[0].fieldnames)
			expect([fieldname, shown(fieldname)]).toEqual([fieldname, cardCol(fieldname)]);
	});

	// Break: the Preview shows the control's ISO text ("2026-12-08T09:30") where the card shows the
	// person's date format.
	it("shows dates as the card writes them, in the table and in the fields", () => {
		const { model } = previewed();
		expect(model.tables[0].rows.map((r) => r.from_time)).toEqual([
			"08-12-2026 09:30:00",
			"08-12-2026 13:30:00",
			"08-12-2026 15:00:00",
		]);
		expect(model.fields.find((f) => f.fieldname === "start_date").value).toBe("08-12-2026");
	});

	// Break: a date is replaced from a label that is not unique (it lands on another field), or from
	// a card that does not hold it; or an update's change is shown raw.
	const dated = [
		[
			"two fields with the label",
			(m) => m.fields.push({ ...m.fields[3], fieldname: "other_date" }),
			"2026-12-08",
		],
		["the card has no such row", (m, c) => (c.rows = c.rows.slice(0, 3)), "2026-12-08"],
		["a blank value is not replaced", (m) => (m.fields[3].value = ""), ""],
	];
	for (const [name, edit, want] of dated)
		it(`a main date keeps its own value when ${name}`, () => {
			const card = timesheetCard();
			const model = timesheetModel();
			edit(model, card);
			cardDatesIntoPreview(model, card);
			expect(model.fields.find((f) => f.fieldname === "start_date").value).toBe(want);
		});

	it("an update's changed date shows the card's from and to", () => {
		const card = { diff: [{ label: "Start Date", from: "01-12-2026", to: "08-12-2026" }] };
		const model = timesheetModel();
		Object.assign(model.fields[3], { changed: true, orig: "2026-12-01" });
		cardDatesIntoPreview(model, card);
		expect([model.fields[3].orig, model.fields[3].value]).toEqual([
			"01-12-2026",
			"08-12-2026",
		]);
	});
});

describe("fillCalculatedCells: what goes into a blank cell, and what never does", () => {
	// Break: a value the person drafted is overwritten by the card's text, or a blank cell the card
	// also shows blank gets text.
	it("fills only blank cells, from the card's cell of that row and column", () => {
		const card = timesheetCard();
		const model = timesheetModel();
		model.tables[0].rows[1].hours = "9"; // drafted
		fillCalculatedCells(model, card, { readOnly: true });
		expect(model.tables[0].rows.map((r) => [r.hours, r.is_billable])).toEqual([
			["3", "No"],
			["9", "No"],
			["2", "No"],
		]);
	});

	// Break: a column a typed control cannot show (a date) or a number it cannot parse is filled
	// with the card's formatted text; or thousands separators reach a number input.
	const kinds = [
		["a date column is left for the control", "from_time", "", "08-12-2026 09:30:00", ""],
		["a thousands comma is ambiguous: blank", "hours", "", "1,500.50", ""],
		["a bare thousands-looking number is ambiguous: blank", "hours", "", "1,500", ""],
		["a percent sign is not a number", "hours", "", "50%", ""],
		["a number is kept as the card wrote it", "hours", "", "1.50", "1.50"],
		["a whole number", "hours", "", "3", "3"],
		["US thousands with decimals: blank", "hours", "", "1,234.50", ""],
		["a comma decimal is ambiguous: blank", "hours", "", "1,50", ""],
		["a dot-thousands comma-decimal is ambiguous: blank", "hours", "", "1.500,00", ""],
		["a space-thousands comma-decimal is ambiguous: blank", "hours", "", "1 500,00", ""],
		["text that is not a number: blank", "hours", "", "abc", ""],
		["an empty card cell: blank", "hours", "", "", ""],
	];
	for (const [name, fieldname, own, text, want] of kinds)
		it(name, () => {
			const card = timesheetCard();
			const model = timesheetModel();
			const i = card.tables[0].fieldnames.indexOf(fieldname);
			card.tables[0].rows[0].cells[i] = text;
			model.tables[0].rows[0][fieldname] = own;
			fillCalculatedCells(model, card, { readOnly: true });
			expect(model.tables[0].rows[0][fieldname]).toBe(want);
		});

	// Break: the Preview shows a number the card does not (a comma-decimal "1,50" read as 150, or
	// left blank) because it was filled for the editable form with the form's strict parse.
	it("verbatim (the Preview) puts the card's own number text in a number cell", () => {
		const card = timesheetCard();
		const model = timesheetModel();
		card.tables[0].rows[0].cells[card.tables[0].fieldnames.indexOf("hours")] = "1.500,00";
		fillCalculatedCells(model, card, { readOnly: false, verbatim: true });
		expect(model.tables[0].rows[0].hours).toBe("1.500,00");
	});

	// Break: the Preview's own rule for calculated read-only columns is pre-empted (the cell reads as
	// an ordinary value and loses its calculated look), or the form leaves a read-only column blank.
	it("a read-only column is filled for the form and left to the Preview's own rule for the Preview", () => {
		const form = built();
		fillCalculatedCells(form.model, form.card, { readOnly: true });
		const preview = built();
		fillCalculatedCells(preview.model, preview.card, { readOnly: false });
		expect([
			form.model.tables[0].rows.map((r) => r.amount),
			preview.model.tables[0].rows.map((r) => r.amount),
		]).toEqual([
			["15.00", "30.00", ""],
			["", "", ""],
		]);
	});
});

describe("the form's filled cells follow the person's edits and are never sent", () => {
	const open = () => {
		const card = timesheetCard();
		const model = timesheetModel();
		prepareToolModel(model, { ...ident, card, fill: { readOnly: true } });
		const send = () => draftCardValues(model, model.snapshot, model.card);
		const rows = model.tables[0].rows;
		return { model, card, rows, send };
	};
	const hours = (rows) => rows.map((r) => r.hours);

	// Break: the form shows Hrs blank (an input showing 0) where the card shows 3 / 1.50 / 2, or
	// opening it already counts as an edit (Save would send the card's own numbers).
	it("opens showing the card's numbers, and as untouched", () => {
		const { model, rows, send } = open();
		expect(hours(rows)).toEqual(["3", "1.50", "2"]);
		expect(send()).toEqual({});
		expect(draftIsDirty(model)).toBe(false);
	});

	// Break: editing a cell of a row sends a calculated cell blank (clearing Hrs on save) or leaves a
	// stale number beside the edit; the other rows change; or an edit sends more than the edit.
	it("an edit blanks the filled cells of that row only, and sends the edit alone", () => {
		const { model, rows, send } = open();
		rows[0].project = "PROJ-0002";
		settleCalculatedCells(model);
		expect(hours(rows)).toEqual(["", "1.50", "2"]);
		expect(send()).toEqual({
			time_logs: [
				{ _draft_row: 0, project: "PROJ-0002" },
				{ _draft_row: 1 },
				{ _draft_row: 2 },
			],
		});
	});

	// Break: the number does not come back when the edit is undone, so the form keeps showing a
	// blank where the draft is again as drafted.
	it("undoing the edit gives the card's number back", () => {
		const { model, rows, send } = open();
		rows[0].project = "PROJ-0002";
		settleCalculatedCells(model);
		rows[0].project = "PROJ-0001";
		settleCalculatedCells(model);
		expect(hours(rows)).toEqual(["3", "1.50", "2"]);
		expect(send()).toEqual({});
	});

	// Break: a number the person types into a filled cell is overwritten by the card's, or dropped
	// from the save, or the cell keeps being managed after it (blanked by the next edit).
	it("a value the person types into a filled cell is theirs: kept, sent, never blanked", () => {
		const { model, rows, send } = open();
		rows[0].hours = 5;
		settleCalculatedCells(model);
		rows[0].project = "PROJ-0002";
		settleCalculatedCells(model);
		expect(rows[0].hours).toBe(5);
		expect(send().time_logs[0]).toEqual({ _draft_row: 0, project: "PROJ-0002", hours: 5 });
	});

	// Break: a text edit blanks the row's Hrs (it looks like data loss), a feeding edit (a time, a link)
	// stops blanking, undo stops restoring, or a text edit after a feeding edit un-blanks the row.
	// `steps` run in order on row 2 with a settle after each; `hrs` is row 2's Hrs after each.
	const stepsTable = [
		["description only", [{ description: "Changed" }], ["1.50"]],
		["a time (from_time)", [{ from_time: "2026-12-08 14:00:00" }], [""]],
		["a link (activity_type)", [{ activity_type: "Planning" }], [""]],
		[
			"a feeding edit undone",
			[{ activity_type: "Planning" }, { activity_type: "" }],
			["", "1.50"],
		],
		[
			"a text edit after a feeding edit",
			[{ activity_type: "Planning" }, { description: "Changed" }],
			["", ""],
		],
		[
			"the feeding edit undone while a text edit stays",
			[{ activity_type: "Planning" }, { description: "Changed" }, { activity_type: "" }],
			["", "", "1.50"],
		],
	];
	for (const [name, steps, want] of stepsTable)
		it(`row 2 Hrs after: ${name}`, () => {
			const { model, rows } = open();
			const seen = steps.map((edit) => {
				Object.assign(rows[1], edit);
				settleCalculatedCells(model);
				return rows[1].hours;
			});
			expect(seen).toEqual(want);
			expect([hours(rows)[0], hours(rows)[2]]).toEqual(["3", "2"]);
		});

	// Break: a filled cell the person did not type into is sent after a text-only edit (Hrs from the
	// card goes to the server as if typed), or the snapshot guard rejects the opened form.
	it("a description-only edit sends the identity and the description, and the form opens clean", () => {
		const { model, rows, send } = open();
		rows[1].description = "Changed";
		settleCalculatedCells(model);
		expect(send()).toEqual({
			time_logs: [
				{ _draft_row: 0 },
				{ _draft_row: 1, description: "Changed" },
				{ _draft_row: 2 },
			],
		});
		expect(rows[1].hours).toBe("1.50");
	});

	it("settling again changes nothing, and a form with nothing filled is left alone", () => {
		const { model, rows } = open();
		rows[0].project = "PROJ-0002";
		expect([settleCalculatedCells(model), settleCalculatedCells(model)]).toEqual([
			true,
			false,
		]);
		expect(settleCalculatedCells({ tables: [], calc: [] })).toBe(false);
		expect(settleCalculatedCells(null)).toBe(false);
	});

	// Break: a row removed or reordered breaks the settle (it follows rows by position, not by key).
	it("follows a row by its key when rows are removed or inserted above it", () => {
		const { model, rows, send } = open();
		rows.splice(0, 1);
		rows[0].project = "PROJ-0002"; // the former row 2
		settleCalculatedCells(model);
		expect(hours(rows)).toEqual(["", "2"]);
		expect(send().time_logs[0]).toEqual({ _draft_row: 1, project: "PROJ-0002" });
	});
});

// ---- the server's first mark of a refused save ---------------------------------------------------

describe("firstServerMark: where the person is sent after a refused save", () => {
	const marked = (fields, edit = () => {}) => {
		const { model } = built();
		model.tables[0].rows.forEach((r) => (r.hours = r.hours || "1"));
		edit(model);
		markMissing(model, fields, []);
		markMissingRows(model, fields);
		return [firstServerMark(model, fields), model];
	};
	// Break: a field that is merely required and empty is where the person is sent; a row is
	// preferred to a main field; or a table-level refusal points at a field. `want` is given the
	// model, for the row key minted at build time.
	const table = [
		[
			"a main field the server named",
			[{ fieldname: "qty", message: "Bad", invalid: true }],
			() => {},
			() => ({ kind: "field", index: 1 }),
		],
		[
			"the first of several main fields, in the form's order, not in the server's",
			[
				{ fieldname: "qty", message: "x" },
				{ fieldname: "title", message: "x" },
			],
			() => {},
			() => ({ kind: "field", index: 0 }),
		],
		[
			"a main field is first even when a row is marked too",
			[
				{ fieldname: "hours", parentfield: "logs", idx: 1, message: "x" },
				{ fieldname: "qty", message: "x" },
			],
			() => {},
			() => ({ kind: "field", index: 1 }),
		],
		[
			"a row the server named (the second row it sent)",
			[{ fieldname: "hours", parentfield: "logs", idx: 2, message: "x" }],
			() => {},
			(m) => ({
				kind: "row",
				tableIndex: 0,
				key: m.tables[0].rows[1].__key,
				fieldname: "hours",
			}),
		],
		[
			"nothing the server named: a required, empty field is not a mark",
			[],
			(m) => (m.fields[0].value = ""),
			() => null,
		],
		[
			"a whole table refused (no field, no row)",
			[{ fieldname: "logs", message: "x" }],
			() => {},
			() => null,
		],
	];
	for (const [name, fields, edit, want] of table)
		it(name, () => {
			const [got, model] = marked(fields, edit);
			expect(got).toEqual(want(model));
		});
});

describe("focusFirstMark: the server's first mark is scrolled to and focused", () => {
	const tree = (html) => {
		const root = document.createElement("div");
		root.innerHTML = html;
		document.body.appendChild(root);
		root.querySelectorAll("*").forEach((el) => (el.scrollIntoView = vi.fn()));
		return root;
	};
	// A form as DraftEditForm draws it: a required empty field comes BEFORE the marked one.
	const FORM = `<div class="jv-draft-body">
		<div class="jv-draft-fld missing"><input id="required-empty"></div>
		<div class="jv-draft-fld invalid"><input id="marked"></div>
		<div id="jv-rowform-0-7"><div id="jv-rowform-0-7-hours-wrap"><input id="jv-rowform-0-7-hours"></div></div>
		<div role="alert" id="banner"></div></div>`;
	// Break: the focus goes to the first field that LOOKS marked (a required empty one) instead of the
	// field the server named; or the mark is not scrolled to.
	it("a main field: the one at that position, not the first marked-looking one", () => {
		const root = tree(FORM);
		const el = focusFirstMark(root, { kind: "field", index: 1 });
		expect(document.activeElement.id).toBe("marked");
		expect(el.scrollIntoView).toHaveBeenCalledTimes(1);
		root.remove();
	});

	// Break: a marked row's field is not found by the id its form gives it, or the row's block is
	// not the fallback.
	it("a row: the control of the named field, else the row's block", () => {
		const root = tree(FORM);
		focusFirstMark(root, { kind: "row", tableIndex: 0, key: 7, fieldname: "hours" });
		expect(document.activeElement.id).toBe("jv-rowform-0-7-hours");
		root.remove();
		const gone = tree(`<div id="jv-rowform-0-7" tabindex="-1"></div>`);
		expect(
			focusFirstMark(gone, { kind: "row", tableIndex: 0, key: 7, fieldname: "x" }).id
		).toBe("jv-rowform-0-7");
		gone.remove();
	});

	// Break: a row flagged by its message alone (no field) puts focus in its first input; a link field
	// opens its suggestion menu on focus and covers the row.
	it("a row flagged without a field takes focus on its block, not on its first input", () => {
		const root = tree(
			`<section id="jv-rowform-0-7" tabindex="-1"><input id="jv-rowform-0-7-activity"></section>`
		);
		focusFirstMark(root, { kind: "row", tableIndex: 0, key: 7, fieldname: "" });
		expect(document.activeElement.id).toBe("jv-rowform-0-7");
		root.remove();
	});

	// Break: a refusal with no mark (busy, a whole table refused) leaves the reason out of sight below
	// a long form, or focus jumps to a field because it is empty.
	it("with no mark it scrolls the error banner into view and takes no focus", () => {
		const root = tree(FORM);
		const before = document.activeElement;
		expect(focusFirstMark(root, null)).toBeNull();
		expect(root.querySelector("#banner").scrollIntoView).toHaveBeenCalledTimes(1);
		expect(document.activeElement).toBe(before);
		root.remove();
	});

	it("does nothing without a root, and falls back to the banner when the mark is not in the DOM", () => {
		expect(focusFirstMark(null, { kind: "field", index: 0 })).toBeNull();
		const root = tree(`<div class="jv-draft-body"><div role="alert" id="banner"></div></div>`);
		expect(focusFirstMark(root, { kind: "field", index: 3 })).toBeNull();
		expect(root.querySelector("#banner").scrollIntoView).toHaveBeenCalledTimes(1);
		root.remove();
	});
});

// ---- row marks line up with the rows a real payload sends ----------------------------------------

describe("a server row mark lands on the row it was sent from (against the real payload)", () => {
	// The server counts the rows of the payload (`idx` is 1-based in `values[table]`). The form
	// marks `sentRows(...)[idx - 1]`: if the two lists ever differ, the mark lands on the wrong row.
	// Break: a blank row, a removed row or an untouched kept row shifts the count between the payload
	// and the marks.
	const setup = () => {
		const card = CARD();
		const model = built(card).model;
		prepareToolModel(model, { ...ident, card });
		return { model, card, t: model.tables[0] };
	};
	const markedRow = (model, idx) => {
		markMissingRows(model, [{ fieldname: "hours", parentfield: "logs", idx, message: "x" }]);
		return model.tables[0].rows.find((r) => r.__missing);
	};
	const cases = [
		[
			"a blank row left in the middle is not a sent row",
			(m, t) => {
				t.rows.splice(1, 0, blankRow(t));
				t.rows[2].activity = "edited-2";
			},
			3, // payload rows: kept 0, kept 2 (edited), kept 3 -> idx 2 is the edited row
			2,
			"edited-2",
		],
		[
			"a removed first row shifts every later one up",
			(m, t) => {
				t.rows.splice(0, 1);
				t.rows[0].activity = "edited-1";
			},
			2,
			1,
			"edited-1",
		],
		[
			"an untouched kept row is a sent row (identity only)",
			(m, t) => {
				t.rows[2].activity = "edited-2";
			},
			3,
			3,
			"edited-2",
		],
		[
			"a row added at the end carries all its cells and is the last row",
			(m, t) => {
				const r = blankRow(t);
				Object.assign(r, { activity: "added", hours: 4 });
				t.rows.push(r);
			},
			4,
			4,
			"added",
		],
	];
	for (const [name, edit, sent, idx, activity] of cases)
		it(name, () => {
			const { model, card, t } = setup();
			edit(model, t);
			const payload = draftCardValues(model, model.snapshot, card).logs;
			expect(payload).toHaveLength(sent);
			const row = markedRow(model, idx);
			expect(row.activity).toBe(activity);
			// and the payload row at that index is the one with that activity (or its identity)
			const sentRow = payload[idx - 1];
			expect(sentRow.activity === undefined || sentRow.activity === activity).toBe(true);
		});
});
