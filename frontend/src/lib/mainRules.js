// Desk's mandatory_depends_on for the MAIN fields of a draft form (a child row's form has its own,
// lib/rowForm.js). Only the star and the missing cue follow it: the server does not enforce the
// rule and nothing on the client blocks a save on it. A rule can answer yes, no or "cannot tell"
// (ruleState: a syntax error, a global such as a settings object, a form-script `fn:` rule).
//
// A field that is on the form only because of its rule is marked `ruleAdded`: it is sent only
// when the person changed it (draftApply.draftValues, draftCardValues), never for the value it
// was shown with. Pure, relative imports only (the phone shares it).
import { checkToYesNo } from "./draftApply.js";
import { normCell } from "./draftCardValues.js";
import { panelField } from "./docFields.js";
import { parentDefaults } from "./draftTables.js";
import { ruleHolds, ruleState, typed } from "./rowForm.js";

// The values a rule may read that the form does not show: every field's static default, then the
// saved record's values (an update), and each table's saved rows (an empty list when it has none:
// a rule such as `!doc.links?.length` reads one). Only built for a form whose meta has a main-field
// rule, so a model without one is unchanged.
export function mainRuleBase(metaFields, stored, storedTables) {
	if (!(metaFields || []).some((f) => f.fieldtype !== "Table" && f.mandatory_depends_on))
		return null;
	const out = parentDefaults(metaFields);
	for (const f of metaFields) {
		if (f.fieldtype === "Table") out[f.fieldname] = (storedTables || {})[f.fieldname] || [];
		else if (stored && f.fieldname in stored) out[f.fieldname] = typed(f, stored[f.fieldname]);
	}
	return out;
}

// A table's rows as a rule reads them: the row's field values, no private `__` keys.
const ruleRows = (rows) =>
	(rows || []).map((r) =>
		Object.fromEntries(Object.entries(r || {}).filter(([k]) => !k.startsWith("__")))
	);

// The rule's `doc`: the base, then the form's own values on top, typed as Desk's doc holds them
// (a Check is 1 or 0, a number is a number), then each table's current rows. `fields` are panel
// fields, or meta fields with `values` ({fieldname: raw value}) for a build that has no panel yet;
// `tables` is {fieldname: rows}.
export function mainRuleDoc(base, fields, values, tables) {
	const doc = { ...(base || {}) };
	for (const f of fields) doc[f.fieldname] = typed(f, values ? values[f.fieldname] : f.value);
	for (const [name, rows] of Object.entries(tables || {})) doc[name] = ruleRows(rows);
	return doc;
}

const tableRowsOf = (model) =>
	Object.fromEntries((model.tables || []).map((t) => [t.fieldname, t.rows]));
const modelRuleDoc = (model) =>
	mainRuleDoc(model.ruleBase, model.fields, null, tableRowsOf(model));

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
		if (mainRuleState(f.mandatory_depends_on, doc, f.fieldname) !== true) continue;
		if (!mainFieldShown(f, doc)) continue;
		const pf = panelField(f, c.value);
		pf.proposed = false;
		pf.ruleAdded = true;
		if (model.verb === "update")
			pf.orig =
				c.value == null
					? ""
					: String(pf.control === "check" ? checkToYesNo(c.value) : c.value);
		pf.changed = model.verb === "update" && String(pf.value) !== String(pf.orig);
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
