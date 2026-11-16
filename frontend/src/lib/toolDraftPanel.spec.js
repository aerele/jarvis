// The pure parts of the desktop's edit form and preview for a server draft card
// (lib/toolDraftPanel.js). Models are built with the real panelField / buildDraftTable, as
// ChatView's buildDraftModel builds them; expectations are written out by hand.
import { describe, expect, it, vi } from "vitest";
import { blankRow, buildDraftTable } from "./draftTables";
import { panelField } from "./docFields";
import { draftCardDirty, draftCardValues } from "./draftCardValues";
import { draftIsDirty } from "./draftSave";
import {
	CANT_EDIT_TEXT,
	computedFromCard,
	focusFirstMark,
	prepareToolModel,
	resolveDraftField,
	toolPrimaryLabel,
} from "./toolDraftPanel";

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

	// Break: the typed path changes: it must keep resolving by label, whatever keys the model
	// wrote (a `fieldname` key in a typed field is ignored).
	it("not exact (a typed action): the label's resolver decides, a fieldname key is ignored", () => {
		const byLabel = vi.fn(() => META[3]);
		expect(resolveDraftField({ label: "Title" }, META, byLabel, false)).toBe(META[3]);
		expect(byLabel).toHaveBeenCalledWith("Title");
		expect(resolveDraftField({ label: "x", fieldname: "date" }, META, byLabel, false)).toBe(
			META[3]
		);
		expect(byLabel).toHaveBeenCalledTimes(2);
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
		expect(CANT_EDIT_TEXT).toBe(
			"This draft can't be edited here. Use Create on the card, or ask Jarvis to change it."
		);
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

describe("toolPrimaryLabel", () => {
	it("is the card's own word", () => {
		expect([
			toolPrimaryLabel({ verb: "create" }),
			toolPrimaryLabel({ verb: "update" }),
		]).toEqual(["Create", "Save changes"]);
	});
});

describe("focusFirstMark: the first mark of a refused save", () => {
	const tree = (html) => {
		const root = document.createElement("div");
		root.innerHTML = html;
		document.body.appendChild(root);
		root.querySelectorAll("*").forEach((el) => (el.scrollIntoView = vi.fn()));
		return root;
	};
	// Break: the first mark is not scrolled to or focused (the person looks at a form with no
	// sign of what to fix), or a later mark is chosen over an earlier one.
	it("scrolls to and focuses the control of the first mark in document order", () => {
		const root = tree(`
			<div class="jv-draft-fld"><input id="a"></div>
			<div class="jv-draft-fld invalid"><input id="b"></div>
			<div class="jv-rowform-missing"><select id="c"></select></div>
			<div class="jv-draft-fld missing"><input id="d"></div>`);
		const mark = focusFirstMark(root);
		expect(mark.querySelector("#b")).not.toBeNull();
		expect(mark.scrollIntoView).toHaveBeenCalledTimes(1);
		expect(document.activeElement.id).toBe("b");
		root.remove();
	});

	it("finds a marked row's field", () => {
		const root = tree(`<div class="jv-rowform-missing"><textarea id="r"></textarea></div>`);
		focusFirstMark(root);
		expect(document.activeElement.id).toBe("r");
		root.remove();
	});

	// Break: a refusal with no field mark (busy, a whole table refused) leaves the person looking at
	// the top of a long form with the reason out of sight below it.
	it("without a mark it scrolls the error banner into view and takes no focus", () => {
		const root = tree(
			`<div class="jv-draft-body"><div class="jv-draft-fld"><input id="x"></div><div role="alert" id="e"></div></div>`
		);
		const before = document.activeElement;
		expect(focusFirstMark(root)).toBeNull();
		expect(root.querySelector("#e").scrollIntoView).toHaveBeenCalledTimes(1);
		expect(document.activeElement).toBe(before);
		root.remove();
	});

	it("does nothing without a mark, a banner or a root", () => {
		const root = tree(`<div class="jv-draft-fld"><input id="x"></div>`);
		expect(focusFirstMark(root)).toBeNull();
		expect(focusFirstMark(null)).toBeNull();
		root.remove();
	});
});
