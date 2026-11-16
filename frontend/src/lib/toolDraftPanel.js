// The pure parts of editing and previewing a SERVER draft card on the desktop: which form field
// a drafted key is, the model's stamp and the snapshot guard, the card's own computed cells for
// Preview, and where the first mark of a refused save is. No Vue, no API; relative imports only.
// The host wiring (the doors, the busy state) is lib/toolDraftHost.js; ChatView.vue only calls it.
import {
	normCell,
	rowCellsChanged,
	setSnapshotCell,
	snapshotDraftModel,
	snapshotMatchesCard,
} from "./draftCardValues.js";
import { stampDraftModel } from "./draftSave.js";

export const CANT_EDIT_TEXT =
	"This draft can't be edited here. Use Create on the card, or ask Jarvis to change it.";
export const STORED_UNAVAILABLE_TEXT =
	"The saved values of this record could not be loaded, so the form shows only what Jarvis drafted.";

// Marks an action that was made from a server draft card (draftFromCard output handed to the
// model builder by the host). A Symbol, so a typed action's JSON, which a model writes, can never
// carry it: the typed path keeps resolving by label and keeps refusing an unloadable record.
export const FROM_CARD = Symbol("jarvis.fromCard");

const isObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);

// Which form field a drafted entry belongs to. With `exact` (the action came from a server draft
// card, `action[FROM_CARD]`) an entry is the field its `fieldname` names, EXACTLY: no match in the
// form's meta leaves the key out of the form (the server keeps an absent key, so nothing is
// lost) and never falls back to the label, which could land the value on another field. Without
// it (every typed action, whatever keys the model wrote) the entry's label is resolved as before:
// `byLabel(label)`.
export function resolveDraftField(entry, metaFields, byLabel, exact) {
	if (exact) {
		const name = entry && entry.fieldname;
		if (!name) return null;
		return (
			(metaFields || []).find((f) => f.fieldname === name && f.fieldtype !== "Table") || null
		);
	}
	return byLabel(entry && entry.label);
}

// Stamp a freshly built model as the form of ONE server draft card and take its snapshot. Call
// this straight after the build (the form meta, the stored record and the rule values are in),
// before the model is shown and before anything can add or remove a row: the snapshot is the
// baseline of every later diff. `guard` is snapshotMatchesCard's answer; when it is not ok the
// form may still open but `saveBlocked` carries the line shown instead of a Save (a wrong
// baseline would send a wrong save).
export function prepareToolModel(model, { token, conversation, card, fill }) {
	// The card's own numbers go in BEFORE the snapshot, so they are the baseline and never a change.
	if (fill) fillCalculatedCells(model, card, fill);
	const snapshot = snapshotDraftModel(model, card);
	const guard = snapshotMatchesCard(snapshot, card);
	// the card as it was when the form opened: later changes to the list item never reach the diff
	const frozen = JSON.parse(JSON.stringify(card));
	stampDraftModel(model, {
		messageKey: "",
		card: frozen,
		editable: false,
		actionKey: `tool:${token}`,
	});
	model.tool = true;
	model.token = token;
	model.conversation = conversation;
	model.snapshot = snapshot;
	model.saveBlocked = guard.ok ? "" : CANT_EDIT_TEXT;
	return { model, guard };
}

