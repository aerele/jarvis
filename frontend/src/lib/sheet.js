// A File Box approval sheet on the board (kind file_box_sheet): the sections, each
// record's decision, the client-side cascade and apply gating (both mirror
// pending_actions/_sheet.py), the apply payload and the copy. Pure; every string
// returned is plain text (callers escape it for an HTML sink).
import { patchOf, stillMissing } from "@/lib/heldEdit";
import { __ } from "@/lib/i18n";
import { REJECT_RE, isApproveRejectPair } from "@/lib/approvalVerbs";

export const PREVIEW = 20; // records a section shows before "Show all"
export const ACTION_LABEL = {
	create: __("Create"),
	apply: __("Apply"),
	edit: __("Edit"),
	use_existing: __("Use existing"),
	skip: __("Skip"),
};
const TERMINAL = ["Executed", "Discarded", "Failed", "Cancelled", "Superseded"];
const SKIP_FILE = "Skip this file"; // a routing answer that skips the whole file: matches
// the server's option text, never translated
const ORDER = ["party", "address", "item", "other"];
const FIXED_LABEL = { item: __("Items"), other: __("Other masters") };

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const isEmpty = (v) => v == null || String(v).trim() === "";

export function sheetTitle(r) {
	return (r && (r.file_name || r.conversation_title)) || __("Approval sheet");
}

// {field, label, message} | null: a legacy fix is its text alone.
export function fixOf(record) {
	const f = record && record.needs_fix;
	if (f && typeof f === "object")
		return {
			field: f.field || null,
			label: f.label || null,
			message: String(f.message || ""),
		};
	return isEmpty(f) ? null : { field: null, label: null, message: String(f) };
}

export function actionsFor(record) {
	return record.op === "update"
		? ["apply", "edit", "skip"]
		: ["create", "edit", "use_existing", "skip"];
}
const defaultAction = (record) => actionsFor(record)[0];

export function sections(records) {
	const out = [];
	for (const key of ORDER) {
		const rows = (records || []).filter(
			(r) => (ORDER.includes(r.category) ? r.category : "other") === key
		);
		if (!rows.length) continue;
		out.push({
			key,
			label: FIXED_LABEL[key] || [...new Set(rows.map((r) => r.doctype))].join(" / "),
			records: rows,
			updatesOnly: rows.every((r) => r.op === "update"),
		});
	}
	return out;
}

const humanize = (f) => {
	const s = String(f).replace(/_/g, " ").trim();
	return s.charAt(0).toUpperCase() + s.slice(1);
};

// The scalar values that tell records apart (secrets and the title left out).
export function keyFields(record, max = 4) {
	const secret = new Set(record.secret || []);
	const out = [];
	for (const [key, value] of Object.entries(record.values || {})) {
		if (secret.has(key) || value == null || typeof value === "object" || isEmpty(value))
			continue;
		if (String(value) === record.title) continue;
		out.push({ key, label: humanize(key), value: String(value) });
		if (out.length === max) break;
	}
	return out;
}

// A patch over values; a table patch edits its rows by position.
export function applyPatch(values, patch) {
	const out = { ...(values || {}) };
	for (const [key, value] of Object.entries(patch || {})) {
		if (Array.isArray(value) && Array.isArray(out[key])) {
			out[key] = out[key].map((row, i) => ({ ...row, ...(value[i] || {}) }));
		} else out[key] = value;
	}
	return out;
}

const clone = (v) => JSON.parse(JSON.stringify(v || {}));

