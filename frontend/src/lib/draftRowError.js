// A refused save that names a ROW only in its words. The server marks a field, or a row's field, when
// it can (error.fields with parentfield + idx); a rule a DocType controller enforces while the draft
// is trialled (a Timesheet's "Row 3: From Time and To Time of TS-... is overlapping with TS-...")
// comes back as a message alone, with no fields. This reads the leading "Row N:" and, only when the
// table it belongs to is unambiguous, marks that row of the form so the person is taken to it.
// Pure, relative imports only. The wording of the message is the server's and is never changed.
import { sentRows } from "./draftTables.js";

const ROW = /^\s*Row\s*#?\s*(\d+)\s*[:：]/i;

const norm = (v) =>
	String(v == null ? "" : v)
		.trim()
		.toLowerCase();
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// the label stands as its own words in the message ("From Time", not a part of another word)
const names = (message, label) =>
	!!label &&
	new RegExp(`(^|[^\\p{L}\\p{N}])${escapeRe(label)}($|[^\\p{L}\\p{N}])`, "iu").test(message);

// The field labels a table shows: its grid columns and the fields of its row form.
function labelsOf(table) {
	const out = new Set();
	for (const c of [
		...((table && table.columns) || []),
		...((table && table.formFields) || []),
	]) {
		const label = norm(c && c.label);
		if (label) out.add(label);
	}
	return out;
}

// { tableIndex, row } for the row the message names, or null. The table is the only one in the form,
// or else the only one that has a field whose label the message names (a label that two tables share
// proves nothing). Never a guess between two tables; null too when that table has no such row.
export function rowNamedBy(message, model) {
	const m = ROW.exec(typeof message === "string" ? message : "");
	const tables = (model && model.tables) || [];
	if (!m || !tables.length) return null;
	const number = Number(m[1]);
	if (!(number >= 1)) return null;
	let at = -1;
	if (tables.length === 1) at = 0;
	else {
		const text = norm(message.slice(m[0].length));
		const labels = tables.map(labelsOf);
		const hit = new Set();
		for (const label of new Set(labels.flatMap((l) => [...l]))) {
			const owners = labels.map((l, i) => (l.has(label) ? i : -1)).filter((i) => i >= 0);
			if (owners.length === 1 && names(text, label)) hit.add(owners[0]);
		}
		if (hit.size !== 1) return null;
		at = [...hit][0];
	}
	const row = sentRows(tables[at], model.verb)[number - 1];
	return row ? { tableIndex: at, row } : null;
}

// Flag the row the message names (and open it), clearing the flag from every other row first, so a
// later save never leaves an old one behind. Returns the match, or null. `__flag` is private form
// state like `__missing` and `__open`: it never reaches the payload or the snapshot.
export function markRowFromMessage(model, message) {
	for (const t of (model && model.tables) || []) for (const r of t.rows) delete r.__flag;
	const hit = rowNamedBy(message, model);
	if (hit) {
		hit.row.__flag = true;
		hit.row.__open = true;
	}
	return hit;
}

// Is any row of the form flagged by a message?
export function hasFlaggedRow(model) {
	return ((model && model.tables) || []).some((t) => t.rows.some((r) => r.__flag));
}
