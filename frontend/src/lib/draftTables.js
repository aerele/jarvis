// The child-table part of the record draft card's model (ChatView's
// buildDraftModel), pulled out so it is unit-testable without mounting the view.
// One table: its grid columns (the form meta's columns plus any key the agent used
// outside them, typed from the child's meta, #655), its row form (form_fields),
// and its rows, each carrying every field of the row form so the form shows saved
// values and Apply can send what the user edits there.
import {
	allRowFields,
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
export function savedRowFor(table, row) {
	if (!row || !row.__name) return null;
	let saved = [];
	try {
		saved = JSON.parse(table.origJson || "null") || [];
	} catch {
		saved = [];
	}
	return saved.find((r) => r && r.name === row.__name) || null;
}

// The plain defaults of the record's own fields (what Desk's new form holds), so a
// child rule reading `parent.update_stock` on a create sees Desk's default.
export function parentDefaults(metaFields) {
	const out = {};
	for (const f of metaFields || [])
		if (f.fieldtype !== "Table" && f.default) out[f.fieldname] = f.default;
	return out;
}

// Put a failed save's missing child fields on their rows and open those rows, so
// the user sees what is missing instead of a silent failure (entries carry
// parentfield, idx and fieldname). Returns how many rows were marked.
export function markMissingRows(model, missing) {
	for (const t of model.tables || []) for (const r of t.rows) delete r.__missing;
	let marked = 0;
	for (const m of missing || []) {
		if (!m || !m.parentfield || !m.idx) continue;
		const t = (model.tables || []).find((x) => x.fieldname === m.parentfield);
		const row = t && t.rows[Number(m.idx) - 1];
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
