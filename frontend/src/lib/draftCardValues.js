// What the edit form of a SERVER draft card sends to save_draft_card: only what the
// person changed, compared with a snapshot taken when the form was built.
//
// Not `draftValues` (draftApply.js): that one drops blanks (so it cannot say "clear this")
// and on an update compares with the STORED record (so it cannot say "unchanged from the
// draft"). Here the baseline is the draft itself, as the form first showed it.
//
// The snapshot is taken after every writer that is not the person has run: the form
// builder's own coercion, the rule-values fill (writes only `__rule` / `__ruleSrc`, never
// a cell) and the computed cells (read-only columns, never compared or sent). Everything
// is compared NORMALISED, so a control's own formatting is not an edit: "3" and 3, a date
// the control rewrote, a Check written as "1" or "Yes".
//
// Rows are matched by the form's own row key (`__key`, minted only by newRowKey()), never by
// position, so an added or inserted row cannot inherit another row's origin.
//
// Pure, relative imports only: the phone shares this file.
import {
	allRowFields,
	checkToYesNo,
	coerceCell,
	coerceOut,
	isFieldWritable,
	normDateVal,
} from "./draftApply.js";

// Never sent, by fieldtype: layout, attachments and the other non-editable types (the
// server's own `_NON_EDIT_FIELDTYPES`, jarvis/chat/api.py) and a secret.
const NEVER_SENT = new Set([
	"Section Break",
	"Column Break",
	"Tab Break",
	"Fold",
	"Heading",
	"HTML",
	"Button",
	"Image",
	"Table",
	"Table MultiSelect",
	"Attach",
	"Attach Image",
	"Signature",
	"Geolocation",
	"Barcode",
	"Password",
]);
const NUMERIC = new Set(["Int", "Float", "Currency", "Percent", "Rating"]);
const DATES = new Set(["Date", "Datetime", "Time"]);
// A main field built by hand may carry only its control.
const FROM_CONTROL = {
	check: "Check",
	number: "Float",
	date: "Date",
	datetime: "Datetime",
	time: "Time",
};

const blank = (v) => v == null || String(v).trim() === "";
const typeOf = (f) => f.fieldtype || FROM_CONTROL[f.control] || "";

// One value in the form that compares equal whatever the control did to its text.
// null / undefined / "" / whitespace are all "".
function norm(fieldtype, v) {
	if (fieldtype === "Check") return checkToYesNo(v);
	if (blank(v)) return "";
	if (NUMERIC.has(fieldtype)) {
		const n = Number(String(v).trim());
		return Number.isFinite(n) ? String(n) : String(v);
	}
	if (DATES.has(fieldtype)) return normDateVal(fieldtype, v);
	return String(v);
}

const sendable = (f) =>
	!!f.fieldname &&
	!String(f.fieldname).startsWith("__") &&
	!NEVER_SENT.has(f.fieldtype) &&
	!f.read_only;

// The columns of a table whose cells the person can change and the form may send.
const cellColumns = (table) => allRowFields(table).filter(sendable);

const rowKeyOf = (r) => (r && r.__key != null ? String(r.__key) : null);

// A row the save counts: a saved row (it has its name) or one with any value in it. A blank
// row left in the grid is nothing (the same test as coerceRow, so the row numbers of a
// failed save's marks line up with the rows sent).
function isReal(table, r) {
	if (!r) return false;
	if (r.__name) return true;
	return allRowFields(table).some((c) => !NEVER_SENT.has(c.fieldtype) && !blank(r[c.fieldname]));
}

const draftRowsOf = (card, fieldname) => {
	const v = card && card.draft && card.draft.values && card.draft.values[fieldname];
	return Array.isArray(v) ? v : null;
};
const verbOf = (model, snapshot, card) =>
	(model && model.verb) ||
	(snapshot && snapshot.verb) ||
	(card && card.draft && card.draft.verb) ||
	"create";

