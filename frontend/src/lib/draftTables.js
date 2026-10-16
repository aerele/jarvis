// The child-table part of the record draft card's model (ChatView's
// buildDraftModel), pulled out so it is unit-testable without mounting the view.
// One table: its grid columns (the form meta's columns plus any key the agent used
// outside them, typed from the child's meta, #655), its row form (form_fields),
// and its rows, each carrying every field of the row form so the form shows saved
// values and Apply can send what the user edits there.
import {
	allRowFields,
	coerceRow,
	isRowKeyColumn,
	overlaySavedRows,
	rowKeyColumn,
	toPanelRow,
} from "@/lib/draftApply";

// A blank row with every field of the table, for "Add row" and an empty table.
export function blankRow(table) {
	const o = {};
	for (const c of allRowFields(table)) o[c.fieldname] = "";
	return o;
}

// The model for one child table, or null when the card does not show it (not
// required, not proposed, and no saved rows). `proposed` is the agent's rows for
// this table (undefined when it proposed none), `saved` the loaded rows (update).
export function buildDraftTable({ fieldname, spec, required, proposed, saved = [], verb }) {
	const proposedRows = proposed && overlaySavedRows(proposed, saved);
	if (!proposedRows && !required && !saved.length) return null;
	const columns = spec.columns.slice();
	const known = new Set(columns.map((c) => c.fieldname));
	for (const r of proposedRows || []) {
		for (const k of Object.keys(r)) {
			if (!known.has(k) && isRowKeyColumn(k)) {
				known.add(k);
				columns.push(rowKeyColumn(k, spec.extra_columns));
			}
		}
	}
	const table = {
		fieldname,
		label: spec.label,
		child: spec.child_doctype,
		columns,
		formFields: spec.form_fields || [],
		rows: [],
		origJson: JSON.stringify(verb === "update" ? saved : null), // raw saved rows
	};
	// A proposal REPLACES the loaded rows. Each row keeps its saved name (__name)
	// so Apply edits THAT row (CR-3).
	const fields = allRowFields(table);
	const src = proposedRows != null ? proposedRows : saved;
	table.rows = src.map((r) => toPanelRow(fields, r));
	if (!table.rows.length) table.rows.push(blankRow(table));
	return table;
}

// The saved row a panel row came from (by its name), for showing what changed.
// The table's saved rows by name, parsed once (the card renders a status per row).
export function savedRowsByName(table) {
	let saved = [];
	try {
		saved = JSON.parse(table.origJson || "null") || [];
	} catch {
		saved = [];
	}
	const out = new Map();
	for (const r of saved) if (r && r.name) out.set(r.name, r);
	return out;
}

export function savedRowFor(table, row) {
	return (row && row.__name && savedRowsByName(table).get(row.__name)) || null;
}

// The plain defaults of the record's own fields (what Desk's new form holds), so a
// child rule reading `parent.update_stock` on a create sees Desk's default.
// A Check or number default is a number, as Desk's doc holds it: the string "0" is
// truthy, and `eval:!parent.x` would read it the wrong way.
const NUMERIC_TYPES = new Set(["Check", "Int", "Float", "Currency", "Percent", "Rating"]);
export function parentDefaults(metaFields) {
	const out = {};
	for (const f of metaFields || []) {
		if (f.fieldtype === "Table" || !f.default) continue;
		const n = Number(f.default);
		out[f.fieldname] = NUMERIC_TYPES.has(f.fieldtype) && !Number.isNaN(n) ? n : f.default;
	}
	return out;
}

// The panel rows the save sent, in the order it sent them: a blank new row is never
// sent (draftValues / tableRowsPayload), so the server's row numbers count only the
// rest. A saved row is always sent on an update (by name), even unchanged.
function sentRows(table, verb) {
	return table.rows.filter(
		(r) => (verb === "update" && r.__name) || Object.keys(coerceRow(table, r, verb)).length
	);
}

// Put a failed save's missing child fields on their rows and open those rows, so
// the user sees what is missing instead of a silent failure (entries carry
// parentfield, idx and fieldname; idx counts the rows the save sent). Returns how
// many rows were marked.
export function markMissingRows(model, missing) {
	for (const t of model.tables || []) for (const r of t.rows) delete r.__missing;
	let marked = 0;
	for (const m of missing || []) {
		if (!m || !m.parentfield || !m.idx) continue;
		const t = (model.tables || []).find((x) => x.fieldname === m.parentfield);
		const row = t && sentRows(t, model.verb)[Number(m.idx) - 1];
		if (!row) continue;
		if (!row.__missing) {
			row.__missing = [];
			marked++;
		}
		row.__missing.push(m.fieldname);
		row.__open = true;
	}
	return marked;
}
