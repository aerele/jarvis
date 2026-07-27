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
export const ASK_FIELD_TYPES = ["date", "datetime", "link", "text"];

/**
 * Parse the first ```jarvis-ask block out of a message.
 * @param {string} content raw assistant message text
 * @returns {{questions: Array<{q: string, type: string, options: string[], doctype: string}>}|null}
 */
export function parseAsk(content) {
	const mt = String(content || "").match(ASK_RE);
	if (!mt) return null;
	try {
		const a = JSON.parse(mt[1].trim());
		const raw = Array.isArray(a) ? a : a && a.questions;
		if (!Array.isArray(raw)) return null;
		const questions = raw
			.slice(0, 6)
			.map((q) => {
				let type = q.type === "boolean" ? "yesno" : q.type;
				if (!["single", "multi", "yesno", ...ASK_FIELD_TYPES].includes(type))
					type = "single";
				return {
					q: String(q.q || q.question || "").trim(),
					type,
					// yesno may carry exactly 2 custom labels (e.g. ["Approve","Reject"]).
					options: Array.isArray(q.options) ? q.options.map(String).slice(0, 8) : [],
					doctype: type === "link" ? String(q.doctype || q.link || "").trim() : "",
				};
			})
			.filter((q) => {
				if (!q.q) return false;
				if (q.type === "yesno" || ASK_FIELD_TYPES.includes(q.type)) return true;
				return q.options.length > 0;
			});
		return questions.length ? { questions } : null;
	} catch (e) {
		return null;
	}
}

/** True once every question carries an answer (a pick, a typed value, or "Other"). */
export function isAskReady(spec, sel, other) {
	if (!spec) return false;
	return spec.questions.every((q, i) => {
		const v = sel[i];
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
		return `${i + 1}. ${q.q} → ${ans.join(", ") || "(no answer)"}`;
	});
	return "Here are my answers:\n" + lines.join("\n");
}
