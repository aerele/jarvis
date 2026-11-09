// The ```jarvis-goto contract, as a plain function: parse a fenced block into
// a normalised {page, prompt} pair, or null.
//
// Extracted the same way chatAsk.js is, so ChatView.vue's gotoOf(m) stays a
// thin cached wrapper and the parsing itself is unit-testable without
// mounting the view (jarvis#884: dashboard builds move off the agent canvas,
// and the agent redirects here instead of building inline).
//
// This is deliberately an ALLOWLIST, not generic navigation: the agent picks
// a page key and a request restatement, never a route. The keys are the ones in
// jarvis/public/js/shared/goto_pages.mjs (DASHBOARDS_PAGE, PAYMENT_CALENDAR_PAGE);
// a block naming anything else is not a valid goto and is left for the raw
// markdown fallback to show.

import {
	DASHBOARDS_PAGE,
	GOTO_PAGES,
	PAYMENT_CALENDAR_PAGE,
} from "../../../jarvis/public/js/shared/goto_pages.mjs";
import { __ } from "./i18n.js";

export const GOTO_RE = /```jarvis-goto[ \t]*\n([\s\S]*?)```/;

const ALLOWED_PAGES = new Set(Object.keys(GOTO_PAGES));

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

// ── the builder's first message ──────────────────────────────────────────────
//
// The goto `prompt` is the agent's restatement of the request, and a
// restatement can drop a detail the user gave: asked for "the connecting lanes
// diagram", the builder got per-item coverage and built no lanes
// (Aerele-RnD/jarvis-admin-v2#740, use case 3). So the user's own words ride
// along under the restatement: the last few user turns, because the ask is not
// always the latest one (a "yes" to "shall I build it?" carries no detail).
// Capped, keeping the newest words, so a pasted wall of text cannot crowd the
// builder's context; left out when the restatement already quotes them whole.

export const REQUEST_WORDS_MAX = 2000;
export const REQUEST_TURNS_MAX = 3;

const MACRO_ROW = "▶ Running macro";
const ATTACHMENT_LINE = /^📎 .*$/gm;

/**
 * The user's words a goto answers: up to REQUEST_TURNS_MAX user turns before it,
 * oldest first, stopping at an earlier Dashboards hand-off. Macro rows and the "📎 file"
 * marker are not the user's words.
 * @param {Array<{name: string, role: string, content: string}>} messages
 * @param {string} messageId the assistant message carrying the goto block
 * @returns {string} "" when the message is not in the list or no user words precede it
 */
export function requestBefore(messages, messageId) {
	const list = Array.isArray(messages) ? messages : [];
	const at = list.findIndex((m) => m && m.name === messageId);
	const turns = [];
	for (let i = at - 1; i >= 0 && turns.length < REQUEST_TURNS_MAX; i--) {
		const m = list[i];
		if (!m) continue;
		// Stops at an earlier DASHBOARDS hand-off only: those words were already sent
		// to the builder. A payment-calendar hand-off never reaches the builder, so
		// the request behind it is still the user's ask if they go on to Dashboards.
		if (m.role === "assistant" && parseGoto(m.content)?.page === DASHBOARDS_PAGE) break;
		if (m.role !== "user" || !m.content || m.content.startsWith(MACRO_ROW)) continue;
		const words = String(m.content).replace(ATTACHMENT_LINE, "").trim();
		if (words) turns.unshift(words);
	}
	return turns.join("\n");
}

/**
 * What the builder is sent: the restatement, then the user's own words.
 * @param {string} prompt the goto block's restatement
 * @param {string} request the user's words (requestBefore)
 * @returns {string}
 */
