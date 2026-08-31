// Pure helpers for the record draft-panel apply path (ChatView.vue applyDraft).
// Extracted so the read_only / create gating and value coercion are unit-testable
// without mounting the view (mirrors chatAction.js / actionSummary.js).

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

// A main-field panel value -> its create_doc/update_doc payload form.
export function coerceOut(field) {
	if (field.control === "check") return field.value === "Yes" ? 1 : 0;
	if (field.control === "number") return field.value === "" ? "" : Number(field.value);
	return field.value;
}

const CHECK_TRUE = ["1", 1, "yes", "true", true, "on"];

function coerceCheck(v) {
	return CHECK_TRUE.includes(typeof v === "string" ? v.toLowerCase() : v) ? 1 : 0;
}

// A child-table row -> the submittable subset. On CREATE a read_only column that
// carries a value is filled; every other verb strips read_only. Unlike the main
// field there is no per-cell `proposed` flag - child rows never carry schema
// defaults (an unproposed cell is "" and is skipped below), so non-emptiness stands
// in for `proposed` and no default-clobber guard is needed here. A Check cell holds
// a raw value (not the "Yes"/"No" string the main control uses), so normalize it the
// same way _checkToYesNo does rather than Number(v) - which turns "Yes"/"true" -> 0.
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
