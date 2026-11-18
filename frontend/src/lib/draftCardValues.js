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
// Row identity, as the server reads it (jarvis/chat/draft_values.py): a kept row is `{name}`
// (an update's saved row), `{_draft_row: i}` (a create row, or an update row the draft ITSELF
// added: a drafted row with no name), then only the cells the person changed. A row the
// person added has no identity and all its cells.
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

// Row identity keys: a cell column called like one must never overwrite it in the payload.
const IDENTITY = new Set(["name", "_draft_row"]);

const sendable = (f) =>
	!!f.fieldname &&
	!String(f.fieldname).startsWith("__") &&
	!NEVER_SENT.has(f.fieldtype) &&
	!f.read_only;

// The columns of a table whose cells the person can change and the form may send.
// A MAIN field called `name` is a real field (a prompt-named create), so only the cells drop it.
const cellColumns = (table) =>
	allRowFields(table).filter((c) => sendable(c) && !IDENTITY.has(c.fieldname));

const isObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);
const rowKeyOf = (r) => (r && r.__key != null ? String(r.__key) : null);

// A row the save counts: a saved row (it has its name) or one with any value in it. A blank
// row left in the grid is nothing (the same test as coerceRow, so the row numbers of a
// failed save's marks line up with the rows sent).
export function isReal(table, r) {
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

// An update's model rows with no name that came from the draft: `{[row index]: drafted index}`.
// buildDraftTable lays an update table out as the draft's own list (proposed rows over the
// saved ones by name), one model row per drafted row in draft order, so the k-th nameless
// model row IS the k-th nameless drafted row. That is trusted only when it can be checked:
// the counts of nameless rows must be equal, and the named rows must be the same names in
// the same order. Otherwise nothing is assigned and those rows are sent whole (nothing
// lost, but their unsent drafted cells are).
function draftedNewRows(rows, drafted) {
	if (!drafted) return {};
	const nameOf = (r) => (r && r.__name ? String(r.__name) : "");
	const draftedName = (d) => (isObject(d) && d.name ? String(d.name) : "");
	const draftNameless = [];
	drafted.forEach((d, i) => {
		if (isObject(d) && !draftedName(d)) draftNameless.push(i);
	});
	const modelNameless = [];
	rows.forEach((r, i) => {
		if (!nameOf(r)) modelNameless.push(i);
	});
	if (modelNameless.length !== draftNameless.length) return {};
	const named = (list, of) => list.map(of).filter(Boolean);
	const modelNames = named(rows, nameOf);
	const draftNames = named(drafted, draftedName);
	if (modelNames.length !== draftNames.length || modelNames.some((n, i) => n !== draftNames[i]))
		return {};
	return Object.fromEntries(modelNameless.map((m, k) => [m, draftNameless[k]]));
}

function snapshotTable(table, verb, card) {
	const cols = cellColumns(table);
	const drafted = draftRowsOf(card, table.fieldname);
	const modelRows = table.rows || [];
	// On a create, row i of the form is row i of the draft only while the builder laid the
	// draft's rows out one for one. If the counts differ nothing can be matched, so every
	// row is treated as new (its complete cells are sent: nothing is lost).
	const aligned = verb === "create" && !!drafted && drafted.length === modelRows.length;
	const added = verb === "update" ? draftedNewRows(modelRows, drafted) : {};
	const keys = [];
	const rows = {};
	const types = {};
	for (const c of cols) types[c.fieldname] = c.fieldtype || "";
	modelRows.forEach((r, i) => {
		const k = rowKeyOf(r);
		if (isReal(table, r)) keys.push(k);
		if (k == null) return;
		const cells = {};
		for (const c of cols) cells[c.fieldname] = norm(c.fieldtype, r[c.fieldname]);
		const origin = verb === "update" ? r.__name || null : aligned ? i : null;
		rows[k] = { origin, cells };
		if (i in added) rows[k].draftRow = added[i];
	});
	return { keys, rows, types };
}

// What the form showed when it was built: call it once, after the model is complete and
// every writer that is not the person has run, and keep the result next to the model.
export function snapshotDraftModel(model, card) {
	const verb = verbOf(model, null, card);
	const fields = {};
	const types = {};
	for (const f of (model && model.fields) || []) {
		if (!f || !f.fieldname) continue;
		fields[f.fieldname] = norm(typeOf(f), f.value);
		types[f.fieldname] = typeOf(f);
	}
	const tables = {};
	for (const t of (model && model.tables) || []) {
		if (t && t.fieldname) tables[t.fieldname] = snapshotTable(t, verb, card);
	}
	return { verb, fields, types, tables };
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

// A kept row's identity key: `_draft_row` (a create row, or an update row the draft itself
// added) or `name` (an update's saved row); null for a row the person added.
function identityOf(was, verb) {
	if (!was) return null;
	if (verb === "create") return was.origin != null ? { _draft_row: was.origin } : null;
	if (was.origin != null) return { name: was.origin };
	return was.draftRow != null ? { _draft_row: was.draftRow } : null;
}

// The complete final row list of a touched table, in the form's order.
function tableRows(table, snap, real, cols, verb) {
	const out = [];
	for (const r of real) {
		const was = snap.rows[rowKeyOf(r)];
		const row = {};
		const identity = identityOf(was, verb);
		if (identity) {
			// A kept row: who it is, then only the cells the person changed.
			Object.assign(row, identity);
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
// private key (`__...`), a read-only, layout, attachment or secret field, or a row cell
// called `name` or `_draft_row`. The row identity is read from the snapshot; `card` only
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

// Whether the person changed any cell of `row` since the snapshot (the cells in `skip` are
// not counted; with `only`, just the columns it accepts are). A row the snapshot does not hold
// counts as changed. The host uses this to
// tell an untouched row from an edited one; it is a read of the same baseline the payload uses.
export function rowCellsChanged(table, row, snapshot, skip = new Set(), only = null) {
	const tsnap = snapshot && snapshot.tables && snapshot.tables[table && table.fieldname];
	const was = tsnap && tsnap.rows && tsnap.rows[rowKeyOf(row)];
	if (!was) return true;
	return cellColumns(table).some(
		(c) =>
			!skip.has(c.fieldname) &&
			(!only || only(c)) &&
			norm(c.fieldtype, row[c.fieldname]) !== (was.cells[c.fieldname] ?? "")
	);
}

// One cell value as the diff compares it ("3" and 3 are the same, a blank is "").
export const normCell = (fieldtype, value) => norm(fieldtype, value);

// Set one cell of the snapshot (so the diff reads `value` as the unchanged baseline). Does
// nothing when the snapshot holds no such cell (a read-only column is never compared).
export function setSnapshotCell(snapshot, tableFieldname, row, fieldname, fieldtype, value) {
	const was = snapshot?.tables?.[tableFieldname]?.rows?.[rowKeyOf(row)];
	if (was && Object.prototype.hasOwnProperty.call(was.cells, fieldname))
		was.cells[fieldname] = norm(fieldtype, value);
}

// Whether closing the form would lose the person's work: the save would send something.
export function draftCardDirty(model, snapshot, card) {
	return Object.keys(draftCardValues(model, snapshot, card)).length > 0;
}

// ---- the snapshot guard ------------------------------------------------------------------
//
// `draftCardValues` is only as right as its baseline. A snapshot taken AFTER the person's
// first keystroke makes a real edit read as "unchanged" (the drafted value is then written
// while the form showed the edit); one taken BEFORE the form finished (an update's stored-
// record overlay) makes untouched cells read as changes. The host calls this right after
// taking the snapshot, and does not trust the snapshot when it says no.

// A drafted value as the form model holds it: draftFromCard stringifies an object, panelField
// and toPanelRow stringify everything else.
const asText = (v) => (v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

// The first cell of `drafted` the snapshot holds that differs from what the draft says, or
// null. Only cells the snapshot compares (writable columns) count.
function firstCellDiff(drafted, cells, types, skip = []) {
	for (const [cell, v] of Object.entries(drafted)) {
		if (skip.includes(cell) || !own(cells, cell)) continue;
		if (cells[cell] !== norm(types[cell] || "", asText(v))) return cell;
	}
	return null;
}

// Names only, never values: `why` goes to logs and a field may be a secret.
function checkTable(name, tsnap, drafted, verb) {
	if (!tsnap || !isObject(tsnap.rows) || !Array.isArray(tsnap.keys))
		return `table ${name}: unreadable snapshot`;
	const types = tsnap.types || {};
	// every row of the grid, a blank one too: a drafted row the person emptied is a mismatch
	const entries = Object.values(tsnap.rows).filter(isObject);
	// The builder lays a listed table out as one grid row per drafted entry (create and update:
	// `table.rows = proposedRows.map(...)`), so a different count means a row was added, removed
	// or inserted before the snapshot, and the payload would read it as "unchanged". An empty
	// drafted list is skipped: the builder shows one blank row for it. A table the snapshot does
	// not hold at all is not checked here: the form builder leaves a table out when it is not in
	// the DocType's form meta, so that case is legitimate and Save must stay on for it.
	if (drafted.length && entries.length !== drafted.length)
		return `table ${name}: ${entries.length} rows in the form, ${drafted.length} in the draft`;
	// the real rows in the grid's order (the object's own key order is not the grid's)
	const inOrder = tsnap.keys.map((k) => tsnap.rows[k]).filter(isObject);
	const differs = (draftedRow, was, where) => {
		const cell = firstCellDiff(draftedRow, was.cells || {}, types, ["name"]);
		return cell ? `table ${name} ${where} ${cell}: form differs from the draft` : "";
	};
	if (verb === "create") {
		// Rows line up with the draft only when the snapshot gave them an origin.
		for (const was of entries) {
			if (was.origin == null || !isObject(drafted[was.origin])) continue;
			const why = differs(drafted[was.origin], was, `row ${was.origin + 1}`);
			if (why) return why;
		}
		return "";
	}
	const draftRows = new Map();
	const order = [];
	for (const d of drafted) {
		if (isObject(d) && typeof d.name === "string" && d.name && !draftRows.has(d.name)) {
			draftRows.set(d.name, d);
			order.push(d.name);
		}
	}
	const named = inOrder.filter((w) => typeof w.origin === "string" && draftRows.has(w.origin));
	const seen = named.map((w) => w.origin);
	const expected = order.filter((n) => seen.includes(n));
	if (seen.length !== expected.length || seen.some((n, i) => n !== expected[i]))
		return `table ${name}: rows are not in the draft's order`;
	for (const was of named) {
		const why = differs(draftRows.get(was.origin), was, `row ${was.origin}`);
		if (why) return why;
	}
	// the rows the draft itself added, matched by their index in the draft's list
	for (const was of entries) {
		if (was.draftRow == null || !isObject(drafted[was.draftRow])) continue;
		const why = differs(drafted[was.draftRow], was, `row at index ${was.draftRow}`);
		if (why) return why;
	}
	return "";
}

// Is `snapshot` the form as `card` drafted it? `{ok, why}`; `why` is for logs, not users.
// Compared: every main field the snapshot holds that the draft also holds (the same
// normalisation the diff uses); on a create table whose rows lined up with the draft, each
// row's cells against the drafted row at its origin index; on an update table, that the
// saved rows the draft also names are in the draft's order and hold every cell the draft
// set, and so do the rows the draft added (by their index); and that a listed table has as many
// grid rows as drafted entries (a row added or removed before the snapshot). A field or cell the draft does
// not hold (a default the form filled) is not compared. `why` names the field or the table,
// row and cell, never a value (a field may be a secret).
// Never throws: anything unreadable is "not ok".
export function snapshotMatchesCard(snapshot, card) {
	try {
		const values = card && card.draft && card.draft.values;
		if (!isObject(snapshot) || !isObject(snapshot.fields) || !isObject(snapshot.tables))
			return { ok: false, why: "no snapshot" };
		if (!isObject(values)) return { ok: false, why: "card has no draft values" };
		const types = snapshot.types || {};
		for (const [fieldname, was] of Object.entries(snapshot.fields)) {
			if (!own(values, fieldname) || Array.isArray(values[fieldname])) continue;
			const want = norm(types[fieldname] || "", asText(values[fieldname]));
			if (was !== want)
				return { ok: false, why: `field ${fieldname}: form differs from the draft` };
		}
		const verb = snapshot.verb === "update" ? "update" : "create";
		for (const [name, tsnap] of Object.entries(snapshot.tables)) {
			const drafted = draftRowsOf(card, name);
			if (!drafted) continue;
			const why = checkTable(name, tsnap, drafted, verb);
			if (why) return { ok: false, why };
		}
		return { ok: true, why: "" };
	} catch (e) {
		return { ok: false, why: `could not compare: ${e && e.message ? e.message : "error"}` };
	}
}
