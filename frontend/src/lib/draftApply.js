// Pure helpers for the record draft-panel apply + render path (ChatView.vue
// applyDraft / draft template). Extracted so the read_only / create gating, value
// coercion, and read-only display are unit-testable without mounting the view
// (mirrors chatAction.js / actionSummary.js). Also holds the child-table helpers:
// saved rows keep their `name`, and an Update sends existing rows by name with
// only their changed cells, so the server never drops a field the card does not
// show (CR-3). No imports: the PWA shares this file through actionSummary.js.

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
// `serverMissing` is a field a failed create named (see docFields.markMissing), which
// meta may not mark required (mandatory_depends_on).
export function isFieldMissing(field) {
	return (
		(!!field.reqd || !!field.serverMissing) &&
		!field.read_only &&
		!String(field.value ?? "").trim()
	);
}

// Display form of a read-only main field's value in the muted static span. Empty ->
// an em-dash so the cell reads as "no value yet" rather than blank/broken; a datetime
// swaps the input-format "T" separator for a space (the raw value is machine-format
// for the native picker, which the span has no chrome to reformat).
export function readonlyDisplay(field) {
	const v = field.value == null ? "" : String(field.value);
	if (v === "") return "-";
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
	// A saved row's name rides along so update_doc edits THAT row and keeps every
	// field the grid does not show (CR-3). A new row has none.
	const out = row.__name ? { name: row.__name } : {};
	for (const c of table.columns) {
		if (c.read_only && verb !== "create") continue;
		const v = row[c.fieldname];
		if (v === "" || v == null) continue;
		out[c.fieldname] = coerceCell(c, v);
	}
	return out;
}

function coerceCell(column, v) {
	if (["Int", "Float", "Currency", "Percent"].includes(column.fieldtype)) return Number(v);
	if (column.fieldtype === "Check") return coerceCheck(v);
	return v;
}

// A saved or proposed child row -> its panel row: the grid columns as strings (dates
// in the input format) plus the row's name kept aside as __name. The panel rows AND
// the untouched baseline go through this, so an unedited table compares equal
// (a Datetime column used to differ and resend - and wipe - the whole table).
export function toPanelRow(columns, src) {
	const out = src && src.name ? { __name: src.name } : {};
	for (const c of columns)
		out[c.fieldname] = normDateVal(
			c.fieldtype,
			src[c.fieldname] == null ? "" : String(src[c.fieldname])
		);
	return out;
}

// The agent may propose a kept row as just its `name` plus the cells it changes.
// Lay each such row over the saved row of the same name, so the panel shows (and
// Apply compares) the full row instead of blanking every column it left out.
// A name that matches no saved row is kept as proposed; the server refuses it.
export function overlaySavedRows(proposedRows, savedRows) {
	const saved = new Map();
	for (const r of savedRows || []) if (r && r.name) saved.set(r.name, r);
	return (proposedRows || []).map((r) =>
		r && r.name && saved.has(r.name) ? { ...saved.get(r.name), ...r } : r
	);
}

// Row keys that are never a grid column: the row's own id, the keys Frappe owns on
// a child row (the server drops them too) and `__` internals.
const ROW_SYSTEM_KEYS = new Set([
	"name",
	"idx",
	"parent",
	"parentfield",
	"parenttype",
	"doctype",
	"docstatus",
	"owner",
	"creation",
	"modified",
	"modified_by",
]);
export function isRowKeyColumn(key) {
	return !ROW_SYSTEM_KEYS.has(key) && !String(key).startsWith("__");
}

// Saved rows an update would delete: every loaded row (origJson) whose name no
// panel row carries any more. Returned as short labels (first shown column's value,
// else the row id) so the card can say WHICH rows go, not only how many (CR-3).
export function removedSavedRows(table) {
	let saved = [];
	try {
		saved = JSON.parse(table.origJson || "null") || [];
	} catch {
		saved = [];
	}
	const kept = new Set((table.rows || []).map((r) => r.__name).filter(Boolean));
	const first = (table.columns || []).find((c) => !c.read_only) || (table.columns || [])[0];
	return saved
		.filter((r) => r && r.name && !kept.has(r.name))
		.map((r) => String((first && r[first.fieldname]) || r.name));
}

// Whether the user changed a loaded table (rows, cells, order or count).
export function tableChanged(table, originalRows) {
	const now = table.rows
		.map((r) => coerceRow(table, r, "update"))
		.filter((r) => Object.keys(r).length);
	const before = (originalRows || []).map((r) =>
		coerceRow(table, toPanelRow(table.columns, r), "update")
	);
	return JSON.stringify(now) !== JSON.stringify(before);
}

// Native date/time inputs REQUIRE canonical values (yyyy-mm-dd / yyyy-mm-ddThh:mm);
// anything else — "2026-07-10 00:00:00", "10-07-2026" — renders the input EMPTY,
// which read as "the date isn't picking". Normalize whatever the agent/doc gave us.
export function normDateVal(fieldtype, v) {
	const s = String(v == null ? "" : v).trim();
	if (!s) return s;
	if (fieldtype === "Date") {
		let m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
		if (m) return `${m[1]}-${m[2]}-${m[3]}`;
		m = s.match(/^(\d{2})[-/](\d{2})[-/](\d{4})$/); // dd-mm-yyyy / dd/mm/yyyy
		if (m) return `${m[3]}-${m[2]}-${m[1]}`;
	}
	if (fieldtype === "Datetime") {
		let m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
		if (m) return `${m[1]}T${m[2]}`;
		m = s.match(/^(\d{4}-\d{2}-\d{2})$/);
		if (m) return `${m[1]}T00:00`;
	}
	if (fieldtype === "Time") {
		const m = s.match(/^(\d{2}:\d{2})/);
		if (m) return m[1];
	}
	return s;
}

// The update payload for a changed table. A saved row sends its name plus ONLY the
// cells the user changed (a cleared cell as null, so it really clears; an untouched
// Datetime is never resent, so its seconds survive). A new row sends its filled
// cells. update_doc keeps every other field of a named row (CR-3).
export function tableRowsPayload(table, originalRows) {
	const saved = new Map();
	for (const r of originalRows || [])
		if (r && r.name) saved.set(r.name, toPanelRow(table.columns, r));
	const out = [];
	for (const row of table.rows) {
		const before = row.__name ? saved.get(row.__name) : null;
		if (!before) {
			const r = coerceRow(table, row, "update");
			if (Object.keys(r).length) out.push(r);
			continue;
		}
		const r = { name: row.__name };
		for (const c of table.columns) {
			if (c.read_only) continue;
			const v = row[c.fieldname] == null ? "" : String(row[c.fieldname]);
			if (v === before[c.fieldname]) continue;
			r[c.fieldname] = v === "" ? null : coerceCell(c, v);
		}
		out.push(r);
	}
	return out;
}
