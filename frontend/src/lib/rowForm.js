// A child row's full form for the record draft card: every field Desk's row edit
// form shows (form_fields from get_doctype_form_meta), laid out in its sections and
// columns, with Desk's depends_on / mandatory_depends_on / read_only_depends_on
// rules evaluated on the row. Pure, so it is unit-testable without the view.

import { allRowFields as rowFields } from "@/lib/draftApply";

const NUMERIC = new Set(["Int", "Float", "Currency", "Percent", "Rating"]);

export { rowFields };

// The row's cells (strings, as the card edits them) typed the way Desk's `doc`
// holds them, so a rule reads `doc.qty > 0` and `doc.is_free_item` as Desk does:
// a Check "0" is falsy, a number is a number.
export function typedRow(fields, row) {
	const doc = {};
	for (const f of fields) {
		const v = row[f.fieldname];
		if (f.fieldtype === "Check")
			doc[f.fieldname] = ["1", 1, true, "true", "yes", "Yes"].includes(v) ? 1 : 0;
		else if (NUMERIC.has(f.fieldtype)) {
			const n = Number(v);
			doc[f.fieldname] = v === "" || v == null || Number.isNaN(n) ? 0 : n;
		} else doc[f.fieldname] = v == null ? "" : v;
	}
	return doc;
}

// Desk's rule for depends_on and its siblings: empty means yes; "eval:<js>" is
// evaluated with the row as `doc` and the record as `parent`; a bare fieldname
// means "that field has a value". A rule that fails to evaluate counts as yes, so
// a field is never hidden by a broken rule. The rules come from DocType meta, which
// only System Managers edit, and Desk evaluates them the same way.
export function ruleHolds(rule, doc, parent) {
	const r = String(rule || "").trim();
	if (!r) return true;
	if (r.startsWith("eval:")) {
		try {
			// eslint-disable-next-line no-new-func
			return !!new Function("doc", "parent", `return (${r.slice(5)});`)(doc, parent || {});
		} catch {
			return true;
		}
	}
	if (r.startsWith("fn:")) return true; // a form-script method: not available here
	const v = doc[r];
	return !(v === undefined || v === null || v === "" || v === 0 || v === "0");
}

// The row form as sections of columns of fields, with each field's visibility,
// required and read-only state worked out for this row. Sections and columns left
// with no visible field are dropped.
export function rowFormLayout(table, row, parent) {
	const doc = typedRow(rowFields(table), row);
	const sections = [];
	let section = null;
	const newSection = (label, visible) => {
		section = { label, visible, columns: [[]] };
		sections.push(section);
	};
	for (const f of table.formFields || []) {
		if (f.fieldtype === "Section Break" || f.fieldtype === "Tab Break") {
			newSection(f.label || "", ruleHolds(f.depends_on, doc, parent));
			continue;
		}
		if (!section) newSection("", true);
		if (f.fieldtype === "Column Break") {
			section.columns.push([]);
			continue;
		}
		if (!ruleHolds(f.depends_on, doc, parent)) continue;
		section.columns[section.columns.length - 1].push({
			field: f,
			reqd:
				!!f.reqd ||
				(!!f.mandatory_depends_on && ruleHolds(f.mandatory_depends_on, doc, parent)),
			readOnly:
				!!f.read_only ||
				(!!f.read_only_depends_on && ruleHolds(f.read_only_depends_on, doc, parent)),
		});
	}
	return sections
		.filter((s) => s.visible)
		.map((s) => ({ label: s.label, columns: s.columns.filter((c) => c.length) }))
		.filter((s) => s.columns.length);
}

// The row-form fields the grid does not show that hold a value: what the read-only
// preview lists under a row, so nothing the card would save is out of sight.
export function rowExtraValues(table, row, parent) {
	const inGrid = new Set((table.columns || []).map((c) => c.fieldname));
	const out = [];
	for (const s of rowFormLayout(table, row, parent))
		for (const col of s.columns)
			for (const { field } of col) {
				if (inGrid.has(field.fieldname)) continue;
				const v = row[field.fieldname];
				if (v === "" || v == null) continue;
				out.push({ label: field.label, value: String(v) });
			}
	return out;
}
