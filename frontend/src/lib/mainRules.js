// Desk's mandatory_depends_on for the MAIN fields of a draft form (a child row's form has its own,
// lib/rowForm.js). Only the star and the missing cue follow it: the server does not enforce the
// rule and nothing on the client blocks a save on it. A rule can answer yes, no or "cannot tell"
// (ruleState: a syntax error, a global such as a settings object, a form-script `fn:` rule).
//
// A field that is on the form only because of its rule is marked `ruleAdded`: it is sent only
// when the person changed it (draftApply.draftValues, draftCardValues), never for the value it
// was shown with. Pure, relative imports only (the phone shares it).
import { allRowFields, checkToYesNo } from "./draftApply.js";
import { isReal, normCell } from "./draftCardValues.js";
import { panelField } from "./docFields.js";
import { parentDefaults } from "./draftTables.js";
import { ruleHolds, ruleState, typed } from "./rowForm.js";

// The tables a rule of this form reads: a `doc.<table>` (or `doc["<table>"]`) in the
// mandatory_depends_on or depends_on of a main field that has a mandatory rule. Only these tables
// go into the rule doc, so a form whose rules name none never walks a row.
const DOC_REF = /\bdoc\s*(?:\??\.\s*([A-Za-z_]\w*)|\[\s*["']([^"']+)["']\s*\])/g;
export function ruleTableNames(metaFields) {
	const tables = new Set(
		(metaFields || []).filter((f) => f.fieldtype === "Table").map((f) => f.fieldname)
	);
	const out = new Set();
	for (const f of metaFields || []) {
		if (f.fieldtype === "Table" || !f.mandatory_depends_on) continue;
		for (const rule of [f.mandatory_depends_on, f.depends_on]) {
			for (const m of String(rule || "").matchAll(DOC_REF)) {
				const name = m[1] || m[2];
				if (tables.has(name)) out.add(name);
			}
		}
	}
	return [...out];
}

// The values a rule may read that the form does not show: every field's static default, then the
// saved record's values (an update), and the saved rows of the tables the rules name (an empty
// list when it has none). Only built for a form whose meta has a main-field rule, so a model
// without one is unchanged.
export function mainRuleBase(metaFields, stored, storedTables, tableNames = []) {
	if (!(metaFields || []).some((f) => f.fieldtype !== "Table" && f.mandatory_depends_on))
		return null;
	const out = parentDefaults(metaFields);
	for (const f of metaFields) {
		if (f.fieldtype !== "Table" && stored && f.fieldname in stored)
			out[f.fieldname] = typed(f, stored[f.fieldname]);
	}
	for (const name of tableNames) out[name] = (storedTables || {})[name] || [];
	return out;
}

// One named table's rows as a rule reads them: the rows the save would count (the same test as
// the send path, so a blank placeholder row is not a row), each cell typed like a field.
function ruleRows(table) {
	const cols = allRowFields(table);
	return (table.rows || [])
		.filter((r) => isReal(table, r))
		.map((r) => Object.fromEntries(cols.map((c) => [c.fieldname, typed(c, r[c.fieldname])])));
}

// {table: rows} for the built tables the rules name; a table the form does not hold keeps what
// the base has (its saved rows).
export function ruleTables(names, tables) {
	const out = {};
	for (const t of tables || [])
		if (t && (names || []).includes(t.fieldname)) out[t.fieldname] = ruleRows(t);
	return out;
}

// The rule's `doc`: the base, then the form's own values on top, typed as Desk's doc holds them
// (a Check is 1 or 0, a number is a number), then the named tables' rows. `fields` are panel
// fields, or meta fields with `values` ({fieldname: raw value}) for a build that has no panel yet.
export function mainRuleDoc(base, fields, values, tables) {
	const doc = { ...(base || {}) };
	for (const f of fields) doc[f.fieldname] = typed(f, values ? values[f.fieldname] : f.value);
	return Object.assign(doc, tables || {});
}

const modelRuleDoc = (model) =>
	mainRuleDoc(model.ruleBase, model.fields, null, ruleTables(model.ruleTables, model.tables));

// Yes / no / null for one main field's rule (see ruleState); `parent` is empty for a main field.
export function mainRuleState(rule, doc, fieldname = "") {
	return ruleState(rule, doc, {}, fieldname ? `main field ${fieldname} mandatory` : "");
}

// Desk hides a field whose depends_on is false; a rule that cannot be read counts as shown.
export function mainFieldShown(f, doc) {
	return ruleHolds(f.depends_on, doc, {}, true, `main field ${f.fieldname} depends_on`);
}

// The live answer for every field of the form that has a rule: { fieldname: true | false | null }.
export function mainRuleStates(model) {
	const out = {};
	if (!model || !model.fields) return out;
	let doc = null;
	for (const f of model.fields) {
		if (!f.mandatory_depends_on) continue;
		doc = doc || modelRuleDoc(model);
		out[f.fieldname] = mainRuleState(f.mandatory_depends_on, doc, f.fieldname);
	}
	return out;
}

// Whether a rule makes a field part of the form: its rule state is true (or null, "cannot tell",
// when `unknownToo`: the build shows such a field, a later reveal waits for a yes) and Desk
// would not hide it (depends_on). One answer for the build and the reveal.
export function ruleAdmits(f, doc, unknownToo) {
	if (!f.mandatory_depends_on || f.read_only) return false;
	const state = mainRuleState(f.mandatory_depends_on, doc, f.fieldname);
	return (state === true || (unknownToo && state === null)) && mainFieldShown(f, doc);
}

// One main field of the form from its meta field. `value` is what it shows, `stored` the saved
// record's value (the baseline of an update's change mark).
export function modelField(f, value, stored, verb, { proposed, ruleAdded }) {
	const pf = panelField(f, value);
	pf.proposed = proposed;
	if (ruleAdded) pf.ruleAdded = true; // shown only for its mandatory rule
	if (verb === "update")
		pf.orig =
			stored == null ? "" : String(pf.control === "check" ? checkToYesNo(stored) : stored);
	pf.changed = verb === "update" && String(pf.value) !== String(pf.orig);
	return pf;
}

// What a watcher reads to know a reveal may be due: null when the model has no candidate.
export function revealKey(model) {
	return model && model.ruleFields && model.ruleFields.length
		? JSON.stringify(modelRuleDoc(model))
		: null;
}

// The candidates the person's edits may make required later (model.ruleFields: {field, value}
// with the meta field and its stored value): a candidate whose rule now holds (exactly true) and
// whose depends_on is not false joins the form, marked `ruleAdded`, and leaves the list. It stays
// when its rule turns false again. For the form of a server draft its snapshot takes the value it
// was shown with, so it is neither a change nor sent until the person edits it. Returns how many
// were added.
export function revealRuleFields(model) {
	const cands = model && model.ruleFields;
	if (!cands || !cands.length) return 0;
	const doc = modelRuleDoc(model);
	let added = 0;
	for (const c of [...cands]) {
		const f = c.field;
		if (!ruleAdmits(f, doc, false)) continue;
		const pf = modelField(f, c.value, c.value, model.verb, {
			proposed: false,
			ruleAdded: true,
		});
		model.fields.push(pf);
		if (model.snapshot) {
			model.snapshot.fields[pf.fieldname] = normCell(pf.fieldtype, pf.value);
			model.snapshot.types[pf.fieldname] = pf.fieldtype;
		}
		cands.splice(cands.indexOf(c), 1);
		added++;
	}
	return added;
}