export function builderSeed(prompt, request) {
	const brief = String(prompt || "").trim();
	// Code points, not UTF-16 units, so the cut never splits an emoji.
	const chars = Array.from(String(request || "").trim());
	const words =
		chars.length > REQUEST_WORDS_MAX
			? "…" + chars.slice(-REQUEST_WORDS_MAX).join("")
			: chars.join("");
	if (!words || brief.includes(words)) return brief;
	return `${brief}\n\nIn my own words: ${words}`;
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

const GOTO_FIRED_PREFIX = "jarvis:goto-fired:";
const GOTO_STAMPS_KEPT = 50;

export function gotoFiredKey(messageId) {
	return GOTO_FIRED_PREFIX + messageId;
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

/**
 * Bounded stamp set: the stamps are write-once per redirect and would otherwise
 * accumulate for the life of the origin. Keeps the newest `keep` (the fired-stamp
 * check is the real guard; this only defends reloads of RECENT transcripts). An
 * unreadable stamp sorts oldest.
 * @param {Storage} storage localStorage, or a stand-in with length/key/getItem/removeItem
 * @param {number} [keep]
 */
export function pruneGotoStamps(storage, keep = GOTO_STAMPS_KEPT) {
	const stamps = [];
	for (let i = 0; i < storage.length; i++) {
		const k = storage.key(i);
		if (k && k.startsWith(GOTO_FIRED_PREFIX)) {
			const parsed = parseFiredStamp(storage.getItem(k));
			stamps.push([k, parsed ? parsed.t : 0]);
		}
	}
	stamps
		.sort((a, b) => b[1] - a[1])
		.slice(keep)
		.forEach(([k]) => storage.removeItem(k));
}

// ── the page map and the decision ───────────────────────────────────────────
//
// One row per allowed page: the card's title and button, the morph line, and the
// fixed SPA route. The route name comes only from GOTO_PAGES; the block never
// supplies one. Copy goes through __() when asked for (at render time), not when
// this module loads, so a page translator that arrives later still applies.
const PAGE_COPY = {
	[DASHBOARDS_PAGE]: () => ({
		title: __("Continue in Dashboards"),
		button: __("Open Dashboards"),
		morph: __("Taking you to the Dashboards builder"),
	}),
	[PAYMENT_CALENDAR_PAGE]: () => ({
		title: __("Continue in Payment Calendar"),
		button: __("Open Payment Calendar"),
		morph: __("Taking you to the Payment Calendar"),
	}),
};

/**
 * @param {string} page a key of GOTO_PAGES
 * @returns {{page: string, title: string, button: string, morph: string, route: {name: string}}|null}
 */
export function gotoPageInfo(page) {
	if (!ALLOWED_PAGES.has(page) || !PAGE_COPY[page]) return null;
	return { page, ...PAGE_COPY[page](), route: { name: GOTO_PAGES[page] } };
}

/**
 * What the card says for a decision: the title and button of the page the button
 * will OPEN (the decision's action), and a subtitle.
 *
 * Dashboards: the subtitle is the agent's restated request, as before.
 * payment-calendar: FIXED copy, never the prompt. The restatement reads as the
 * request done ("Per-day payables for May"), and the calendar does not apply it,
 * so showing it would promise something the page does not do. When the user
 * cannot open the calendar the card falls back to Dashboards and says why.
 *
 * @param {{page: string, prompt: string}|null} goto from parseGoto
 * @param {{card: boolean, action: string|null}|null} decision from gotoDecision
 * @returns {{title: string, button: string, sub: string}|null}
 */
export function gotoCardCopy(goto, decision) {
	if (!goto || !decision || !decision.card) return null;
	const info = gotoPageInfo(decision.action);
	if (!info) return null;
	let sub = goto.prompt;
	if (goto.page === PAYMENT_CALENDAR_PAGE) {
		sub =
			decision.action === PAYMENT_CALENDAR_PAGE
				? __("Day-by-day supplier dues, cash and collections, one month at a time.")
				: __("The Payment Calendar isn't available for your account.");
	}
	return { title: info.title, button: info.button, sub };
}

// An opener is enough: a half-streamed or later-closed block is still a card or a
// question the user has to answer, and the calendar must not move them away from it.
const INTERACTIVE_FENCE_RE = /```(?:jarvis-ask|jarvis-action|confirm)[ \t]*(?:\n|$)/;

/**
 * Whether a message carries a block the user has to answer in the chat itself: a
 * clarifying question (jarvis-ask), a draft/action card (jarvis-action) or a
 * confirmation (confirm).
 * @param {string} content raw assistant message text
 * @returns {boolean}
 */
export function hasInteractiveFence(content) {
	return INTERACTIVE_FENCE_RE.test(String(content || ""));
}

const TEXT_INPUT_TYPES = new Set([
	"",
	"text",
	"search",
	"email",
	"url",
	"tel",
	"password",
	"number",
	"date",
	"datetime-local",
	"month",
	"time",
	"week",
]);

/**
 * Whether the user is typing in a field inside the chat view other than the main
 * composer (its text is the `input` draft check; being focused while EMPTY is the
 * normal state after a send and must not block). Duck-typed so it is unit-testable
 * without a DOM.
 * @param {{tagName?: string, type?: string, isContentEditable?: boolean}|null} active document.activeElement
 * @param {{contains: function}|null} root the chat view's root element
 * @param {object|null} composer the main composer's own textarea
 * @returns {boolean}
 */
export function focusInsideField(active, root, composer) {
	if (!active || !root || active === composer || !root.contains(active)) return false;
	if (active.isContentEditable) return true;
	const tag = String(active.tagName || "").toUpperCase();
	if (tag === "TEXTAREA") return true;
	return tag === "INPUT" && TEXT_INPUT_TYPES.has(String(active.type || "").toLowerCase());
}

/**
 * Whether a modal or dialog is open anywhere on the page (frappe-ui / headless-ui
 * dialogs render role="dialog" with aria-modal, teleported outside the chat view).
 * @param {{querySelector: function}|null} doc
 * @returns {boolean}
 */
export function modalIsOpen(doc) {
	return !!doc?.querySelector?.('[role="dialog"], [aria-modal="true"], dialog[open]');
}

/**
 * Pure: what one parsed goto does in this tab right now.
 *
 * Dashboards: today's logic, unchanged - the card always, and the automatic
 * redirect when the turn is live, did not error, was not stopped, and has no
 * fired stamp. Nothing about the composer, the queue, or the tab matters.
 *
 * payment-calendar: the same, and also only when the user can open the page
 * (canView), this tab sent the turn (ownTurn), nothing is half-typed or open
 * (draft: composer text, an attachment, a recording, another field being typed
 * in, or a dialog open), no turn of this tab is queued, and the same message has
 * no other interactive block for the user to answer (interactive). canView false
 * means the card's action is Dashboards (with the prompt) and nothing opens by
 * itself. The calendar keeps no stamp: exactly-once is the ownership token
 * ChatView consumes when it opens (so a replayed run:end finds none).
 *
 * @param {{page: string, prompt: string}|null} goto from parseGoto
 * @param {{canView?: boolean, ownTurn?: boolean, draft?: boolean, queued?: boolean,
 *   interactive?: boolean, live?: boolean, error?: boolean, stopped?: boolean,
 *   stamp?: string|null}} [ctx]
 * @returns {{card: boolean, autoOpen: boolean, action: "dashboards"|"payment-calendar"|null}}
 */
export function gotoDecision(goto, ctx = {}) {
	if (!goto || !ALLOWED_PAGES.has(goto.page)) {
		return { card: false, autoOpen: false, action: null };
	}
	const { canView, ownTurn, draft, queued, interactive, live, error, stopped, stamp } = ctx;
	const settled = !!live && !error && !stopped && !stamp;
	if (goto.page === PAYMENT_CALENDAR_PAGE) {
		if (canView !== true) return { card: true, autoOpen: false, action: DASHBOARDS_PAGE };
		return {
			card: true,
			autoOpen: settled && !!ownTurn && !draft && !queued && !interactive,
			action: PAYMENT_CALENDAR_PAGE,
		};
	}
	return { card: true, autoOpen: settled, action: DASHBOARDS_PAGE };
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
