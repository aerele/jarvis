// Desk's mandatory_depends_on for the MAIN fields of a draft form (a child row's form has its own,
// lib/rowForm.js). Only the star and the missing cue follow it: the server does not enforce the
// rule and nothing on the client blocks a save on it. A rule can answer yes, no or "cannot tell"
// (ruleState: a syntax error, a global such as a settings object, a form-script `fn:` rule).
// Pure, relative imports only (the phone shares it).
import { parentDefaults } from "./draftTables.js";
import { ruleState, typed } from "./rowForm.js";

// The values a rule may read that the form does not show: every field's static default, then the
// saved record's values (an update). Only built for a form whose meta has a main-field rule, so a
// model without one is unchanged.
export function mainRuleBase(metaFields, stored) {
	if (!(metaFields || []).some((f) => f.fieldtype !== "Table" && f.mandatory_depends_on))
		return null;
	const out = parentDefaults(metaFields);
	for (const f of metaFields) {
		if (f.fieldtype !== "Table" && stored && f.fieldname in stored)
			out[f.fieldname] = typed(f, stored[f.fieldname]);
	}
	return out;
}

// The rule's `doc`: the base, then the form's own values on top, typed as Desk's doc holds them
// (a Check is 1 or 0, a number is a number). `fields` are panel fields, or meta fields with
// `values` ({fieldname: raw value}) for a build that has no panel yet.
export function mainRuleDoc(base, fields, values) {
	const doc = { ...(base || {}) };
	for (const f of fields) doc[f.fieldname] = typed(f, values ? values[f.fieldname] : f.value);
	return doc;
}

// Yes / no / null for one main field's rule (see ruleState); `parent` is empty for a main field.
export function mainRuleState(rule, doc, fieldname = "") {
	return ruleState(rule, doc, {}, fieldname ? `main field ${fieldname} mandatory` : "");
}

// The live answer for every field of the form that has a rule: { fieldname: true | false | null }.
export function mainRuleStates(model) {
	const out = {};
	if (!model || !model.fields) return out;
	let doc = null;
	for (const f of model.fields) {
		if (!f.mandatory_depends_on) continue;
		doc = doc || mainRuleDoc(model.ruleBase, model.fields);
		out[f.fieldname] = mainRuleState(f.mandatory_depends_on, doc, f.fieldname);
	}
	return out;
}
