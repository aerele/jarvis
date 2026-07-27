// "Open in Dashboards" — the way back from a builder conversation in MAIN CHAT.
//
// The Dashboards builder's thread is an ordinary Jarvis Conversation, so it
// also shows up in the main chat list. Opened there, the agent's html artifact
// renders as a static preview: main chat's canvas has no query-tool bridge, so
// nothing in it fetches data. That is accepted — what was missing is the door
// back to the page where the same document DOES run with data.
//
// Nothing in a message says "this is a dashboard": an html canvas in main chat
// is just an html canvas (dashboardRestore.js documents the same trap on the
// builder side). The marker lives on the CONVERSATION — `origin_page`, stamped
// server-side from the builder's send context — so the affordance is a
// property of the conversation plus the item type, never of the html itself.
//
// Both ends of the hand-off keep their decisions here, as pure functions: what
// main chat routes to, what the builder would cost the user by accepting the
// promotion, and what identity it comes away with. A .vue SFC cannot be
// imported into the plain node test runner, so logic that lives in one is logic
// nothing can test behaviourally.
import { themeKey } from "./dashboardThemes.js";

/**
 * Does this canvas item, in this conversation, get the affordance?
 *
 * `originPage` is read from a conversation fetch that lands one full round trip
 * after `currentId` already names the conversation being switched to, so the
 * value on its own is not enough: it must be BOUND to the conversation it was
 * read for (`originOf`). Until the new conversation's fetch lands — and forever,
 * if that fetch rejects — the pair does not match and the gate stays closed,
 * which is the fail-closed half. The other half is that a refresh of the
 * conversation ALREADY on screen (a `message:enriched` reload, the resync on
 * every tab focus) never invalidates anything, so the button does not blink out
 * from under the user at the moment it is most likely to be clicked.
 *
 * Same shape as the preview header's own `artifact.conv === currentId` gate.
 *
 * @param {{originPage: string, originOf: string, conversation: string, cv: {type?: string}|null}} arg
 */
export function canOpenInDashboards({ originPage, originOf, conversation, cv }) {
	if (originPage !== "dashboards") return false;
	if (!conversation || originOf !== conversation) return false;
	return !!cv && cv.type === "html";
}

// Frappe timestamps arrive as "YYYY-MM-DD HH:MM:SS[.ffffff]" — same server,
// same format on both sides of the comparison. Normalised to a fixed-width key
// so a string compare is exact. Date.parse is deliberately NOT used: its
// handling of more than three fractional-second digits is implementation-
// defined, and these values carry six.
function stampKey(v) {
	const m = (typeof v === "string" ? v : "")
		.trim()
		.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?/);
	if (!m) return "";
	return m.slice(1, 7).join("") + "." + (m[7] || "").padEnd(6, "0").slice(0, 6);
}

/**
 * Is `a` strictly later than `b`? Unreadable/absent on either side answers
 * `false` — the caller's fallback is the pre-existing behaviour, so an old
 * server that returns no `creation` degrades to it instead of misrouting.
 */
export function isNewerStamp(a, b) {
	const ka = stampKey(a);
	const kb = stampKey(b);
	return !!ka && !!kb && ka > kb;
}

