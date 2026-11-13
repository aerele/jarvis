// The ```jarvis-ask contract, as plain functions: parse a fenced block into a
// normalised question list, decide when the draft is complete, and render the
// picks as the message that goes back to the agent.
//
// Extracted from ChatView so the SAME rules run on every surface that shows the
// cards (main chat, the Dashboards builder pane). The block spec lives in
// jarvis-persona skills/jarvis-chat-blocks/SKILL.md; the normalisation here is
// the defensive half of it (the model emits JSON, not a validated payload).
// <AskCard> is the one renderer; these functions are what it renders from.

export const ASK_RE = /```jarvis-ask[ \t]*\n([\s\S]*?)```/;

// Types that take a typed/picked VALUE rather than option buttons. An ask made
// only of these renders as a compact mini-form (no numbered badges).
export const ASK_FIELD_TYPES = ["date", "datetime", "field", "link", "select", "text"];

// A field question names a DocType and a field; both are interpolated into an
// API call, so only plain identifiers get through (DocType names may hold
// spaces, fieldnames may not).
const DOCTYPE_RE = /^[A-Za-z0-9][A-Za-z0-9 _-]*$/;
const FIELDNAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

// Types a model may put in `kind` instead of `type`.
const ASK_KINDS = ["single", "multi", "yesno", "boolean", ...ASK_FIELD_TYPES];

// A question the user may leave blank: `optional: true` / `required: false`, or
// a label that says so the way models write it ("Last name (optional)"). The
// label is kept as written: the board and the answer text quote it verbatim.
const OPTIONAL_LABEL_RE = /\(\s*optional\s*\)\s*$/i;
function askOptional(q, text) {
	return q.optional === true || q.required === false || OPTIONAL_LABEL_RE.test(text);
}

// The question text, under whichever key the model used.
function askText(q) {
	for (const key of ["q", "question", "label", "title", "prompt"]) {
		const v = q[key] == null ? "" : String(q[key]).trim();
		if (v) return v;
	}
	return "";
}

// A nested `field: {doctype, fieldname}` object, or null. A list, an empty object or one
// naming neither key is not a field reference (chat_asks._nested_field agrees).
function nestedField(f) {
	if (!f || typeof f !== "object" || Array.isArray(f)) return null;
	return f.doctype || f.fieldname ? f : null;
}

// An option's display text. The spec asks for plain strings, but a model also
// writes `{label, value}` objects; String() of one draws "[object Object]" (#837).
// Its readable text comes first, then its value; an option with neither is dropped.
// chat_asks._option_text agrees.
const OPTION_KEYS = ["label", "title", "text", "name", "value"];
export function optionText(o) {
	if (o == null) return "";
	if (typeof o !== "object") return String(o).trim();
	if (Array.isArray(o)) return "";
	for (const key of OPTION_KEYS) {
		const v = o[key];
		if (v != null && typeof v !== "object" && String(v).trim()) return String(v).trim();
	}
	return "";
}

// A bare array, `{questions: [...]}`, or one question object on its own.
function askItems(a) {
	if (Array.isArray(a)) return a;
	if (!a || typeof a !== "object") return null;
	if ("questions" in a) return a.questions;
	return askText(a) || a.type || a.kind ? [a] : null;
}

/**
 * A readable label for a field whose question carries none: strip a leading
 * `custom_`, underscores to spaces, capitalise the first letter
 * (`custom_gstin_2` -> `Gstin 2`).
 */
