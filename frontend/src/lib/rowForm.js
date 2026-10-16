// A child row's full form for the record draft card: every field Desk's row edit
// form shows (form_fields from get_doctype_form_meta), laid out in its sections and
// columns, with Desk's depends_on / mandatory_depends_on / read_only_depends_on
// rules evaluated on the row. Pure, so it is unit-testable without the view.
import { allRowFields, checkToYesNo, toPanelRow } from "@/lib/draftApply";

const NUMERIC = new Set(["Int", "Float", "Currency", "Percent", "Rating"]);

// The row as Desk's `doc` holds it, so a rule reads `doc.qty > 0`,
// `doc.is_free_item` and `doc.docstatus` as Desk does: a Check "0" is falsy, a
// number is a number, and the row's own keys (docstatus, idx, name, __islocal)
// are there. ERPNext's child rules read them often (`!doc.__islocal`,
// `doc.docstatus==0`); without them a rule would hide fields Desk shows.
export function typedRow(fields, row, { index = 0, docstatus = 0 } = {}) {
	const doc = {
		docstatus,
		idx: index + 1,
		name: row.__name || "",
		__islocal: row.__name ? 0 : 1,
	};
	for (const f of fields) {
		const v = row[f.fieldname];
		if (f.fieldtype === "Check") doc[f.fieldname] = checkToYesNo(v) === "Yes" ? 1 : 0;
		else if (NUMERIC.has(f.fieldtype)) {
			const n = Number(v);
			doc[f.fieldname] = v === "" || v == null || Number.isNaN(n) ? 0 : n;
		} else doc[f.fieldname] = v == null ? "" : v;
	}
	return doc;
}

// Compiled rules, by their text: a rule is compiled once, not on every keystroke.
const compiled = new Map();
const warned = new Set();

// Desk's rule semantics: empty means yes; "eval:<js>" runs with the row as `doc`
// and the record as `parent`; a bare fieldname means "that field has a value".
// When a rule cannot be evaluated (a syntax error, a missing object, a form-script
// "fn:" rule) the answer is `fallback`: yes for depends_on, so a field is never
// hidden by a broken rule; no for mandatory/read_only rules, so a broken rule
// never locks a field or demands a value (Desk treats a failed rule as not
// applying). The rules come from DocType meta, which only System Managers edit,
// and Desk evaluates them the same way. A failure is warned once per rule.
export function ruleHolds(rule, doc, parent, fallback = true) {
	const r = String(rule || "").trim();
	if (!r) return true;
	if (r.startsWith("eval:")) {
		try {
			let fn = compiled.get(r);
			if (!fn) {
				// eslint-disable-next-line no-new-func
				fn = new Function("doc", "parent", `return (${r.slice(5)});`);
				compiled.set(r, fn);
			}
			return !!fn(doc, parent || {});
		} catch (e) {
			if (!warned.has(r)) {
				warned.add(r);
				console.warn(`Jarvis card: could not evaluate the rule "${r}"`, e);
			}
			return fallback;
		}
	}
	if (r.startsWith("fn:")) return fallback; // a form-script method: not available here
	const v = doc[r];
	return !(v === undefined || v === null || v === "" || v === 0 || v === "0");
}

// Whether a cell holds a value worth showing: zero and an unticked Check do not.
function filled(field, v) {
	if (v === "" || v == null) return false;
	if (field.fieldtype === "Check" || NUMERIC.has(field.fieldtype)) return Number(v) !== 0;
	return true;
}

// The row form as sections of columns of fields, with each field's visibility,
// required and read-only state worked out for this row. A tab's rule applies to
// every section inside it. A field (or section) a rule would hide is still shown
// when the field holds a value, so nothing the card would save is out of sight.
// Sections and columns left with no field are dropped. `ctx` gives the row's
// index for the rules; `parent.docstatus` is the record's.
export function rowFormLayout(table, row, parent, ctx = {}) {
	const doc = typedRow(allRowFields(table), row, {
		docstatus: Number((parent || {}).docstatus || 0),
		...ctx,
	});
	const sections = [];
	let section = null;
	let tabVisible = true;
	const newSection = (label, visible) => {
		section = { label, visible: visible && tabVisible, columns: [[]] };
		sections.push(section);
	};
	for (const f of table.formFields || []) {
		if (f.fieldtype === "Tab Break") {
			tabVisible = ruleHolds(f.depends_on, doc, parent);
			newSection(f.label || "", true);
			continue;
		}
		if (f.fieldtype === "Section Break") {
			newSection(f.label || "", ruleHolds(f.depends_on, doc, parent));
			continue;
		}
		if (!section) newSection("", true);
		if (f.fieldtype === "Column Break") {
			section.columns.push([]);
			continue;
		}
		const valued = filled(f, row[f.fieldname]);
		if (!ruleHolds(f.depends_on, doc, parent) && !valued) continue;
		section.columns[section.columns.length - 1].push({
			field: f,
			reqd:
				!!f.reqd ||
				(!!f.mandatory_depends_on &&
					ruleHolds(f.mandatory_depends_on, doc, parent, false)),
			readOnly:
				!!f.read_only ||
				(!!f.read_only_depends_on &&
					ruleHolds(f.read_only_depends_on, doc, parent, false)),
			valued,
		});
	}
	return sections
		.map((s) => ({
			label: s.label,
			columns: s.columns
				.map((c) => (s.visible ? c : c.filter((i) => i.valued)))
				.filter((c) => c.length),
		}))
		.filter((s) => s.columns.length);
}

// The row-form fields the grid does not show that hold a value: what the read-only
// preview lists under a row. Zero and an unticked Check are not "a value" here, or
// every saved row would list dozens of zeros; a ticked Check reads Yes.
export function rowExtraValues(table, row, parent, ctx = {}) {
	const inGrid = new Set((table.columns || []).map((c) => c.fieldname));
	const out = [];
	for (const s of rowFormLayout(table, row, parent, ctx))
		for (const col of s.columns)
			for (const { field } of col) {
				const v = row[field.fieldname];
				if (inGrid.has(field.fieldname) || !filled(field, v)) continue;
				out.push({
					fieldname: field.fieldname,
					label: field.label,
					value: field.fieldtype === "Check" ? "Yes" : String(v),
				});
			}
	return out;
}

// For a row's toggle and form: how many of its form's fields are required and blank
// (or named missing by a failed save), and, for a saved row on an update, which
// fields differ from the saved row. `changed` counts only fields the grid does not
// show, since the grid shows its own.
export function rowStatus(table, row, parent, savedRow, ctx = {}) {
	const inGrid = new Set((table.columns || []).map((c) => c.fieldname));
	const before = savedRow ? toPanelRow(allRowFields(table), savedRow) : null;
	const serverMissing = new Set(row.__missing || []);
	const changedFields = new Set();
	let missing = 0;
	let changed = 0;
	for (const s of rowFormLayout(table, row, parent, ctx))
		for (const col of s.columns)
			for (const item of col) {
				const name = item.field.fieldname;
				const v = row[name] == null ? "" : String(row[name]);
				if (!item.readOnly && ((item.reqd && !v.trim()) || serverMissing.has(name)))
					missing++;
				if (before && !item.readOnly && v !== (before[name] ?? "")) {
					changedFields.add(name);
					if (!inGrid.has(name)) changed++;
				}
			}
	return { missing, changed, changedFields };
}