/**
 * Where the affordance goes.
 *
 * A saved dashboard bound to this conversation opens IN PLACE (`?edit=`): the
 * builder loads the stored document, runs its sources live, and saves back
 * over the same row. With no saved row there is nothing to edit, so the
 * builder is asked to promote THIS artifact instead (`?chat=&canvas=`) and
 * replays it from the transcript.
 *
 * The clicked message wins over an older save. A conversation keeps iterating
 * after its first save, so "this conversation has a dashboard" is not "this
 * artifact IS that dashboard" — routing a click on a newer, never-saved build
 * to `?edit=` opens a different document and leaves the one the user pointed
 * at unreachable. Compared on `creation`, not `modified`: editing the saved row
 * later must not re-hijack clicks on builds that came after it.
 *
 * A message with a canvas and NO `creation` is the row being streamed into
 * right now: main chat mints it client-side from the first delta and the
 * realtime `canvas` frame attaches the artifact to it, so it carries no server
 * stamp until the next transcript load. That row is by definition the newest
 * thing in the thread — and the build the user just watched appear is exactly
 * the one they are most likely to click — so it promotes. An unreadable stamp
 * is a different story (an old server, not a live frame) and keeps the
 * pre-existing `?edit=` behaviour, as does a saved row that reports no
 * `creation` of its own.
 *
 * Promoting DESPITE a saved row carries that row's name along (`&dash=`). The
 * conversation already has an identity — the build being promoted is a later
 * iteration of the SAME dashboard, not a different one — so the builder adopts
 * it and "Save changes" updates that row instead of writing a second one for
 * content the first already holds. Only this fork can carry it: with no saved
 * row there is nothing to adopt, and `?edit=` opens the row directly.
 *
 * @param {{dashboard: {name?: string, creation?: string}|null, conversation: string, messageId: string, messageCreation?: string}} arg
 * @returns {{path: string, query: object}} a vue-router location
 */
export function dashboardOpenRoute({ dashboard, conversation, messageId, messageCreation }) {
	const saved = (dashboard && dashboard.name) || "";
	const promote = {
		path: "/dashboards",
		query: { chat: conversation || "", canvas: messageId || "" },
	};
	if (!saved) return promote;
	const adopt = { path: "/dashboards", query: { ...promote.query, dash: saved } };
	if (!messageCreation) return adopt;
	if (isNewerStamp(messageCreation, dashboard.creation)) return adopt;
	return { path: "/dashboards", query: { edit: saved } };
}

/**
 * Would accepting a `?chat=&canvas=` promotion cost the user something they
 * have to own? (DashboardsPage's discard confirm, extracted so it is testable.)
 *
 * Re-opening the builder's OWN thread is free: both messages live in the same
 * transcript, so repointing the canvas at another of them loses nothing — and
 * that is the feature's primary path, where a confirm is pure noise (its
 * "Open its chat" action would even bounce the user back where they started).
 * WITH an editing identity it is not free: accepting flips Save from
 * update-in-place to create-new, which is a real change the user must own.
 *
 * A different conversation keeps the full guard: an unsaved canvas, an editing
 * target, or another thread's restored canvas are all things to ask about.
 *
 * An ADOPTION (`dash` — the saved row this build belongs to) changes only the
 * editing leg: the identity is not being thrown away, it is being kept, or a
 * builder that had none is upgrading to update-in-place. Nothing to own, so
 * nothing to ask. A DIFFERENT row is still a real editing session being
 * replaced, and every other leg of the guard is untouched — an adoption does
 * not licence discarding another thread's unsaved canvas.
 *
 * @param {{conv: string, chatConv: string, canvasMsg: string, unsavedCanvas: boolean, editing: boolean, editingName?: string, dash?: string}} arg
 */
export function wouldDiscardOnPromotion({
	conv,
	chatConv,
	canvasMsg,
	unsavedCanvas,
	editing,
	editingName,
	dash,
}) {
	const losesEditing = dash ? !!editingName && editingName !== dash : !!editing;
	if (chatConv && chatConv === conv) return losesEditing;
	if (unsavedCanvas) return true;
	if (losesEditing) return true;
	return !!(chatConv && chatConv !== conv && canvasMsg);
}

// ── adoption: the SAVE identity is not the REVISION identity ────────────────
//
// An adopted promotion holds two documents at once. The canvas shows the build
// the user clicked — a later iteration than the row it belongs to — while the
// row supplies the identity, so "Save changes" reconciles them instead of
// writing a duplicate. Until that Save (or an `?edit=` that loads the row's own
// html), the TRANSCRIPT is the current document and the stored row is history.
// Everything below follows from that one asymmetry.