// Preview shows the numbers the CARD shows (the server's trial), never a recomputation. The
// card's table is matched to a model table only on a label that is unique on both sides, with
// the same number of rows; its cells are keyed by the column fieldnames the card carries. Any
// doubt leaves the table out and its calculated cells blank. Returns `{[table fieldname]:
// [row -> {fieldname: text}]}` (sparse), the shape DraftPreview's `model.computed` takes.
export function computedFromCard(model, card) {
	const out = {};
	const cardTables = (card && Array.isArray(card.tables) ? card.tables : []).filter(isObject);
	const tables = (model && model.tables) || [];
	const labelOf = (t) =>
		String((t && t.label) || "")
			.trim()
			.toLowerCase();
	for (const t of tables) {
		const label = labelOf(t);
		if (!label || tables.filter((x) => labelOf(x) === label).length !== 1) continue;
		const mine = cardTables.filter((c) => labelOf(c) === label);
		if (mine.length !== 1) continue;
		const ct = mine[0];
		const rows = Array.isArray(ct.rows) ? ct.rows : [];
		const count = Number.isInteger(ct.count) ? ct.count : rows.length;
		if (count !== (t.rows || []).length || rows.length > count) continue;
		const names = Array.isArray(ct.fieldnames) ? ct.fieldnames : [];
		const aligned = new Array(t.rows.length).fill(undefined);
		rows.forEach((r, i) => {
			const cells = isObject(r) && Array.isArray(r.cells) ? r.cells : [];
			const row = {};
			names.forEach((name, j) => {
				const cell = cells[j];
				if (typeof name === "string" && name && cell != null && String(cell) !== "")
					row[name] = cell;
			});
			if (Object.keys(row).length) aligned[i] = row;
		});
		if (aligned.some(Boolean)) out[t.fieldname] = aligned;
	}
	return out;
}

// ---- cells the card shows and the draft did not set --------------------------------------
//
// The server's trial gives a card cells nobody drafted: Hrs on a Timesheet, an Is Billable
// default. In the form and the Preview those cells are blank unless they are filled from the card
// (`lineItemSummary` fills only a READ-ONLY column, and Hrs and Is Billable are not read-only).
// A cell filled here is display: it is put in the row before the snapshot, so it is the baseline
// and is not a change; `settleCalculatedCells` blanks it while the person has edited its row (its
// value would be stale) and gives it back when the row is as drafted again. It is sent only if
// the person types a value of their own into it; clearing it by hand is such a change too (the
// empty cell is sent), it is not undone.

// A cell of these types never feeds a calculation, so editing one leaves the row's filled cells.
const TEXT_TYPES = new Set([
	"Data",
	"Small Text",
	"Text",
	"Long Text",
	"Text Editor",
	"Code",
	"HTML Editor",
	"Markdown Editor",
]);
const feedsCalculation = (column) => !TEXT_TYPES.has(column.fieldtype || "");
const NUMBER_TYPES = new Set(["Int", "Float", "Currency", "Percent", "Rating"]);
// A typed control cannot show the card's formatted text for these.
const NOT_FILLED = new Set(["Date", "Datetime", "Time", "Table", "Table MultiSelect"]);
const isBlank = (v) => v == null || String(v).trim() === "";
// The card's text for a number, only when it is plain digits with a dot decimal ("1.50"). Any
// comma, space, percent sign or symbol is ambiguous without the site's number format ("1,500" is
// 1.5 in a comma-decimal format), so the cell stays blank: blank is honest, a wrong number is not.
const PLAIN_NUMBER = /^-?\d+(\.\d+)?$/;
const plainNumber = (text) => {
	const t = String(text).trim();
	return PLAIN_NUMBER.test(t) ? t : null;
};

// What to put in a cell of this column for the card's text, or null to leave it blank. `verbatim`
// (the Preview, which only displays) puts the card's own text in a number cell, so Preview reads
// exactly as the card does in every number format.
function cellFor(column, text, verbatim) {
	const type = column.fieldtype || "";
	if (column.read_only) return String(text);
	if (NOT_FILLED.has(type)) return null;
	if (NUMBER_TYPES.has(type)) return verbatim ? String(text) : plainNumber(text);
	return String(text);
}