// {index: {action, values, existing}}: a bounced apply's decisions, else what was
// on screen before, else the default. `values` stays null until Edit is used. A
// bounced skip under another skip (older applies sent the cascade as skips) is
// left to the cascade.
export function initialChoices(records, decisions, previous) {
	const given = (decisions && decisions.records) || {};
	const below = underSkips(records, (i) => given[i] || given[String(i)]);
	const out = {};
	for (const r of records || []) {
		let d = given[r.index] || given[String(r.index)];
		if (d && d.action === "skip" && below.has(r.index)) d = null;
		const allowed = actionsFor(r);
		if (d && allowed.includes(d.action)) {
			const existing = d.existing ? String(d.existing) : "";
			// the server echoes the id only; carry the label forward if it's for
			// the same id (a bounced reload else shows the raw id, not the title)
			const prev = previous && previous[r.index];
			const existingLabel =
				d.action === "use_existing" && prev && prev.existing === existing
					? prev.existingLabel || ""
					: "";
			out[r.index] = {
				action: d.action,
				values: d.action === "edit" ? applyPatch(clone(r.values), d.values) : null,
				existing,
				...(existingLabel && { existingLabel }),
			};
		} else if (previous && previous[r.index] && allowed.includes(previous[r.index].action)) {
			out[r.index] = { ...previous[r.index] };
		} else out[r.index] = { action: defaultAction(r), values: null, existing: "" };
	}
	return out;
}

export const isOpenQuestion = (q) => !q.status || q.status === "Pending";
const optionsOf = (q) => (Array.isArray(q.options) ? q.options.map(String) : []);

export function initialAnswers(questions, decisions, previous) {
	const given = (decisions && decisions.answers) || {};
	const out = {};
	for (const q of (questions || []).filter(isOpenQuestion)) {
		const text = given[q.name] && String(given[q.name].text || "");
		if (!text) {
			out[q.name] = (previous && previous[q.name]) || { option: "", note: "" };
			continue;
		}
		const opts = optionsOf(q);
		const option = opts.includes(text) ? text : opts.find((o) => text.startsWith(o + ": "));
		out[q.name] = option
			? { option, note: text.slice(option.length).replace(/^:\s*/, "") }
			: { option: "", note: text };
	}
	return out;
}

// A routing question takes one of its options, never free text.
export function answerText(q, a) {
	const option = (a && a.option) || "";
	if (q.routing) return optionsOf(q).includes(option) ? option : "";
	const note = String((a && a.note) || "").trim();
	return option && note ? `${option}: ${note}` : option || note;
}

const kindOf = (choice) =>
	choice && (choice.action === "skip" || choice.action === "use_existing")
		? choice.action
		: "write";

function dependents(records) {
	const out = new Map();
	for (const r of records || [])
		for (const d of r.depends_on || []) {
			if (!out.has(d)) out.set(d, []);
			out.get(d).push(r.index);
		}
	return out;
}

// Records below a skip, through writes and skips (a used existing record stops it).
function underSkips(records, choiceOf) {
	const deps = dependents(records);
	const out = new Set();
	const todo = (records || [])
		.filter((r) => kindOf(choiceOf(r.index)) === "skip")
		.map((r) => r.index);
	while (todo.length)
		for (const j of deps.get(todo.pop()) || [])
			if (!out.has(j) && kindOf(choiceOf(j)) !== "use_existing") {
				out.add(j);
				todo.push(j);
			}
	return out;
}

// Records to write that need `from`, transitively (a used existing record needs nothing).
export function reachFrom(records, choices, from) {
	const deps = dependents(records);
	const seen = new Set();
	const stack = [from];
	while (stack.length) {
		for (const j of deps.get(stack.pop()) || []) {
			if (!seen.has(j) && kindOf(choices[j]) === "write") {
				seen.add(j);
				stack.push(j);
			}
		}
	}
	return seen;
}

// {index: the skipped record it needs}: what the server skips with them.
export function cascade(records, choices) {
	const out = new Map();
	for (const r of records || []) {
		if (kindOf(choices[r.index]) !== "skip") continue;
		for (const j of reachFrom(records, choices, r.index)) if (!out.has(j)) out.set(j, r.index);
	}
	return new Map([...out].sort((a, b) => a[0] - b[0]));
}