/**
 * What the builder chat tells the agent it is revising.
 *
 * NOT the Save target. `sendDashboardChat`'s editing name becomes a
 * `{doctype, name}` context, and the turn handler turns that into "they are
 * revising the saved dashboard <name> — call jarvis__get_doc … then produce the
 * full revised document". Pointed at a row the canvas is already AHEAD of, that
 * instruction makes the next small tweak ("make the totals bold") rebuild from
 * the STORED html: the promoted build is silently reverted, and — because the
 * adoption also armed Save — the next Save writes that reversion over the row.
 *
 * So while an adoption is active the agent is told nothing and revises from the
 * transcript, exactly as an identity-less promotion does. The moment the two
 * documents are one again, the name goes back on.
 *
 * @param {{editingName: string, adoptionActive: boolean}} arg
 */
export function agentRevisionTarget({ editingName, adoptionActive }) {
	return adoptionActive ? "" : editingName || "";
}

/**
 * Is this mount resuming an adopted promotion rather than an edit session?
 *
 * Every route change remounts the builder (there is no <KeepAlive>), so "promote
 * → hop to the chat → come back" is an ordinary move. The sticky editing target
 * alone cannot tell the two states apart, and read as an edit target it sends
 * the mount through `loadEdit`, which puts the ROW's html on the canvas and
 * clears the canvas message — answering with an older document than the one the
 * user left, and forgetting the promoted build.
 *
 * The adopted state has its own signature: the row an adoption named, the same
 * row still the sticky target, plus the canvas message and the thread it was
 * promoted from. All four, or it is an ordinary edit session (`loadEdit` clears
 * the canvas message precisely because the canvas is the row's document then).
 * An explicit `?edit=` is the user asking for a document and always wins.
 *
 * @param {{routeEdit: string, adoptedRow: string, editingSticky: string, canvasMsg: string, chatConv: string}} arg
 */
export function resumesAdoption({ routeEdit, adoptedRow, editingSticky, canvasMsg, chatConv }) {
	if (routeEdit) return false;
	return !!adoptedRow && adoptedRow === editingSticky && !!canvasMsg && !!chatConv;
}

/**
 * The identity a promotion comes away with.
 *
 * `dash` is the row main chat named, `detail` what `get_dashboard` answered for
 * it (null when the fetch failed), `gone` whether that failure was a definitive
 * "no such row for you", `priorName` the identity the builder already had.
 * Three rules, in order:
 *
 * - Adopt only an EDITABLE row. `dash` arrives on a URL and `get_dashboard` is
 *   read-gated, while `save_dashboard` demands owner/admin — so an Org- or
 *   Role-scoped row this user may merely read would otherwise get the badge and
 *   a "Save changes" that throws a PermissionError after the dialog is filled.
 * - A fetch that never ANSWERED, while `dash` is the identity the builder
 *   already had, keeps that identity. The confirm was SKIPPED on the promise
 *   that the row is kept (see wouldDiscardOnPromotion); clearing it on a blip
 *   breaks that promise silently and the next Save writes the duplicate anyway.
 *   A fetch that answered "you may not edit this" is not a blip — it is the
 *   rule above, and it wins. Neither is a 404/403: `get_dashboard` is read-
 *   gated, so a deleted row (or one this user may no longer touch) THROWS
 *   rather than answering `can_edit: false`, and `gone` is how the caller says
 *   the fetch DID answer. The identity naming that row is forgotten — the same
 *   thing the remount path does with a sticky it can no longer resolve.
 * - Anything else degrades to the identity-less promotion that shipped before.
 *
 * The row's `theme` rides along: the agent must design for the theme the row
 * actually has, and the save-time validator re-lints the html against it — a
 * Slate row saved with the picker left on the default is rejected outright.
 *
 * @param {{dash?: string, detail: {name?: string, can_edit?: boolean|number, theme?: string}|null, priorName?: string, gone?: boolean}} arg
 * @returns {{adopted: object|null, keepPrior: boolean, name: string, theme: string}}
 */
export function adoptionIdentity({ dash, detail, priorName, gone }) {
	const answered = !!(detail && detail.name);
	const adopted = dash && answered && detail.can_edit ? detail : null;
	const keepPrior = !answered && !gone && !!dash && !!priorName && priorName === dash;
	return {
		adopted,
		keepPrior,
		name: adopted ? adopted.name : keepPrior ? priorName : "",
		theme: adopted ? themeKey(adopted.theme) : "",
	};
}
