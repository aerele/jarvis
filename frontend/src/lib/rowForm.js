// A child row's full form for the record draft card: every field Desk's row edit
// form shows (form_fields from get_doctype_form_meta), laid out in its sections and
// columns, with Desk's depends_on / mandatory_depends_on / read_only_depends_on
// rules evaluated on the row. Pure, so it is unit-testable without the view.
import { allRowFields, checkToYesNo, ruleFetchSource, toPanelRow } from "@/lib/draftApply";

const NUMERIC = new Set(["Int", "Float", "Currency", "Percent", "Rating"]);

// One value as Desk's `doc` holds it: a Check "0" is falsy, a number is a number.
function typed(field, v) {
	if (field.fieldtype === "Check") return checkToYesNo(v) === "Yes" ? 1 : 0;
	if (NUMERIC.has(field.fieldtype)) {
		const n = Number(v);
		return v === "" || v == null || Number.isNaN(n) ? 0 : n;
	}
	return v == null ? "" : v;
}

// The row as Desk's `doc` holds it, so a rule reads `doc.qty > 0`,
// `doc.is_free_item` and `doc.docstatus` as Desk does: a Check "0" is falsy, a
// number is a number, and the row's own keys (docstatus, idx, name, __islocal,
// parent, parentfield, parenttype) are there. ERPNext's child rules read them often
// (`!doc.__islocal`, `doc.docstatus==0`); without them a rule would hide fields
// Desk shows.
// `ctx.ruleFields` are the hidden fields some rule reads (table.ruleFields); their
// values ride on the row as `__rule`, typed like any field, and go in first so a
// field of the form always wins. A value Desk fetches from a link (fetch_from) is
// the field's default once the row's link differs from the saved row's (`__ruleSrc`).
// Known limit: on a create, a hidden field Desk fills by fetch_from (is_stock_item
// from the Item) is not fetched here, so its rule reads the default and may differ
// from Desk until the record is saved.
export function typedRow(
	fields,
	row,
	{
		index = 0,
		docstatus = 0,
		ruleFields = [],
		parenttype = "",
		parentfield = "",
		parent = "",
	} = {}
) {
	const doc = {};
	const rule = row.__rule || {};
	const saved = row.__ruleSrc || {};
	for (const rf of ruleFields) {
		const from = ruleFetchSource(rf);
		const stale =
			from && from in saved && (row[from] == null ? "" : String(row[from])) !== saved[from];
		doc[rf.fieldname] = typed(
			rf,
			stale || !(rf.fieldname in rule) ? rf.default : rule[rf.fieldname]
		);
	}
	Object.assign(doc, {
		docstatus,
		idx: index + 1,
		name: row.__name || "",
		__islocal: row.__name ? 0 : 1,
		parenttype,
		parentfield,
		parent,
	});
	for (const f of fields) doc[f.fieldname] = typed(f, row[f.fieldname]);
	return doc;
}

// Desk's rule helpers (frappe/public/js/frappe/utils: datatype.js, number_format.js),
// so a rule such as `eval:in_list(["A", "B"], parent.purpose)` or `cint(doc.qty)`
// runs here as it does in the form. `flt` reads a plain number or one with "," group
// separators (Desk's default number format). Other helpers (`erpnext.*`, `frappe.*`)
// are not here: such a rule falls back, see ruleHolds.
const cstr = (s) => (s == null ? "" : s + "");
function cint(v, def) {
	if (v === true) return 1;
	if (v === false) return 0;
	v = v + "";
	if (v !== "0") v = v.replace(/^0+/, "");
	v = parseInt(v, 10);
	if (Number.isNaN(v)) v = def === undefined ? 0 : def;
	return v;
}
function flt(v, decimals) {
	if (v == null || v == "") return 0;
	if (typeof v !== "number") {
		v = parseFloat((v + "").replace(/,/g, ""));
		if (Number.isNaN(v)) v = 0;
	}
	if (decimals == null) return v;
	const m = Math.pow(10, decimals);
	return Math.round(v * m) / m;
}
const in_list = (list, item) => list.includes(item);

