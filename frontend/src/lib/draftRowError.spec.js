import { describe, expect, it } from "vitest";
import { hasFlaggedRow, markRowFromMessage, rowNamedBy } from "./draftRowError.js";
import { draftCardDirty, draftCardValues, snapshotDraftModel } from "./draftCardValues.js";
import { blankRow, sentRows } from "./draftTables.js";
import { timesheetCard, timesheetModel } from "./timesheetDraftFixture.js";

const cols = (...labels) =>
	labels.map((label) => ({ fieldname: label.toLowerCase().replace(/ /g, "_"), label }));
const row = (key, cells = { a: "x" }) => ({ __key: key, ...cells });
// a form model: tables in the order given, each with its columns and rows
const model = (tables, verb = "create") => ({
	verb,
	tables: tables.map(([fieldname, label, labels, rows, formLabels = []]) => ({
		fieldname,
		label,
		columns: cols(...labels),
		formFields: cols(...formLabels),
		rows,
	})),
});
// three rows with a value in `field` (a row with no value in a column is a blank row the save never sends)
const rows3 = (field = "from_time") =>
	["k1", "k2", "k3"].map((k, i) => row(k, { [field]: String(i + 1) }));
const OVERLAP = "Row 3: From Time and To Time of TS-2026-00008 is overlapping with TS-2026-00007";

describe("rowNamedBy: which row of which table a message names", () => {
	const one = () => model([["time_logs", "Time Logs", ["From Time", "Hrs"], rows3()]]);
	const two = () =>
		model([
			[
				"items",
				"Items",
				["Item Code", "Rate", "Description"],
				rows3("item_code"),
				["Warehouse"],
			],
			["taxes", "Taxes", ["Account", "Rate Type", "Description"], rows3("account")],
		]);
	// Break: a leading "Row N:" is not read (or read in the middle of a sentence), the wrong row of
	// the table is marked (an off-by-one against the rows the save sent), or a message that names a
	// table that cannot be told apart from its neighbour marks one of them anyway.
	it.each([
		["the only table, the server's own wording", one(), OVERLAP, [0, "k3"]],
		["a hash and spaces", one(), "  row #1 : Hours must be set", [0, "k1"]],
		["the row is past the end", one(), "Row 4: Hours must be set", null],
		["row zero", one(), "Row 0: nothing", null],
		["not at the start", one(), "Check Row 3: From Time overlaps", null],
		["no row at all", one(), "From Time overlaps another entry", null],
		["another language is left to the banner", one(), "Fila 3: Horas", null],
		["a label only the first table has", two(), "Row 2: Warehouse is required", [0, "k2"]],
		["a label only the second table has", two(), "Row 1: Account is not valid", [1, "k1"]],
		["a label BOTH tables have", two(), "Row 2: Description is too long", null],
		["labels of both tables", two(), "Row 2: Warehouse and Account differ", null],
		["a label that is part of another word", two(), "Row 2: Rated Items differ", null],
		["no label named", two(), "Row 2: something is wrong", null],
		["no tables", { verb: "create", tables: [] }, "Row 1: x", null],
		["a message that is not text", one(), undefined, null],
	])("%s", (_, m, message, want) => {
		const hit = rowNamedBy(message, m);
		expect(hit && [hit.tableIndex, hit.row.__key]).toEqual(want);
	});

	// Break: an update numbers rows the way a create does, so the server's "Row 2" lands on a different
	// row (a saved row is always sent, a blank new row never is).
	it("counts the rows the save sent: saved rows always, a blank new row never", () => {
		const saved = (key, name) => ({ __key: key, __name: name });
		const m = model(
			[
				[
					"time_logs",
					"Time Logs",
					["From Time"],
					[saved("s1", "A"), row("blank", {}), saved("s2", "B")],
				],
			],
			"update"
		);
		expect(rowNamedBy("Row 2: x", m).row.__key).toBe("s2");
	});
});

describe("markRowFromMessage: the mark on the form", () => {
	// Break: the old mark stays on another row after the next refusal, the marked row stays closed
	// (the person sees nothing), or the private flag reaches the payload.
	it("flags and opens the named row, clears the old flag, and changes nothing that is sent", () => {
		const m = model([["time_logs", "Time Logs", ["From Time"], rows3()]]);
		m.tables[0].rows[0].__flag = true;
		expect(hasFlaggedRow(m)).toBe(true);
		expect(markRowFromMessage(m, OVERLAP).row.__key).toBe("k3");
		expect(m.tables[0].rows.map((r) => !!r.__flag)).toEqual([false, false, true]);
		expect(m.tables[0].rows[2].__open).toBe(true);
		expect(markRowFromMessage(m, "Hours must be set")).toBeNull();
		expect(hasFlaggedRow(m)).toBe(false);
	});

	it("does not change the payload of a form (private keys are never sent)", () => {
		const m = {
			...model([["time_logs", "Time Logs", ["From Time"], rows3()]]),
			fields: [],
			calc: [],
		};
		const card = { draft: { verb: "create", values: { time_logs: [{}, {}, {}] } } };
		const snap = snapshotDraftModel(m, card);
		const before = JSON.stringify(draftCardValues(m, snap, card));
		markRowFromMessage(m, OVERLAP);
		expect(JSON.stringify(draftCardValues(m, snap, card))).toBe(before);
	});
});

describe("the row number the server counts is the row the form marks", () => {
	// Break: the mark lands one row off when a blank row sits above the named one (the form counts it, the
	// payload does not: the server's "Row 2" is the SECOND row SENT), or a flag makes the form look edited.
	it("with a blank row above, Row 2 is the second row the payload sends", () => {
		const m = timesheetModel();
		const card = timesheetCard();
		const snap = snapshotDraftModel(m, card);
		const table = m.tables[0];
		table.rows.splice(1, 0, blankRow(table)); // [r1, blank, r2, r3]
		table.rows[3].description = "Fixes, changed"; // touch the table so it is sent
		const sent = draftCardValues(m, snap, card).time_logs;
		expect(sent).toHaveLength(3);
		expect(sentRows(table, "create")).toHaveLength(sent.length);
		// the second row sent is the drafted row 1 (_draft_row), which is the form's third row
		expect(sent[1]._draft_row).toBe(1);
		expect(rowNamedBy("Row 2: From Time overlaps", m).row).toBe(table.rows[2]);
		expect(rowNamedBy("Row 3: From Time overlaps", m).row).toBe(table.rows[3]);
		expect(rowNamedBy("Row 4: From Time overlaps", m)).toBeNull();
	});

	it("a flagged row does not make an untouched form look edited", () => {
		const m = timesheetModel();
		const card = timesheetCard();
		const snap = snapshotDraftModel(m, card);
		expect(draftCardDirty(m, snap, card)).toBe(false);
		markRowFromMessage(m, "Row 2: From Time overlaps");
		expect(m.tables[0].rows[1].__flag).toBe(true);
		expect(draftCardDirty(m, snap, card)).toBe(false);
	});
});
