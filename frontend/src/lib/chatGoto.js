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

// ── the "fired" stamp (jarvis#912) ───────────────────────────────────────────
//
// One ```jarvis-goto message gets one durable localStorage stamp, keyed by
// message id. Before #912 the stamp was a bare marker (a fired timestamp, so
// the run:end handler never redirects twice for the same message). #912 adds
// a second job: remembering WHICH builder conversation that hand-off created,
// so a later trigger for the SAME message (the "Continue in Dashboards" card
// button has no per-click guard, unlike the auto-redirect) can navigate
// there instead of building a duplicate.
//
// The value stays a bare stringified timestamp ("<epochMs>") for as long as
// no conversation is known yet - byte-identical to the pre-#912 shape, and
// what an old stamp already on disk looks like. Once the conversation is
// known it becomes JSON ({t, conv}). Both shapes parse.

export function gotoFiredKey(messageId) {
	return "jarvis:goto-fired:" + messageId;
}

/**
 * @param {string|null} raw the localStorage value at gotoFiredKey(messageId)
 * @returns {{t: number, conv: string}|null} null when unfired / unreadable
 */
export function parseFiredStamp(raw) {
	if (!raw) return null;
	try {
		const v = JSON.parse(raw);
		if (v && typeof v === "object" && typeof v.t === "number") {
			return { t: v.t, conv: String(v.conv || "") };
		}
	} catch (e) {
		// not JSON - the bare pre-#912 shape, handled below
	}
	const t = Number(raw);
	return Number.isFinite(t) ? { t, conv: "" } : null;
}

/**
 * @param {number} t fired-at timestamp (ms)
 * @param {string} [conv] the builder conversation the hand-off landed on
 */
export function encodeFiredStamp(t, conv) {
	return conv ? JSON.stringify({ t, conv }) : String(t);
}
