// filterModel - the browser half of the plan-08 filter contract.
//
// Everything here is pure: the clause shape, which value control a field family
// wants, when a clause is COMPLETE enough to send, the versioned URL payload,
// and the mapping from the server's stable error codes to copy. FilterGroup.vue
// renders it and useListPage owns it; both are testable because none of it
// touches Vue, the router or the network.
//
// The authority is the server schema (jarvis/chat/list_filters.py) — never this
// file. We only ever read `operators` / `default_operator` / `fieldtype` /
// `options` off a schema entry; a clause the browser thinks is fine is still
// re-validated server-side, and a clause the browser cannot describe is dropped
// rather than guessed at.

/** Bumped with the wire shape of the URL payload (server CONTRACT_VERSION is separate). */
export const URL_CONTRACT_VERSION = 1;
/** One versioned query param carries the whole clause list (plan §8). */
export const URL_PARAM = "fv2";

// Client-side mirrors of list_filters.py's §9 caps. The server enforces them;
// these stop us building a request it will certainly reject, and bound what a
// hand-edited URL can inflate into memory.
export const DEFAULT_LIMITS = {
	max_clauses: 20,
	max_in_values: 100,
	max_value_chars: 1000,
};
/** A whole `fv2=` payload longer than this is treated as junk, not parsed. */
export const MAX_URL_CHARS = 8000;

// Operator vocabulary, labelled as Frappe labels them. The schema decides WHICH
// of these a field offers; this map only supplies the words. Its KEYS are also
// the whole operator vocabulary — a URL naming anything else is not a clause we
// can render, whatever the schema says.
export const OPERATOR_LABELS = {
	"=": "equals",
	"!=": "not equals",
	like: "like",
	"not like": "not like",
	in: "in",
	"not in": "not in",
	is: "is",
	">": "greater than",
	"<": "less than",
	">=": "greater than or equals",
	"<=": "less than or equals",
	Between: "between",
	Timespan: "timespan",
};

// Frappe's date/datetime comparison labels (filter.js uses these words for the
// temporal families so ">" does not read as arithmetic on a date).
const TEMPORAL_OPERATOR_LABELS = {
	">": "after",
	"<": "before",
	">=": "on or after",
	"<=": "on or before",
};

/** Every operator the wire understands. Derived, so the two cannot drift. */
export const OPERATORS = Object.freeze(Object.keys(OPERATOR_LABELS));

/** frappe.utils.data.get_timespan_date_range's accepted tokens, in its own order. */
export const TIMESPANS = [
	"last 7 days",
	"last 14 days",
	"last 30 days",
	"last 90 days",
	"last week",
	"last month",
	"last quarter",
	"last 6 months",
	"last year",
	"yesterday",
	"today",
	"tomorrow",
	"this week",
	"this month",
	"this quarter",
	"this year",
	"next week",
	"next month",
	"next quarter",
	"next 6 months",
	"next year",
];

const NUMERIC_FIELDTYPES = new Set(["Int", "Float", "Currency", "Percent", "Duration", "Rating"]);

