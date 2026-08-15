// The ```jarvis-goto contract, as a plain function: parse a fenced block into
// a normalised {page, prompt} pair, or null.
//
// Extracted the same way chatAsk.js is, so ChatView.vue's gotoOf(m) stays a
// thin cached wrapper and the parsing itself is unit-testable without
// mounting the view (jarvis#884: dashboard builds move off the agent canvas,
// and the agent redirects here instead of building inline).
//
// This is deliberately an ALLOWLIST, not generic navigation: the agent picks
// a request restatement, not an arbitrary route. Today the only destination
// is "dashboards"; a block naming anything else is not a valid goto and is
// left for the raw markdown fallback to show.

export const GOTO_RE = /```jarvis-goto[ \t]*\n([\s\S]*?)```/;

const ALLOWED_PAGES = new Set(["dashboards"]);

/**
 * Parse the first ```jarvis-goto block out of a message.
 * @param {string} content raw assistant message text
 * @returns {{page: string, prompt: string}|null}
 */
export function parseGoto(content) {
	const mt = String(content || "").match(GOTO_RE);
	if (!mt) return null;
	try {
		const a = JSON.parse(mt[1].trim());
		if (!a || typeof a !== "object") return null;
		const page = String(a.page || "").trim();
		const prompt = String(a.prompt || "").trim();
		if (!ALLOWED_PAGES.has(page) || !prompt) return null;
		return { page, prompt };
	} catch (e) {
		return null;
	}
}