// Compiled rules, by their text: a rule is compiled once, not on every keystroke. A rule
// that fails to compile is cached as FAILED, so it is neither recompiled nor warned about
// again.
const compiled = new Map();
const FAILED = Symbol("rule failed to compile");
const warned = new Set();
function warnOnce(rule, label, why, err) {
	if (warned.has(rule)) return;
	warned.add(rule);
	const msg = `Jarvis card: ${why} "${rule}"${label ? ` (${label})` : ""}`;
	if (err) console.warn(msg, err);
	else console.warn(msg);
}

// Desk's rule semantics: empty means yes; "eval:<js>" runs with the row as `doc`
// and the record as `parent`; a bare fieldname means "that field has a value".
// Desk's in_list / cint / flt / cstr are available to a rule.
// When a rule cannot be evaluated (a syntax error, a missing object, a form-script
// "fn:" rule) the answer is `fallback`: yes for depends_on, so a field is never
// hidden by a broken rule; no for mandatory/read_only rules, so a broken rule
// never locks a field or demands a value (Desk treats a failed rule as not
// applying). The rules come from DocType meta, which only System Managers edit,
// and Desk evaluates them the same way. A failure is warned once per rule, naming
// `label` (the child doctype or table, the field and the rule kind) when given.
export function ruleHolds(rule, doc, parent, fallback = true, label = "") {
	const r = String(rule || "").trim();
	if (!r) return true;
	if (r.startsWith("eval:")) {
		let fn = compiled.get(r);
		if (fn === FAILED) return fallback;
		try {
			if (!fn) {
				// eslint-disable-next-line no-new-func
				fn = new Function(
					"doc",
					"parent",
					"in_list",
					"cint",
					"flt",
					"cstr",
					`return (${r.slice(5)});`
				);
				compiled.set(r, fn);
			}
			return !!fn(doc, parent || {}, in_list, cint, flt, cstr);
		} catch (e) {
			if (!fn) compiled.set(r, FAILED);
			warnOnce(r, label, "could not evaluate the rule", e);
			return fallback;
		}
	}
	if (r.startsWith("fn:")) {
		// a form-script method: not available here
		warnOnce(r, label, "cannot run the form-script rule");
		return fallback;
	}
	const v = doc[r];
	return !(v === undefined || v === null || v === "" || v === 0 || v === "0");
}

// The grid the user sees by default: the columns the form meta configured (Desk's grid
// settings). A key the agent proposed outside them is appended as a column too
// (buildDraftTable, #655), but it is still a change "outside the grid" for the toggle,
// the summary and the preview: the grid shows only the new value, the row form and the
// preview show what it was. A table built without that list (the PWA) uses its columns.
export function gridNames(table) {
	return new Set(table.gridFields || (table.columns || []).map((c) => c.fieldname));
}

// The row's "changed" set, defined once and BEFORE the layout runs: the fields the card
// treats as changed for this row, close to what Save sends (tableRowsPayload,
// draftApply.js). A row with no saved match counts every non-blank cell; a saved row
// counts the cells that differ from it. They differ in one case: a Check is compared as
// Yes/No here, so a proposed Yes over a saved 1 is not a change, while the payload still
// sends that same-value Check (1). A field a rule makes read-only still counts: only a
// static read_only column is never sent. The layout and rowStatus both consume this set.
export function rowChangedSet(table, row, savedRow) {
	const before = savedRow ? toPanelRow(allRowFields(table), savedRow) : null;
	const out = new Set();
	for (const f of allRowFields(table)) {
		if (f.read_only) continue;
		const v = row[f.fieldname] == null ? "" : String(row[f.fieldname]);
		if (before ? !sameValue(f, v, before[f.fieldname] ?? "") : v !== "") out.add(f.fieldname);
	}
	return out;
}