// Fill the cells of `model`'s tables that are blank in the model and shown by the card, matching
// rows and columns the way `computedFromCard` does (unique label, equal row count, column
// fieldnames). `readOnly: false` leaves the read-only columns to the Preview's own calculated-cell
// rule; `verbatim: true` (the Preview) fills a number cell with the card's text as shown. Records
// what it filled in `model.calc`, for `settleCalculatedCells`. Returns the list.
export function fillCalculatedCells(model, card, { readOnly = true, verbatim = false } = {}) {
	const calc = [];
	const cells = computedFromCard(model, card);
	for (const t of (model && model.tables) || []) {
		const rows = cells[t.fieldname];
		if (!rows) continue;
		t.rows.forEach((row, i) => {
			const shown = rows[i];
			if (!shown || row.__key == null) return;
			for (const c of t.columns || []) {
				if (!(c.fieldname in shown) || !isBlank(row[c.fieldname])) continue;
				if (c.read_only && !readOnly) continue;
				const value = cellFor(c, shown[c.fieldname], verbatim);
				if (value == null) continue;
				row[c.fieldname] = value;
				calc.push({
					table: t.fieldname,
					key: String(row.__key),
					fieldname: c.fieldname,
					fieldtype: c.fieldtype || "",
					text: value,
					last: value,
				});
			}
		});
	}
	model.calc = calc;
	return calc;
}

// Keep the filled cells honest while the person edits. A filled cell shows the card's value
// while its row is as drafted; once any other editable cell of the row that can feed a
// calculation (any type but a pure text one) has changed, the value may be stale, so it is blank (ERPNext calculates it on save). A cell the person typed into
// themselves (its value is neither what was last put there) is theirs: left alone, sent as an
// edit. The snapshot cell follows what was last put in, so the blanking is never a change.
// Idempotent. Returns true when it wrote something.
export function settleCalculatedCells(model) {
	const calc = model && model.calc;
	if (!Array.isArray(calc) || !calc.length) return false;
	let wrote = false;
	const byRow = new Map();
	for (const e of calc) {
		const id = `${e.table}\u0000${e.key}`;
		if (!byRow.has(id)) byRow.set(id, []);
		byRow.get(id).push(e);
	}
	for (const entries of byRow.values()) {
		const table = (model.tables || []).find((t) => t.fieldname === entries[0].table);
		const row = table && table.rows.find((r) => String(r.__key) === entries[0].key);
		if (!row) continue;
		const filled = new Set(entries.map((e) => e.fieldname));
		const edited = rowCellsChanged(table, row, model.snapshot, filled, feedsCalculation);
		for (const e of entries) {
			if (e.owned) continue;
			if (normCell(e.fieldtype, row[e.fieldname]) !== normCell(e.fieldtype, e.last)) {
				e.owned = true; // the person's own value
				continue;
			}
			const want = edited ? "" : e.text;
			if (normCell(e.fieldtype, want) === normCell(e.fieldtype, e.last)) continue;
			row[e.fieldname] = want;
			e.last = want;
			setSnapshotCell(model.snapshot, e.table, row, e.fieldname, e.fieldtype, want);
			wrote = true;
		}
	}
	return wrote;
}

// ---- the Preview's dates -----------------------------------------------------------------
//
// The client has no date formatter of its own: the card's dates are the server's text in the
// person's date format. The Preview shows that text for a date, a datetime or a time, so it can
// never read "2026-12-08T09:30" where the card reads "08-12-2026 09:30:00". Tables: the card's
// cell, by the same matching as `computedFromCard`. Main fields: the card's row (create) or
// change (update) with the same label, when the label is unique on both sides. Anything in doubt
// keeps the model's own value. Changes the Preview's model only.
const DATE_TYPES = new Set(["Date", "Datetime", "Time"]);
const FIELD_DATE = { date: "Date", datetime: "Datetime", time: "Time" };
const fieldType = (f) => f.fieldtype || FIELD_DATE[f.control] || "";
const labelKey = (v) =>
	String(v || "")
		.trim()
		.toLowerCase();