/** "last 7 days" → "Last 7 days". The WIRE value is never touched. */
export function timespanLabel(token) {
	const text = String(token || "");
	return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

export function operatorLabel(operator, fieldtype) {
	if ((fieldtype === "Date" || fieldtype === "Datetime") && TEMPORAL_OPERATOR_LABELS[operator]) {
		return TEMPORAL_OPERATOR_LABELS[operator];
	}
	return OPERATOR_LABELS[operator] || operator;
}

// ── schema index ────────────────────────────────────────────────────────────
// A field is identified by (doctype, fieldname), exactly as Frappe does, because
// a child field's fieldname alone is not unique against the parent's.
const SEP = "␟";

export function fieldKey(doctype, fieldname) {
	return `${doctype || ""}${SEP}${fieldname || ""}`;
}

/**
 * The catalog as a lookup, or NULL when there is no catalog to look in. The
 * distinction is load-bearing: null means "we do not know", which several
 * helpers below treat differently from "we know, and this field is not in it".
 */
export function schemaIndex(schema) {
	if (!schema || !Array.isArray(schema.fields)) return null;
	const index = new Map();
	for (const entry of schema.fields) {
		index.set(fieldKey(entry.doctype, entry.fieldname), entry);
	}
	return index;
}

export function entryFor(index, clause) {
	if (!index || !clause) return null;
	return index.get(fieldKey(clause.doctype, clause.fieldname)) || null;
}

export function limitsOf(schema) {
	const limits = (schema && schema.limits) || {};
	return {
		max_clauses: limits.max_clauses || DEFAULT_LIMITS.max_clauses,
		max_in_values: limits.max_in_values || DEFAULT_LIMITS.max_in_values,
		max_value_chars: limits.max_value_chars || DEFAULT_LIMITS.max_value_chars,
	};
}

/**
 * Field-picker options grouped the way Frappe's FieldSelect groups them: the
 * root DocType's own fields first, then one group per child DocType. The child
 * labels already arrive as "Label (Child DocType)" from the server.
 */
export function fieldOptions(schema) {
	const rootLabel = (schema && (schema.label || schema.root_doctype)) || "Fields";
	const groups = [];
	const byGroup = new Map();
	for (const entry of (schema && schema.fields) || []) {
		// The server names the section: the list's own name for its fields, the
		// PARENT's Table-field label for a child table ("Sources", not
		// "Jarvis Dashboard Source"), and a separate section for the generic
		// standard fields. Falling back to the old derivation keeps an older
		// server's schema renderable.
		const group = entry.group || (entry.is_child ? entry.doctype : rootLabel);
		if (!byGroup.has(group)) {
			const bucket = { group, items: [] };
			byGroup.set(group, bucket);
			groups.push(bucket);
		}
		byGroup.get(group).items.push({
			label: entry.label || entry.fieldname,
			value: fieldKey(entry.doctype, entry.fieldname),
			description: entry.fieldname,
		});
	}
	return groups;
}

// ── clause shape ────────────────────────────────────────────────────────────
let seq = 0;

/** A stable per-row key so Vue does not reuse a row's DOM across a removal. */
export function nextClauseId() {
	seq += 1;
	return `fc${seq}`;
}

/**
 * The empty value for an operator/family pair. Shape matters: `in` holds a real
 * list (deviation D10 — never a comma string), `Between` holds exactly two
 * bounds (D14-a — both are required, so a half-filled row stays PENDING and is
 * never sent).
 */
export function emptyValueFor(operator) {
	if (operator === "in" || operator === "not in") return [];
	if (operator === "Between") return ["", ""];
	if (operator === "is") return "set";
	return "";
}

export function makeClause({ doctype, fieldname, operator, value, display }) {
	return {
		id: nextClauseId(),
		doctype: doctype || "",
		fieldname: fieldname || "",
		operator: operator || "",
		value: value === undefined ? emptyValueFor(operator) : value,
		// UI-only: the Link title we last saw for this name. Never serialized —
		// plan §8 forbids treating a label as authority.
		display: display || null,
	};
}

/** A new row for `entry`, seeded with the SERVER's default operator (D5/D5-a). */
export function clauseForEntry(entry) {
	const operator = entry.default_operator || (entry.operators || [])[0] || "=";
	return makeClause({
		doctype: entry.doctype,
		fieldname: entry.fieldname,
		operator,
		value: emptyValueFor(operator),
	});
}

/**
 * Re-point a clause at another field: the operator falls back to the new
 * field's default whenever the old one is not in its allowed set, and the value
 * is reset because a Link name is not a date is not a Check.
 */
export function retargetClause(clause, entry) {
	const operators = entry.operators || [];
	const operator = operators.includes(clause.operator)
		? clause.operator
		: entry.default_operator || operators[0] || "=";
	return {
		...clause,
		doctype: entry.doctype,
		fieldname: entry.fieldname,
		operator,
		value: emptyValueFor(operator),
		display: null,
	};
}

/**
 * Switch operators, keeping the value only when the two operators want the SAME
 * value shape. Going scalar → `in` promotes the value into a one-element list
 * (which is what the user means); anything else starts empty rather than
 * pretending "2026-01-01" is a valid Check.
 */
export function setOperator(clause, operator) {
	const from = valueShape(clause.operator);
	const to = valueShape(operator);
	let value;
	if (from === to) {
		value = clause.value;
	} else if (from === "scalar" && to === "list") {
		value = isBlank(clause.value) ? [] : [String(clause.value)];
	} else if (from === "list" && to === "scalar") {
		const first = (Array.isArray(clause.value) ? clause.value : []).find((v) => !isBlank(v));
		value = first === undefined ? "" : String(first);
	} else {
		value = emptyValueFor(operator);
	}
	return { ...clause, operator, value };
}

function valueShape(operator) {
	if (operator === "in" || operator === "not in") return "list";
	if (operator === "Between") return "pair";
	if (operator === "is") return "token";
	return "scalar";
}

// ── value controls ──────────────────────────────────────────────────────────
/**
 * Which control renders a clause's value. Operator wins over fieldtype (an `is`
 * filter is a set/not-set choice whatever the family, and `Between` is always a
 * range) — mirroring how Frappe's Filter picks its control.
 */
export function controlFor(entry, operator) {
	if (!entry) return "text";
	const fieldtype = entry.fieldtype;
	if (operator === "is") return "is";
	if (operator === "Timespan") return "timespan";
	if (operator === "Between") {
		if (fieldtype === "Datetime") return "between-datetime";
		if (fieldtype === "Date") return "between-date";
		// P0-03: the server never offers Between on a non-temporal field (its
		// effective-Data operator set removes it), so a clause that still carries
		// Between on one can only be a hand-edited URL or a stale link. Reconcile
		// drops it; this is the belt — NEVER render a date range over a text/number
		// column, fall through to the field's own control instead.
		return "text";
	}
	if (operator === "in" || operator === "not in") {
		if (fieldtype === "Link") return "multi-link";
		if (fieldtype === "Select" && selectOptions(entry)) return "multi-select";
		return "multi";
	}
	if (operator === "like" || operator === "not like") return "text";
	if (fieldtype === "Check") return "check";
	if (fieldtype === "Select" && selectOptions(entry)) return "select";
	if (fieldtype === "Link") return "link";
	if (fieldtype === "Date") return "date";
	if (fieldtype === "Datetime") return "datetime";
	if (fieldtype === "Time") return "time";
	if (NUMERIC_FIELDTYPES.has(fieldtype)) return "number";
	return "text";
}

/**
 * A Select's metadata options, or null when they cannot be enumerated
 * (`link:`-form options are resolved server-side at render time, so the control
 * degrades to free text rather than showing a lie).
 *
 * A LEADING BLANK is a real option meaning "not set" — Frappe writes it as an
 * empty first line — so it is kept and labelled, never dropped and never
 * confused with "no filter" (MIGRATION-CHECKLIST §7).
 */
export function selectOptions(entry) {
	const raw = (entry && entry.options) || "";
	if (!entry || entry.fieldtype !== "Select" || !raw || raw.startsWith("link:")) return null;
	return raw.split("\n").map((o) => o.trim());
}

export function selectControlOptions(entry) {
	const options = selectOptions(entry) || [];
	return options.map((o) => ({ label: o === "" ? "Not set" : o, value: o }));
}

/** The DocType a Link value control should search, or "" when it cannot know. */
export function linkTarget(entry) {
	if (!entry || entry.fieldtype !== "Link") return "";
	// Dynamic Link's `options` names the CONTROLLING FIELD, not a DocType, so it
	// has no searchable target until the panel reads that field's value — until
	// then it renders as text (MIGRATION-CHECKLIST §7). Not a deviation, an
	// unbuilt affordance.
	return entry.options || "";
}

// ── datetime-local ⇄ Frappe's datetime string ───────────────────────────────
// `<input type="datetime-local">` speaks ISO with a `T` and no seconds
// ("2026-01-01T09:30"). Frappe's DATETIME_FORMAT is "%Y-%m-%d %H:%M:%S", and the
// mismatch is NOT cosmetic: `_between_bounds` only calls `get_datetime()` when
// the bound contains a SPACE, so a `T`-form bound falls through to `getdate()`,
// which parses the date and DISCARDS the time — a Between from 09:00 would
// silently become one from midnight. Both bounds are converted here, once, so no
// control has to remember.
const DATETIME_INPUT_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?/;

/** Frappe's "YYYY-MM-DD HH:mm:ss" for whatever the input produced. */
export function fromDatetimeInput(value) {
	const match = DATETIME_INPUT_RE.exec(String(value || "").trim());
	if (!match) return isBlank(value) ? "" : String(value);
	return `${match[1]} ${match[2]}:${match[3] || "00"}`;
}

/** The `T`-form the input needs to DISPLAY a stored value. */
export function toDatetimeInput(value) {
	const match = DATETIME_INPUT_RE.exec(String(value || "").trim());
	if (!match) return "";
	return `${match[1]}T${match[2]}`;
}

// ── completeness ────────────────────────────────────────────────────────────
export function isBlank(value) {
	return value === null || value === undefined || String(value).trim() === "";
}

/**
 * A clause is COMPLETE when it is worth sending. Everything else is a pending
 * row: it stays on screen, it is not counted, and it is not in the request.
 *
 * This is the rule that keeps the two server deviations from ever reaching a
 * user as an error. D14-a rejects a `Between` with one bound, so a half-filled
 * range is pending here; D14 turns a blank numeric into `= 0`, which nobody
 * means, so a blank numeric is pending here too.
 */
export function isComplete(clause, entry) {
	if (!clause || !entry) return false;
	const operators = entry.operators || [];
	if (!clause.operator || !operators.includes(clause.operator)) return false;
	return isShapeComplete(clause);
}

/**
 * Completeness WITHOUT a catalog to check the field against — the value has the
 * shape its operator needs, and that is all we can know.
 *
 * This exists for exactly one situation: a shared link's clauses when the schema
 * fetch failed. Dropping them there would show an UNFILTERED list for a filtered
 * URL, which is the one failure a filtered list must never have; sending them
 * lets the server — the only authority anyway — either answer or reject with a
 * code we can show.
 */
export function isShapeComplete(clause) {
	if (!clause || !clause.operator) return false;
	const value = clause.value;
	switch (clause.operator) {
		case "is":
			return value === "set" || value === "not set";
		case "in":
		case "not in":
			return Array.isArray(value) && value.some((v) => !isBlank(v));
		case "Between":
			return (
				Array.isArray(value) &&
				value.length === 2 &&
				!isBlank(value[0]) &&
				!isBlank(value[1])
			);
		case "Timespan":
			return TIMESPANS.includes(String(value || "").trim());
		default:
			return !isBlank(value);
	}
}

/**
 * Complete clauses only — pending rows never reach the wire.
 *
 * `index` may be null, meaning "no catalog loaded": then completeness is the
 * shape check above rather than a field check, because the alternative is
 * silently sending nothing.
 */
export function toWire(clauses, index) {
	const out = [];
	for (const clause of clauses || []) {
		if (!index) {
			if (!isShapeComplete(clause)) continue;
		} else if (!isComplete(clause, entryFor(index, clause))) continue;
		out.push({
			doctype: clause.doctype,
			fieldname: clause.fieldname,
			operator: clause.operator,
			value: wireValue(clause),
		});
	}
	return out;
}

function wireValue(clause) {
	if (clause.operator === "in" || clause.operator === "not in") {
		return (clause.value || []).filter((v) => !isBlank(v)).map((v) => String(v));
	}
	if (clause.operator === "Between") {
		return [String(clause.value[0]), String(clause.value[1])];
	}
	return String(clause.value);
}

/**
 * The Filter chip's count: complete clauses, not rows on screen (plan §6.4).
 * `index` may be null on the same terms as :func:`toWire` — a count of 0 while
 * clauses are in force would be its own lie.
 */
export function activeCount(clauses, index) {
	let n = 0;
	for (const clause of clauses || []) {
		const ok = index ? isComplete(clause, entryFor(index, clause)) : isShapeComplete(clause);
		if (ok) n += 1;
	}
	return n;
}

// ── URL state (plan §8) ─────────────────────────────────────────────────────
/**
 * One versioned query param. The payload is the four-part clause shape of plan
 * §5 — [doctype, fieldname, operator, value] — which is both compact and the
 * same tuple the server contract is written in.
 *
 * `k` (the view key) is carried because Skills and Agents mount a list inside a
 * TABBED route: two tabs share one URL, and a clause list for the other tab's
 * doctype must be ignored rather than half-applied (judgment C08-7).
 */
export function serializeClauses(viewKey, clauses, index) {
	const wire = toWire(clauses, index);
	if (!wire.length) return "";
	return JSON.stringify({
		v: URL_CONTRACT_VERSION,
		k: viewKey || "",
		c: wire.map((c) => [c.doctype, c.fieldname, c.operator, c.value]),
	});
}

/** Normalize a URL value to its clause shape WITHOUT truncating (P1-02). */
function shapeValue(value) {
	if (Array.isArray(value)) return value.map((v) => String(v == null ? "" : v));
	if (value === null || value === undefined) return "";
	return String(value);
}

/**
 * Whether a URL value is over a §9 bound (P1-02). Three distinct cases — an
 * oversize scalar, an oversize member of a list, or an over-count list — all
 * mean the same thing to the reader: the clause was mangled in the URL and must
 * be REJECTED, never silently reshaped into a different valid query. The old
 * `boundValue` sliced arrays and strings and reported nothing, so "match this
 * exact value" quietly became "match this prefix".
 */
function exceedsBounds(value, limits) {
	if (Array.isArray(value)) {
		if (value.length > limits.max_in_values) return true;
		return value.some((v) => String(v == null ? "" : v).length > limits.max_value_chars);
	}
	if (value === null || value === undefined) return false;
	return String(value).length > limits.max_value_chars;
}

/** True when a payload is too long to put in a URL that will survive the trip. */
export function isUrlPayloadTooLarge(param) {
	return typeof param === "string" && param.length > MAX_URL_CHARS;
}

/**
 * The result of a payload we could not read AT ALL — truncated in a chat client,
 * over the length bound, hand-mangled, or written by a newer contract version.
 *
 * This is deliberately NOT `null`. `null` means "there is nothing here for us"
 * (no param, or a payload addressed to a sibling tab's list) and is correctly
 * silent; `unreadable` means "there WAS a filter set in this link and it is
 * gone", which the user has to be told, because the alternative is a list that
 * looks like an answer to a question it never asked.
 */
export function unreadableClauses() {
	return { unreadable: true, viewKey: "", clauses: [], skipped: 0, bounded: 0 };
}

/**
 * Parse and BOUND a `fv2=` payload (plan §8 step 2).
 *
 * Returns one of:
 *   null                                   nothing here for us (silent)
 *   {unreadable:true, clauses:[]}          there was a payload; say so
 *   {viewKey, clauses, skipped}            readable; `skipped` rows were junk
 */
export function parseClauseParam(raw, viewKey, schema) {
	if (!raw || typeof raw !== "string") return null;
	if (isUrlPayloadTooLarge(raw)) return unreadableClauses();
	let data;
	try {
		data = JSON.parse(raw);
	} catch (e) {
		return unreadableClauses();
	}
	if (!data || typeof data !== "object" || Array.isArray(data)) return unreadableClauses();
	// A payload written for a sibling tab's list is not ours to interpret — and
	// not ours to complain about either. Checked BEFORE the version, so a future
	// contract on another tab stays silent here.
	if (viewKey && data.k && data.k !== viewKey) return null;
	if (data.v !== URL_CONTRACT_VERSION) return unreadableClauses();
	if (!Array.isArray(data.c)) return unreadableClauses();
	const limits = limitsOf(schema);
	const clauses = [];
	let skipped = Math.max(0, data.c.length - limits.max_clauses);
	// Over-bound values are a DISTINCT outcome from a structurally broken row:
	// the clause was well-formed but too large to carry, and it is rejected, not
	// truncated (P1-02).
	let bounded = 0;
	// UX2: the (doctype, fieldname) of the rows we set aside, so the notice can
	// name the affected fields (resolved to labels via the catalog when it is
	// present). Only the identifiable rows land here — a row too broken to carry a
	// fieldname stays a bare count.
	const skippedFields = [];
	const boundedFields = [];
	for (const row of data.c.slice(0, limits.max_clauses)) {
		if (!Array.isArray(row) || row.length < 3) {
			skipped += 1;
			continue;
		}
		const [doctype, fieldname, operator, value] = row;
		if (
			typeof doctype !== "string" ||
			typeof fieldname !== "string" ||
			!doctype ||
			!fieldname
		) {
			skipped += 1;
			continue;
		}
		// The operator vocabulary is closed. An unknown one cannot be rendered
		// (no control, no label) and cannot be compiled, so keeping the row would
		// only produce a filter that silently never applies.
		if (typeof operator !== "string" || !OPERATORS.includes(operator)) {
			skipped += 1;
			skippedFields.push({ doctype, fieldname });
			continue;
		}
		const raw = value === undefined ? emptyValueFor(operator) : value;
		// Reject an oversize scalar, an oversize member, or an over-count list —
		// never silently reshape "match this exact value" into "match this prefix"
		// or drop the last member of an `in` (P1-02).
		if (exceedsBounds(raw, limits)) {
			bounded += 1;
			boundedFields.push({ doctype, fieldname });
			continue;
		}
		clauses.push(makeClause({ doctype, fieldname, operator, value: shapeValue(raw) }));
	}
	return {
		unreadable: false,
		viewKey: data.k || viewKey || "",
		clauses,
		skipped,
		bounded,
		skippedFields,
		boundedFields,
	};
}

/**
 * Plan §8 step 3/4: keep only what this caller may still filter on, and hand
 * back what was dropped so the page can SAY so. A field that vanished (renamed,
 * un-permitted, withheld by the endpoint) is never silently reinterpreted.
 */
export function reconcileClauses(clauses, index) {
	// No catalog ⇒ nothing to reconcile against. Dropping on a guess would be
	// the silent narrowing this function exists to prevent.
	if (!index) return { kept: [...(clauses || [])], dropped: [] };
	const kept = [];
	const dropped = [];
	for (const clause of clauses || []) {
		const entry = entryFor(index, clause);
		if (!entry || !(entry.operators || []).includes(clause.operator)) dropped.push(clause);
		else kept.push(clause);
	}
	return { kept, dropped };
}

/**
 * "Created On", "Created On and Status", "Created On, Status and 2 more" (UX2).
 * A friendly, bounded field-name list for a notice.
 */
export function nameList(labels) {
	const names = (labels || []).filter(Boolean);
	if (!names.length) return "";
	if (names.length === 1) return names[0];
	if (names.length === 2) return `${names[0]} and ${names[1]}`;
	return `${names.slice(0, 2).join(", ")} and ${names.length - 2} more`;
}

/**
 * Resolve field identities to their catalog LABELS via the index (UX2). Falls
 * back to nothing when a field is absent from the catalog — a raw fieldname is not
 * a label and naming it would be worse than a count. So the notices name a field
 * only when the schema can tell us its human label.
 */
export function labelsFor(items, index) {
	const out = [];
	for (const it of items || []) {
		const entry = index ? entryFor(index, it) : null;
		if (entry && entry.label) out.push(entry.label);
	}
	return out;
}

export function droppedNotice(dropped, index) {
	const n = (dropped || []).length;
	if (!n) return "";
	// A dropped clause is dropped BECAUSE its field is absent from the catalog, so
	// there is rarely a label to name — the count phrasing is the honest one.
	const names = nameList(labelsFor(dropped, index));
	const verb = n === 1 ? "is" : "are";
	const past = n === 1 ? "was" : "were";
	if (names) return `${names} ${verb} no longer available on this list and ${past} removed.`;
	return `${n} filter${
		n === 1 ? "" : "s"
	} from this link ${verb} no longer available on this list and ${past} removed.`;
}

/** A payload that arrived but could not be read at all. Never silent. */
export const UNREADABLE_NOTICE =
	"The filters in this link couldn't be read, so this list is unfiltered.";

/**
 * The view is rolled back to legacy transport (M1.4): a shared link's filters are
 * NOT applied and there is nothing to retry. Honest and distinct from the
 * transient "couldn't be loaded" copy.
 */
export const ROLLED_BACK_NOTICE =
	"Filters are turned off for this list, so this link's filters weren't applied.";

/** Rows inside a readable payload that were not clauses. `labels` names them (UX2). */
export function skippedNotice(n, labels) {
	if (!n) return "";
	const names = nameList(labels);
	const was = n === 1 ? "was" : "were";
	if (names) {
		const verb = countOf(labels) === 1 ? "wasn't" : "weren't";
		return `The ${names} filter${
			countOf(labels) === 1 ? "" : "s"
		} in this link ${verb} valid and ${was} ignored.`;
	}
	return `${n} filter${n === 1 ? "" : "s"} in this link ${was} not valid and ${was} ignored.`;
}

/**
 * Clauses in a readable payload whose value was over a size/count bound (P1-02).
 * Distinct from `skippedNotice`: those rows were malformed, these were well-formed
 * but too large to carry, and were rejected rather than truncated into a
 * different question. `labels` names the affected fields (UX2).
 */
export function boundedNotice(n, labels) {
	if (!n) return "";
	const names = nameList(labels);
	const was = n === 1 ? "was" : "were";
	if (names) {
		return `The ${names} filter${
			countOf(labels) === 1 ? "" : "s"
		} in this link ${was} too large to apply and ${was} left off.`;
	}
	return `${n} filter${
		n === 1 ? "" : "s"
	} in this link ${was} too large to apply and ${was} left off.`;
}

function countOf(labels) {
	return (labels || []).filter(Boolean).length;
}

// The filter set is real and applied, but too big to put in a URL. Two cases,
// because "your link is stale" and "there is no link" are different problems:
// one leaves a shareable address behind, the other never had one.
export const URL_TOO_LARGE_NOTICE =
	"This filter set is too large to share as a link. The address bar keeps the last shareable one.";
export const URL_TOO_LARGE_UNSHARED_NOTICE =
	"This filter set is too large to share as a link. It is applied, but not in the address bar.";

// ── server error codes → what the panel does about it ───────────────────────
export const ERR_UNKNOWN_VIEW = "list_filter_unknown_view";
export const ERR_VIEW_NOT_FILTERABLE = "list_filter_view_not_filterable";
//: The view IS migrated but an operator rolled it back via the runtime kill
//: switch. Distinct from ERR_VIEW_NOT_FILTERABLE ("never migrated") so the panel
//: can say "turned off" with NO retry — retrying cannot succeed until the flag is
//: flipped back — instead of the transient "couldn't be loaded / Try again" copy.
export const ERR_VIEW_ROLLED_BACK = "list_filter_view_rolled_back";
export const ERR_BAD_PAYLOAD = "list_filter_bad_payload";
export const ERR_UNKNOWN_FIELD = "list_filter_unknown_field";
export const ERR_INVALID_OPERATOR = "list_filter_invalid_operator";
export const ERR_INVALID_VALUE = "list_filter_invalid_value";
export const ERR_TOO_MANY_CLAUSES = "list_filter_too_many_clauses";
export const ERR_TOO_MANY_VALUES = "list_filter_too_many_values";
export const ERR_VALUE_TOO_LONG = "list_filter_value_too_long";
export const ERR_SCHEMA_UNAVAILABLE = "list_filter_schema_unavailable";
export const ERR_QUERY_TOO_EXPENSIVE = "list_filter_query_too_expensive";

/**
 * How the panel treats each code:
 *   "schema"    the field catalog itself is gone/stale → refresh it, offer retry
 *   "cap"       a limit was hit → say the limit, block adding more
 *   "row"       one clause is wrong → attribute it to that row
 *   "transient" our side, retryable → banner + retry, list untouched
 */
export const FILTER_ERROR_KIND = {
	[ERR_UNKNOWN_VIEW]: "schema",
	[ERR_VIEW_NOT_FILTERABLE]: "disabled",
	[ERR_VIEW_ROLLED_BACK]: "disabled",
	[ERR_UNKNOWN_FIELD]: "schema",
	[ERR_INVALID_OPERATOR]: "schema",
	[ERR_BAD_PAYLOAD]: "row",
	[ERR_INVALID_VALUE]: "row",
	[ERR_VALUE_TOO_LONG]: "row",
	[ERR_TOO_MANY_CLAUSES]: "cap",
	[ERR_TOO_MANY_VALUES]: "cap",
	[ERR_SCHEMA_UNAVAILABLE]: "transient",
	// Its own kind: the filter is VALID, it just costs too much to run. "Narrow
	// it" is the action, and retrying unchanged is futile — so this is neither a
	// row error (nothing is wrong with the clause) nor transient (waiting will
	// not help).
	[ERR_QUERY_TOO_EXPENSIVE]: "cost",
};

const FALLBACK_COPY = {
	[ERR_UNKNOWN_VIEW]: "Filters aren't available for this list.",
	[ERR_VIEW_NOT_FILTERABLE]: "Filters aren't available for this list yet.",
	[ERR_VIEW_ROLLED_BACK]: "Filters are turned off for this list.",
	[ERR_UNKNOWN_FIELD]: "A filter field is no longer available on this list.",
	[ERR_INVALID_OPERATOR]: "That condition isn't valid for the field it's on.",
	[ERR_BAD_PAYLOAD]: "Those filters couldn't be read.",
	[ERR_INVALID_VALUE]: "A filter value isn't valid.",
	[ERR_VALUE_TOO_LONG]: "A filter value is too long.",
	[ERR_TOO_MANY_CLAUSES]: "Too many filters at once.",
	[ERR_TOO_MANY_VALUES]: "Too many values in one condition.",
	[ERR_SCHEMA_UNAVAILABLE]: "Filters are unavailable for this list right now.",
	[ERR_QUERY_TOO_EXPENSIVE]:
		"That filter is too broad to run on this list. Narrow it and try again.",
};

function envelopeIn(candidate) {
	if (!candidate || typeof candidate !== "object") return null;
	const error = candidate.error;
	if (error && typeof error === "object" && typeof error.code === "string") return error;
	return null;
}

/**
 * Dig the stable code out of whatever the transport handed us.
 *
 * frappe-ui's `call` throws on the deliberate 4xx/5xx that
 * `filter_errors_to_envelope` sets, and it builds `e.messages` from
 * `_server_messages` CONCAT the response body's `message` key — so our
 * `{ok:false, error:{code,message}}` envelope arrives as an OBJECT inside that
 * array (its JSON.parse attempt fails and leaves it untouched). We also accept
 * the envelope arriving as the resolved value or as a JSON string, because that
 * is one Frappe response-shape change away and reading a coded rejection as an
 * empty result set is the one failure mode a filtered list must never have.
 */
export function filterErrorInfo(e) {
	if (!e) return null;
	const candidates = [e, ...(Array.isArray(e.messages) ? e.messages : [])];
	let error = null;
	for (const candidate of candidates) {
		if (!candidate) continue;
		let value = candidate;
		if (typeof value === "string") {
			if (value.indexOf("list_filter_") === -1) continue;
			try {
				value = JSON.parse(value);
			} catch (err) {
				continue;
			}
		}
		error = envelopeIn(value);
		if (error) break;
	}
	if (!error || !FILTER_ERROR_KIND[error.code]) return null;
	const message =
		(typeof error.message === "string" && error.message.trim()) ||
		FALLBACK_COPY[error.code] ||
		"That filter was rejected.";
	return { code: error.code, kind: FILTER_ERROR_KIND[error.code], message };
}

/**
 * Best-effort attribution of a row-level rejection to the row that caused it.
 * The server never sends a clause index (its messages are user-facing copy that
 * names the FIELD LABEL: "Created On needs a start and an end."), so we match on
 * the label. No match ⇒ the error stays at panel level; we never blame a row we
 * are not sure about.
 */
export function attributeError(error, clauses, index) {
	if (!error || error.kind !== "row") return null;
	const message = String(error.message || "");
	let match = null;
	for (const clause of clauses || []) {
		const entry = entryFor(index, clause);
		const label = entry && entry.label;
		if (!label || !mentionsLabel(message, label)) continue;
		if (match) return null; // ambiguous (same field twice) — don't guess
		match = clause.id;
	}
	return match;
}

const LABEL_CACHE = new Map();

/**
 * Whether a message names this field, on WORD boundaries.
 *
 * A substring test blames the wrong row for any short label: a field labelled
 * "e" matches "Qty must be a number." and the error lands on a row that has
 * nothing to do with it. Labels are user data (they carry translations,
 * brackets, punctuation), so the pattern is escaped, and `\b` is not enough
 * because a label may start or end with a non-word character — hence the
 * explicit non-word-or-edge guards.
 */
function mentionsLabel(message, label) {
	let re = LABEL_CACHE.get(label);
	if (!re) {
		const escaped = String(label).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
		re = new RegExp(`(^|\\W)${escaped}(\\W|$)`);
		LABEL_CACHE.set(label, re);
	}
	return re.test(message);
}