// One value as the card words a change: "(empty)" for blank, Yes/No for a Check, else
// the value as written (0 stays 0). The preview's `orig -> new` pair, the row form's
// "was X" and the main fields' change line all go through this.
export function changeDisplay(field, v) {
	if (field && field.fieldtype === "Check") return checkToYesNo(v);
	return v == null || String(v) === "" ? "(empty)" : String(v);
}

// A long value clipped for display ("was X"): at most CLIP_MAX characters, ending in an
// ellipsis (U+2026) when cut. The caller puts the full text in a title.
export const CLIP_MAX = 200;
export function clipText(text, max = CLIP_MAX) {
	const t = String(text ?? "");
	return t.length > max ? t.slice(0, max - 1) + "\u2026" : t;
}

// Whether a cell holds a value worth showing: zero and an unticked Check do not.
function filled(field, v) {
	if (v === "" || v == null) return false;
	// A Check holds 1/0 but the agent may send Yes/No or true/false: read it the way
	// the submit side does, or "false" and "No" would count as ticked.
	if (field.fieldtype === "Check") return checkToYesNo(v) === "Yes";
	if (NUMERIC.has(field.fieldtype)) return Number(v) !== 0;
	return true;
}

// The row form as sections of columns of fields, with each field's visibility,
// required and read-only state worked out for this row. A tab's rule applies to
// every section inside it. A field (or section) a rule would hide is still shown
// when the field holds a value, so nothing the card would save is out of sight.
// Sections and columns left with no field are dropped. `ctx` gives the row's
// index for the rules; `parent.docstatus` is the record's. The rules' `doc` also
// carries parenttype / parentfield / parent, taken from `parent` (the record) and the
// table unless `ctx` gives them.
// On a row with a matching saved row the show-if-valued rule gives way to the
// changed set (`ctx.changed`, or worked out from `ctx.savedRow`): a field a rule hides
// stays hidden while it is unchanged, as in Desk (an update does not send it), and a
// changed field is always laid out, even one that now holds 0 or blank. A field named in
// `row.__missing` (a failed save asked for it) is laid out whatever a rule says.
export function rowFormLayout(table, row, parent, ctx = {}) {
	const { changed: changedIn, savedRow, ...docCtx } = ctx;
	const changed = changedIn || (savedRow ? rowChangedSet(table, row, savedRow) : null);
	const doc = typedRow(allRowFields(table), row, {
		docstatus: Number((parent || {}).docstatus || 0),
		ruleFields: table.ruleFields || [],
		parenttype: (parent || {}).doctype || "",
		parentfield: table.fieldname || "",
		parent: (parent || {}).name || "",
		...docCtx,
	});
	const named = new Set(row.__missing || []);
	// What a rule warning names: the child doctype (else the table), the field, the kind.
	const where = table.child || table.fieldname || "row";
	const sections = [];
	let section = null;
	let tabVisible = true;
	const newSection = (label, visible, id = "") => {
		section = { id, label, visible: visible && tabVisible, columns: [[]], first: "" };
		sections.push(section);
	};
	for (const f of table.formFields || []) {
		if (f.fieldtype === "Tab Break") {
			tabVisible = ruleHolds(
				f.depends_on,
				doc,
				parent,
				true,
				`${where} tab ${f.label || ""} depends_on`
			);
			newSection(f.label || "", true, f.fieldname);
			continue;
		}
		if (f.fieldtype === "Section Break") {
			newSection(
				f.label || "",
				ruleHolds(
					f.depends_on,
					doc,
					parent,
					true,
					`${where} section ${f.label || ""} depends_on`
				),
				f.fieldname
			);
			continue;
		}
		if (!section) newSection("", true);
		if (f.fieldtype === "Column Break") {
			section.columns.push([]);
			continue;
		}
		if (!section.first) section.first = f.fieldname;
		const valued = filled(f, row[f.fieldname]);
		// Whether a field a rule hides is shown anyway: it holds a value to save (or, on
		// a saved row, a changed one), or a failed save named it, so the user can fix it.
		const shown = (changed ? changed.has(f.fieldname) : valued) || named.has(f.fieldname);
		const at = `${where}.${f.fieldname}`;
		if (!ruleHolds(f.depends_on, doc, parent, true, `${at} depends_on`) && !shown) continue;
		section.columns[section.columns.length - 1].push({
			field: f,
			reqd:
				!!f.reqd ||
				(!!f.mandatory_depends_on &&
					ruleHolds(f.mandatory_depends_on, doc, parent, false, `${at} mandatory`)),
			readOnly:
				!!f.read_only ||
				(!!f.read_only_depends_on &&
					ruleHolds(f.read_only_depends_on, doc, parent, false, `${at} read_only`)),
			valued,
			shown,
		});
	}
	return sections
		.map((s) => ({
			// A stable key for v-for (not the position, which moves when a section above
			// is hidden): the break's fieldname, else the section's first field.
			id: s.id || `sec-${s.first}`,
			label: s.label,
			columns: s.columns
				.map((c) => (s.visible ? c : c.filter((i) => i.shown)))
				.filter((c) => c.length),
		}))
		.filter((s) => s.columns.length);
}