function snapshotTable(table, verb, card) {
	const cols = cellColumns(table);
	const drafted = draftRowsOf(card, table.fieldname);
	// On a create, row i of the form is row i of the draft only while the builder laid the
	// draft's rows out one for one. If the counts differ nothing can be matched, so every
	// row is treated as new (its complete cells are sent: nothing is lost).
	const aligned = verb === "create" && !!drafted && drafted.length === (table.rows || []).length;
	const keys = [];
	const rows = {};
	(table.rows || []).forEach((r, i) => {
		const k = rowKeyOf(r);
		if (isReal(table, r)) keys.push(k);
		if (k == null) return;
		const cells = {};
		for (const c of cols) cells[c.fieldname] = norm(c.fieldtype, r[c.fieldname]);
		const origin = verb === "update" ? r.__name || null : aligned ? i : null;
		rows[k] = { origin, cells };
	});
	return { keys, rows };
}

// What the form showed when it was built: call it once, after the model is complete and
// every writer that is not the person has run, and keep the result next to the model.
export function snapshotDraftModel(model, card) {
	const verb = verbOf(model, null, card);
	const fields = {};
	for (const f of (model && model.fields) || []) {
		if (f && f.fieldname) fields[f.fieldname] = norm(typeOf(f), f.value);
	}
	const tables = {};
	for (const t of (model && model.tables) || []) {
		if (t && t.fieldname) tables[t.fieldname] = snapshotTable(t, verb, card);
	}
	return { verb, fields, tables };
}

function tableTouched(table, snap, real, cols) {
	const keys = real.map(rowKeyOf);
	if (keys.length !== snap.keys.length || keys.some((k, i) => k !== snap.keys[i])) return true;
	return real.some((r) => {
		const was = snap.rows[rowKeyOf(r)];
		if (!was) return true;
		return cols.some(
			(c) => norm(c.fieldtype, r[c.fieldname]) !== (was.cells[c.fieldname] ?? "")
		);
	});
}

// The complete final row list of a touched table, in the form's order.
function tableRows(table, snap, real, cols, verb) {
	const out = [];
	for (const r of real) {
		const was = snap.rows[rowKeyOf(r)];
		const row = {};
		if (was && was.origin != null) {
			// A kept row: who it is, then only the cells the person changed.
			if (verb === "create") row._draft_row = was.origin;
			else row.name = was.origin;
			for (const c of cols) {
				if (norm(c.fieldtype, r[c.fieldname]) === (was.cells[c.fieldname] ?? "")) continue;
				row[c.fieldname] = blank(r[c.fieldname]) ? "" : coerceCell(c, r[c.fieldname]);
			}
			out.push(row);
			continue;
		}
		// A new row: no identity, every filled cell. One with nothing to send is left out.
		for (const c of cols)
			if (!blank(r[c.fieldname])) row[c.fieldname] = coerceCell(c, r[c.fieldname]);
		if (Object.keys(row).length) out.push(row);
	}
	return out;
}

// The `values` of save_draft_card: a main field only when it differs from the snapshot (a
// cleared one as ""), a table only when touched, then as its complete final list. Never a
// private key (`__...`), a read-only, layout, attachment or secret field, `_draft_row` on an
// update, or `name` on a create row. The row identity is read from the snapshot; `card` only
// supplies the verb when the model carries none.
export function draftCardValues(model, snapshot, card) {
	const values = {};
	if (!model) return values;
	const snap = snapshot || { fields: {}, tables: {} };
	const verb = verbOf(model, snap, card);
	for (const f of model.fields || []) {
		if (!f || !sendable(f) || !isFieldWritable(f, verb)) continue;
		const cur = norm(typeOf(f), f.value);
		const was = Object.prototype.hasOwnProperty.call(snap.fields || {}, f.fieldname)
			? snap.fields[f.fieldname]
			: "";
		if (cur === was) continue;
		values[f.fieldname] = cur === "" ? "" : coerceOut(f);
	}
	for (const t of model.tables || []) {
		if (!t || !t.fieldname) continue;
		const tsnap = (snap.tables && snap.tables[t.fieldname]) || { keys: [], rows: {} };
		const cols = cellColumns(t);
		const real = (t.rows || []).filter((r) => isReal(t, r));
		if (!tableTouched(t, tsnap, real, cols)) continue;
		values[t.fieldname] = tableRows(t, tsnap, real, cols, verb);
	}
	return values;
}

// Whether closing the form would lose the person's work: the save would send something.
export function draftCardDirty(model, snapshot, card) {
	return Object.keys(draftCardValues(model, snapshot, card)).length > 0;
}