export function humanizeFieldname(fieldname) {
	const s = String(fieldname || "")
		.replace(/^custom_/, "")
		.replace(/_+/g, " ")
		.trim();
	return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Parse the first ```jarvis-ask block out of a message.
 * @param {string} content raw assistant message text
 * @returns {{questions: Array<{q: string, type: string, options: string[], doctype: string, fieldname: string}>}|null}
 */
export function parseAsk(content) {
	const mt = String(content || "").match(ASK_RE);
	if (!mt) return null;
	try {
		const a = JSON.parse(mt[1].trim());
		const raw = askItems(a);
		if (!Array.isArray(raw)) return null;
		const questions = raw
			.slice(0, 6)
			.map((q) => {
				// A nested `field: {doctype, fieldname}` makes it a field question.
				const nested = nestedField(q.field);
				let type = q.type == null && ASK_KINDS.includes(q.kind) ? q.kind : q.type;
				if (nested) type = "field";
				if (type === "boolean") type = "yesno";
				if (!["single", "multi", "yesno", ...ASK_FIELD_TYPES].includes(type))
					type = "single";
				// A Select field's choice list can be long; only the button types stay at 8.
				const cap = type === "select" ? 200 : 8;
				const options = Array.isArray(q.options)
					? q.options.map(optionText).filter(Boolean).slice(0, cap)
					: [];
				// A dropdown with nothing to pick from is just a typed answer.
				if (type === "select" && !options.length) type = "text";
				const text = askText(q);
				const optional = askOptional(q, text) ? { optional: true } : {};
				if (type === "field") {
					const doctype = String((nested || q).doctype || "").trim();
					const fieldname = String((nested || q).fieldname || "").trim();
					const parent = String((nested || q).parent || "").trim();
					const parentfield = String((nested || q).parentfield || "").trim();
					// A bad identifier must not reach the API call; the question
					// degrades to a typed answer instead of vanishing.
					if (
						!DOCTYPE_RE.test(doctype) ||
						!FIELDNAME_RE.test(fieldname) ||
						(parent && !DOCTYPE_RE.test(parent)) ||
						(parentfield && (!parent || !FIELDNAME_RE.test(parentfield)))
					) {
						return {
							q: text || humanizeFieldname(fieldname),
							type: "text",
							options: [],
							doctype: "",
							...optional,
						};
					}
					return {
						q: text,
						type,
						options: [],
						doctype,
						fieldname,
						...(parent ? { parent } : {}),
						...(parentfield ? { parentfield } : {}),
						...optional,
					};
				}
				return {
					q: text,
					type,
					// yesno may carry exactly 2 custom labels (e.g. ["Approve","Reject"]).
					options,
					doctype: type === "link" ? String(q.doctype || q.link || "").trim() : "",
					...optional,
				};
			})
			.filter((q) => {
				if (q.type === "field") return true;
				if (!q.q) return false;
				if (q.type === "yesno" || ASK_FIELD_TYPES.includes(q.type)) return true;
				return q.options.length > 0;
			});
		return questions.length ? { questions } : null;
	} catch (e) {
		return null;
	}
}

// Nothing picked or typed for a question.
function askBlank(v, free) {
	if (free) return false;
	if (Array.isArray(v)) return v.length === 0;
	return v == null || String(v).trim() === "";
}

/**
 * True once every question carries an answer (a pick, a typed value, or "Other").
 * An optional question may stay blank; once something is typed into it, it must
 * be a valid answer like any other (a bad number still blocks).
 */
export function isAskReady(spec, sel, other) {
	if (!spec) return false;
	return spec.questions.every((q, i) => {
		const v = sel[i];
		if (q.optional && askBlank(v, (other[i] || "").trim())) return true;
		// Drawn-only types (a `field` question resolved to a control).
		if (q.type === "loading") return false;
		if (q.type === "number") {
			const n = String(v == null ? "" : v)
				.replace(/,/g, "")
				.trim();
			return n !== "" && Number.isFinite(Number(n));
		}
		if (ASK_FIELD_TYPES.includes(q.type)) return v != null && String(v).trim() !== "";
		const free = (other[i] || "").trim();
		if (q.type === "multi") return (Array.isArray(v) && v.length > 0) || !!free;
		return (v != null && v !== "") || !!free;
	});
}

/** Render the answered ask as the user message that resumes the turn. */
export function askAnswerText(spec, sel, other) {
	const lines = spec.questions.map((q, i) => {
		const ans = [];
		const v = sel[i];
		const free = (other[i] || "").trim();
		if (Array.isArray(v)) ans.push(...v);
		// Single-answer questions: a typed "Other…" IS the answer — never send
		// both it and a leftover pick (the UI keeps them exclusive; this is the
		// belt-and-braces for stale state).
		else if (v != null && v !== "" && !free) ans.push(v);
		if (free) ans.push(free);
		// A blank optional question was skipped on purpose; the agent must not re-ask it.
		const blank = q.optional ? "(skipped)" : "(no answer)";
		return `${i + 1}. ${q.q} → ${ans.join(", ") || blank}`;
	});
	return "Here are my answers:\n" + lines.join("\n");
}