// What the read-only preview lists under a row, for the row-form fields the grid does
// not show. On an update (`ctx.savedRow`) the fields that differ from the saved row
// come first, as `{fieldname, label, was, now, changed: true}` worded by changeDisplay
// (so a new 0, blank or unticked Check is listed); then the other filled values as
// `{fieldname, label, value}`. Zero and an unticked Check are not "a value" there, or
// every saved row would list dozens of zeros; a ticked Check reads Yes.
export function rowExtraValues(table, row, parent, ctx = {}) {
	const inGrid = gridNames(table);
	const { savedRow, ...layoutCtx } = ctx;
	const changedSet = savedRow ? rowChangedSet(table, row, savedRow) : null;
	const before = savedRow ? toPanelRow(allRowFields(table), savedRow) : null;
	const changed = [];
	const also = [];
	for (const s of rowFormLayout(table, row, parent, { ...layoutCtx, changed: changedSet }))
		for (const col of s.columns)
			for (const { field } of col) {
				const name = field.fieldname;
				if (inGrid.has(name)) continue;
				const v = row[name];
				if (changedSet && changedSet.has(name)) {
					changed.push({
						fieldname: name,
						label: field.label,
						was: changeDisplay(field, before[name]),
						now: changeDisplay(field, v),
						changed: true,
					});
				} else if (filled(field, v)) {
					also.push({
						fieldname: name,
						label: field.label,
						value: field.fieldtype === "Check" ? checkToYesNo(v) : String(v),
					});
				}
			}
	return [...changed, ...also];
}

// Whether a cell's current value equals the saved one. A Check compares as Yes/No
// on both sides, so a proposed "Yes" over a saved "1" is not a change.
function sameValue(field, now, was) {
	if (field.fieldtype === "Check") return checkToYesNo(now) === checkToYesNo(was);
	return now === was;
}

// For a row's toggle and form: how many fields a failed save named missing are still
// blank, and, for a saved row on an update, which fields differ from the saved row
// (rowChangedSet). `changed` counts only fields the grid does not show, since the grid
// shows its own. `changedFields` is empty for a row with no saved match.
// A required blank alone is not missing: ERPNext fills many of them on save (a row's
// item name and UOM from its item), so flagging each would warn about fields nobody
// needs to touch; its label keeps the star. A named field counts whether the form
// shows it, shows it read-only, or lists it under "Also needed", so no rule is
// evaluated here (`parent` and `ctx` are kept for callers).
export function rowStatus(table, row, parent, savedRow, ctx = {}) {
	const inGrid = gridNames(table);
	const changedFields = savedRow ? rowChangedSet(table, row, savedRow) : new Set();
	const missing = [...new Set(row.__missing || [])].filter(
		(n) => !String(row[n] == null ? "" : row[n]).trim()
	).length;
	let changed = 0;
	for (const name of changedFields) if (!inGrid.has(name)) changed++;
	return { missing, changed, changedFields };
}