export function cardDatesIntoPreview(model, card) {
	const cells = computedFromCard(model, card);
	for (const t of (model && model.tables) || []) {
		const rows = cells[t.fieldname];
		if (!rows) continue;
		t.rows.forEach((row, i) => {
			for (const c of t.columns || []) {
				const text = rows[i] && rows[i][c.fieldname];
				if (
					DATE_TYPES.has(c.fieldtype || "") &&
					!isBlank(text) &&
					!isBlank(row[c.fieldname])
				)
					row[c.fieldname] = text;
			}
		});
	}
	const rows = (card && Array.isArray(card.rows) ? card.rows : []).filter(
		(r) => r && typeof r === "object"
	);
	const diff = (card && Array.isArray(card.diff) ? card.diff : []).filter(
		(d) => d && typeof d === "object"
	);
	const fields = (model && model.fields) || [];
	for (const f of fields) {
		if (!DATE_TYPES.has(fieldType(f))) continue;
		const key = labelKey(f.label);
		if (!key || fields.filter((x) => labelKey(x.label) === key).length !== 1) continue;
		const row = rows.filter((r) => labelKey(r.label) === key);
		if (row.length === 1 && !isBlank(row[0].value) && !isBlank(f.value))
			f.value = String(row[0].value);
		const change = diff.filter((d) => labelKey(d.label) === key);
		if (change.length === 1 && f.changed) {
			if (!isBlank(change[0].to)) f.value = String(change[0].to);
			if (!isBlank(change[0].from)) f.orig = String(change[0].from);
		}
	}
	return model;
}

// The label of the form's primary button, in the card's own words.
export function toolPrimaryLabel(model) {
	return model && model.verb === "update" ? "Save changes" : "Create";
}

// ---- where the first mark of a refused save is -------------------------------------------
//
// Only what the SERVER marked counts: a main field it named (`error.fields` without a
// parentfield) or a row it named (`row.__missing`, put there by markMissingRows). A field that is
// merely required and empty is not a mark: the form draws it too, but the person is not sent
// there.

// The first server mark of `model`, in the form's order (main fields, then each table's rows), or
// null. `fields` is the refusal's `error.fields`; call this after markMissing / markMissingRows.
// `{kind: "field", index}` (the position in model.fields) or `{kind: "row", tableIndex, key,
// fieldname}`.
export function firstServerMark(model, fields) {
	const marked = new Set(
		(Array.isArray(fields) ? fields : [])
			.filter((m) => m && !m.parentfield && typeof m.fieldname === "string")
			.map((m) => m.fieldname)
	);
	const index = ((model && model.fields) || []).findIndex((f) => marked.has(f.fieldname));
	if (index >= 0) return { kind: "field", index };
	const tables = (model && model.tables) || [];
	for (let tableIndex = 0; tableIndex < tables.length; tableIndex++) {
		const row = (tables[tableIndex].rows || []).find(
			(r) => Array.isArray(r.__missing) && r.__missing.length
		);
		if (row) return { kind: "row", tableIndex, key: row.__key, fieldname: row.__missing[0] };
	}
	return null;
}

const ERROR_SELECTOR = ".jv-draft-body [role=alert]";
const esc = (id) => (typeof CSS !== "undefined" && CSS.escape ? CSS.escape(id) : id);

// Scroll the server's first mark of `root` into view and put focus in its control; returns the
// marked element, or null. With no mark (a busy draft, a table the server refused as a whole)
// the error banner is scrolled into view instead and takes no focus. Called after the DOM has
// caught up with the marks. `mark` is firstServerMark's answer.
export function focusFirstMark(root, mark) {
	const find = (sel) => (root && root.querySelector ? root.querySelector(sel) : null);
	let el = null;
	if (mark && mark.kind === "field") {
		const all = root && root.querySelectorAll ? root.querySelectorAll(".jv-draft-fld") : [];
		el = all[mark.index] || null;
	} else if (mark && mark.kind === "row") {
		const base = `jv-rowform-${mark.tableIndex}-${mark.key}`;
		el = find(`#${esc(`${base}-${mark.fieldname}`)}`) || find(`#${esc(base)}`);
	}
	if (!el) {
		const banner = find(ERROR_SELECTOR);
		if (banner && typeof banner.scrollIntoView === "function")
			banner.scrollIntoView({ block: "nearest", behavior: "smooth" });
		return null;
	}
	if (typeof el.scrollIntoView === "function")
		el.scrollIntoView({ block: "center", behavior: "smooth" });
	const control =
		el.matches && el.matches("input, select, textarea")
			? el
			: el.querySelector("input, select, textarea") || el;
	if (typeof control.focus === "function") control.focus({ preventScroll: true });
	return el;
}
