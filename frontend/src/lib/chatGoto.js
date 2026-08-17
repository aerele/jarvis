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

// ── closing the double-click race (jarvis#912 round 2) ──────────────────────
//
// The "Continue in Dashboards" card has no per-click guard (it stays
// clickable on every later visit to the transcript), so two triggers for the
// same message in the window before send() answers with a conversation used
// to both read "no stamp yet" and each start their own build. gotoDashboards
// (ChatView.vue) closes that by claiming the stamp synchronously through this
// function before deciding anything else - the live run:end auto-redirect
// claims through the same call, so there is exactly one place a fresh build
// ever gets stamped.
//
// A bare stamp (fired, no conversation recorded yet) counts as an existing
// claim only inside this window - comfortably above one send() round trip.
// Two things depend on it expiring: a claim whose send() never reaches the
// server because the tab died first (DashboardChatPane's forgetGotoClaim
// undoes it on every ordinary failure, but not that one), and every
// pre-#912 stamp already on disk, which is bare by construction and would
// otherwise read as "claimed" forever.
const GOTO_CLAIM_WINDOW_MS = 45000;

/**
 * Pure: decides what one gotoDashboards trigger for `messageId` should do,
 * given whatever raw value is CURRENTLY at gotoFiredKey(messageId). Two
 * synchronous callers threading the same value through it (the second using
 * whatever the first returned as `stamp`) behave exactly as two real
 * localStorage round trips would - the shape both the source wiring and a
 * test exercising the race use.
 *
 * @param {string|null} raw the localStorage value at gotoFiredKey(messageId)
 * @param {number} [now] fired-at timestamp for a fresh claim
 * @returns {{build: true, stamp: string}|{build: false, conv: string}}
 *   build: true  - unclaimed (or a claim past its window) - the caller must
 *                  persist `stamp` before it does anything else, then build.
 *   build: false - already claimed; resume `conv`, which is "" while the
 *                  claiming send() is still in flight.
 */
export function claimGotoFire(raw, now = Date.now()) {
	const stamp = parseFiredStamp(raw);
	if (stamp && (stamp.conv || now - stamp.t < GOTO_CLAIM_WINDOW_MS)) {
		return { build: false, conv: stamp.conv };
	}
	return { build: true, stamp: encodeFiredStamp(now) };
}