const editValues = (record, choice) => (choice && choice.values) || record.values || {};

// What the record still needs before an apply: "" | fix | fix_unchanged | missing | existing.
export function recordIssue(record, choice) {
	const action = (choice && choice.action) || defaultAction(record);
	if (action === "skip") return "";
	if (action === "use_existing") return choice.existing ? "" : "existing";
	const editing = action === "edit";
	if (fixOf(record)) {
		if (!editing) return "fix";
		if (!Object.keys(patchOf(record.values, editValues(record, choice))).length)
			return "fix_unchanged";
	}
	const values = editing ? editValues(record, choice) : record.values;
	return stillMissing(record.needs_input, record.index, values).length ? "missing" : "";
}

const GATE_PARTS = [
	[["fix", "fix_unchanged"], (n) => __("fix or skip {0}", [plural(n, "record")])],
	[["missing"], (n) => __("fill the missing values on {0}", [plural(n, "record")])],
	[["existing"], (n) => __("pick the existing record for {0}", [plural(n, "record")])],
];

// A routing question answered "Skip this file": no record is written.
export function skipsFile(questions, answers) {
	return (questions || []).some(
		(q) =>
			q.routing && isOpenQuestion(q) && answerText(q, (answers || {})[q.name]) === SKIP_FILE
	);
}

// What keeps Apply disabled, in the sheet's order: {records: {index: code},
// questions: [name], summary, first: {type, index|name} | null}.
export function blockers({ records, choices, questions, answers }) {
	const skipped = cascade(records, choices);
	const out = { records: {}, questions: [], summary: "", first: null };
	const shown = skipsFile(questions, answers) ? [] : sections(records);
	for (const s of shown) {
		for (const r of s.records) {
			if (skipped.has(r.index)) continue;
			const code = recordIssue(r, choices[r.index]);
			if (!code) continue;
			out.records[r.index] = code;
			if (!out.first) out.first = { type: "record", index: r.index };
		}
	}
	for (const q of (questions || []).filter(isOpenQuestion)) {
		if (answerText(q, (answers || {})[q.name])) continue;
		out.questions.push(q.name);
		if (!out.first) out.first = { type: "question", name: q.name };
	}
	const codes = Object.values(out.records);
	const parts = out.questions.length
		? [__("answer {0}", [plural(out.questions.length, "question")])]
		: [];
	for (const [wanted, word] of GATE_PARTS) {
		const n = codes.filter((c) => wanted.includes(c)).length;
		if (n) parts.push(word(n));
	}
	if (parts.length) out.summary = __("Before you apply: {0}", [parts.join(" · ")]);
	return out;
}

// apply_sheet's `decisions`: every record's choice (the server skips the cascade;
// all of them when the file is skipped), only the edited values, the open
// questions' answers, and the sheet it was read from.
export function buildDecisions(rec, choices, answers) {
	const all = skipsFile(rec.questions, answers);
	const records = {};
	for (const r of rec.records || []) {
		const choice = choices[r.index] || {};
		const action = all ? "skip" : choice.action || defaultAction(r);
		const entry = { action };
		if (action === "edit") entry.values = patchOf(r.values, editValues(r, choice));
		if (action === "use_existing") entry.existing = choice.existing || "";
		records[r.index] = entry;
	}
	const out = {};
	for (const q of (rec.questions || []).filter(isOpenQuestion)) {
		const a = (answers || {})[q.name] || {};
		// A non-routing question that's really an Approve/Reject pair: a chosen
		// reject-verb option sends approve: 0 (routing answers are always 1).
		const reject =
			!q.routing &&
			isApproveRejectPair(optionsOf(q)) &&
			REJECT_RE.test(String(a.option || "").trim());
		out[q.name] = { text: answerText(q, a), approve: reject ? 0 : 1 };
	}
	return {
		records,
		answers: out,
		expected_card_sha256: rec.card_sha256,
		record_count: rec.record_count,
	};
}

