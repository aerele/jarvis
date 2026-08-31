// Pure helpers for the record draft-panel apply + render path (ChatView.vue
// applyDraft / draft template). Extracted so the read_only / create gating, value
// coercion, and read-only display are unit-testable without mounting the view
// (mirrors chatAction.js / actionSummary.js).

// Which raw values count as a checked Check. Single source of truth for both the
// display side (checkToYesNo, seeding the "Yes"/"No" control) and the submit side
// (coerceCheck) so the two can never drift.
const CHECK_TRUE = ["1", 1, "yes", "true", true, "on"];

// Normalize a raw Check value to the "Yes"/"No" string the panel control binds to.
export function checkToYesNo(v) {
	return CHECK_TRUE.includes(typeof v === "string" ? v.toLowerCase() : v) ? "Yes" : "No";
}

// read_only is a Frappe form-UI flag, not a write barrier: the agent's direct
// create_doc sets read_only fields too (e.g. Error Log method/error). A read_only
// MAIN field is therefore submitted only when the agent PROPOSED a value for it on
// a CREATE. Gate on `proposed`, not mere non-emptiness: an unproposed required
// read_only Check renders as "No"/0 and would otherwise clobber a schema default of
// 1. UPDATE always strips read_only (a confirm card must not overwrite a
// server-managed field on an existing doc).
export function isFieldWritable(field, verb) {
	return !field.read_only || (verb === "create" && !!field.proposed);
}

// Whether a main field should paint the required "missing" cue. Read-only fields are
// excluded: the user can't fill them, so the amber "fill me" prompt (and the earlier
// editable-input-that-discards-your-value trap) must not fire on them.
export function isFieldMissing(field) {
	return !!field.reqd && !field.read_only && !String(field.value ?? "").trim();
}

// Display form of a read-only main field's value in the muted static span. Empty ->
// an em-dash so the cell reads as "no value yet" rather than blank/broken; a datetime
// swaps the input-format "T" separator for a space (the raw value is machine-format
// for the native picker, which the span has no chrome to reformat).
export function readonlyDisplay(field) {
	const v = field.value == null ? "" : String(field.value);
	if (v === "") return "—";
	if (field.control === "datetime") return v.replace("T", " ");
	return v;
}

// A main-field panel value -> its create_doc/update_doc payload form.
export function coerceOut(field) {
	if (field.control === "check") return field.value === "Yes" ? 1 : 0;
	if (field.control === "number") return field.value === "" ? "" : Number(field.value);
	return field.value;
}

function coerceCheck(v) {
	return checkToYesNo(v) === "Yes" ? 1 : 0;
}

// A child-table row -> the submittable subset. On CREATE a read_only column that
// carries a value is filled; every other verb strips read_only. Unlike the main
// field there is no per-cell `proposed` flag - child rows never carry schema
// defaults (an unproposed cell is "" and is skipped below), so non-emptiness stands
// in for `proposed` and no default-clobber guard is needed here. A Check cell holds
// a raw value (not the "Yes"/"No" string the main control uses), so normalize it via
// checkToYesNo rather than Number(v) - which turns "Yes"/"true" -> 0.
export function coerceRow(table, row, verb) {
	const out = {};
	for (const c of table.columns) {
		if (c.read_only && verb !== "create") continue;
		let v = row[c.fieldname];
		if (v === "" || v == null) continue;
		if (["Int", "Float", "Currency", "Percent"].includes(c.fieldtype)) v = Number(v);
		if (c.fieldtype === "Check") v = coerceCheck(v);
		out[c.fieldname] = v;
	}
	return out;
}