// The longest field label `text` names on word edges ("HSN/SAC Code is required").
export function fieldNamedIn(text, fields) {
	const hay = String(text || "").toLowerCase();
	let best = null;
	for (const f of fields || []) {
		const label = String(f.label || "").toLowerCase();
		if (label.length < 3 || f.fieldtype === "Table" || f.fieldtype === "Table MultiSelect")
			continue;
		const at = hay.indexOf(label);
		if (at === -1) continue;
		const before = hay.charAt(at - 1);
		const after = hay.charAt(at + label.length);
		if (/[a-z0-9]/.test(before) || /[a-z0-9]/.test(after)) continue;
		if (!best || label.length > best.label.length) best = f;
	}
	return best;
}

// The main field a fix is about: its `field`, else the label its message names.
function fixField(fix, meta) {
	const fields = (meta && meta.fields) || [];
	if (!fix || !fields.length) return null;
	if (!fix.field) return fieldNamedIn(fix.message, fields);
	const f = fields.find((x) => x.fieldname === fix.field);
	return f && f.fieldtype !== "Table" && f.fieldtype !== "Table MultiSelect" ? f : null;
}

// SP-D1: a main field two or more of `records` still need (missing, or named by
// their fix), set once for all of them. `metas`: {doctype: form meta}.
export function setForAllGroups(records, choices, skipped, metas) {
	const groups = new Map();
	const add = (field, label, index) => {
		if (!groups.has(field.fieldname))
			groups.set(field.fieldname, { fieldname: field.fieldname, label, field, indexes: [] });
		const g = groups.get(field.fieldname);
		if (!g.indexes.includes(index)) g.indexes.push(index);
	};
	for (const r of records || []) {
		const choice = choices[r.index];
		if (kindOf(choice) !== "write" || (skipped && skipped.has(r.index))) continue;
		const values = choice && choice.action === "edit" ? editValues(r, choice) : r.values;
		for (const e of stillMissing(r.needs_input, r.index, values)) {
			if (!e.parentfield && e.field) add(e.field, e.label || e.field.label, r.index);
		}
		const named = fixField(fixOf(r), metas && metas[r.doctype]);
		if (!named) continue;
		const now = values[named.fieldname];
		const fixed =
			choice &&
			choice.action === "edit" &&
			!isEmpty(now) &&
			now !== r.values[named.fieldname];
		if (!fixed) add(named, named.label, r.index);
	}
	return [...groups.values()].filter((g) => g.indexes.length >= 2);
}

// Why a cascaded record is skipped, by the record it needs directly.
export function cascadeReason(record, choices, skipped) {
	const deps = record.depends_on || [];
	const own = deps.find((d) => kindOf(choices[d]) === "skip");
	if (own !== undefined)
		return __("Also skipped: it needs #{0}, which you're skipping.", [own + 1]);
	const via = deps.find((d) => skipped.has(d));
	return via === undefined
		? ""
		: __("Also skipped: it needs #{0}, which is skipped with #{1}.", [
				via + 1,
				skipped.get(via) + 1,
		  ]);
}

export const cascadeNote = (n) =>
	__("Applying also skips {0} that need{1} a skipped one.", [
		plural(n, "record"),
		n === 1 ? "s" : "",
	]);

// {records: {index: message}, questions: {name: message}, sheet: [message]}
export function splitErrors(errors, questions) {
	const names = new Set((questions || []).map((q) => q.name));
	const out = { records: {}, questions: {}, sheet: [] };
	for (const [key, message] of Object.entries(errors || {})) {
		if (/^\d+$/.test(key)) out.records[key] = String(message);
		else if (names.has(key)) out.questions[key] = String(message);
		else out.sheet.push(String(message));
	}
	return out;
}

const REFUSALS = {
	changed: __("This sheet changed since you opened it. Reloading it…"),
	busy: __("Someone is acting on this sheet right now. Try again in a moment."),
	executing: __("This sheet is being applied right now."),
	already_handled: __("This sheet was already handled."),
	not_found: __("This sheet is no longer available."),
	unavailable: __("The apply couldn't start. Nothing was created. Try again."),
	identity_refused: __(
		"This can no longer run as the person who dropped the file. Skip this file, or ask them to drop it again."
	),
	armed_run: __("This run is stopping, so nothing was applied."),
	collecting: __("Jarvis is still listing this sheet. Apply it once the run pauses."),
	invalid: __("Some records or answers need attention. Nothing was applied."),
	tampered: __("This sheet failed an integrity check. Nothing was created."),
	unverifiable: __("This sheet can no longer be verified on this site. Nothing was created."),
};

export function sheetRefusal(res) {
	const code = (res && res.reason_code) || "";
	const server = res && res.error && res.error.message;
	return REFUSALS[code] || (server ? String(server) : __("This sheet could not be applied."));
}

// The sheet left Pending for good (or is gone): the lane drops it.
export function isSheetSettled(res) {
	if (!res) return false;
	return (
		TERMINAL.includes(res.pa_status) ||
		["not_found", "already_handled"].includes(res.reason_code)
	);
}

export const isTerminal = (status) => TERMINAL.includes(status);

const OUTCOME = {
	created: (e) => __("Created {0} {1} as {2}", [e.doctype, e.title, e.name]),
	existing: (e) =>
		e.auto
			? __("Used the existing {0} {1} for {2} (it already existed)", [
					e.doctype,
					e.name,
					e.title,
			  ])
			: __("Used the existing {0} {1} for {2}", [e.doctype, e.name, e.title]),
	updated: (e) => __("Updated {0} {1}", [e.doctype, e.name || e.title]),
	skipped: (e) =>
		e.cascade
			? __("Skipped {0} {1} (it needed a skipped record)", [e.doctype, e.title])
			: __("Skipped {0} {1}", [e.doctype, e.title]),
};
// literal call sites, so frappe's JS extractor can harvest these strings
const OUTCOME_WORD = {
	created: (n) => __("created {0}", [n]),
	existing: (n) => __("used {0} existing", [n]),
	updated: (n) => __("updated {0}", [n]),
	skipped: (n) => __("skipped {0}", [n]),
};

export function outcomeSummary(outcome) {
	const records = (outcome && outcome.records) || [];
	const parts = [];
	for (const action of Object.keys(OUTCOME_WORD)) {
		const n = records.filter((e) => e.action === action).length;
		if (n) parts.push(OUTCOME_WORD[action](n));
	}
	const line = parts.join(" · ");
	return {
		line: line.charAt(0).toUpperCase() + line.slice(1),
		rows: records
			.filter((e) => OUTCOME[e.action])
			.map((e) => ({ key: e.index, action: e.action, text: OUTCOME[e.action](e) })),
		answers: ((outcome && outcome.answers) || []).map((a) => ({
			key: a.name,
			question: a.title || a.name,
			answer: a.answer || "",
		})),
	};
}

export function progressText(p) {
	return p && p.total
		? __("Applying the sheet… {0} of {1}", [p.done || 0, p.total])
		: __("Applying the sheet…");
}

// Read-only line for a sheet that can't be acted on (listing, applying, ended).
export function sheetStatusLine(rec) {
	if (!rec) return "";
	if (rec.status === "Executed") return __("Applied.");
	if (rec.status === "Discarded")
		return rec.reason_code === "discarded" || !rec.reason
			? __("Skipped. Nothing from this sheet was created.")
			: rec.reason;
	if (rec.status === "Failed") return rec.reason || __("This sheet could not be applied.");
	if (rec.status === "Pending")
		return __(
			"This sheet can't be verified right now, so it can't be applied. Try again, or skip this file."
		);
	return rec.reason || __("This sheet was already handled.");
}
