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
// main chat routes to, and what the builder would cost the user by accepting
// the promotion. A .vue SFC cannot be imported into the plain node test runner,
// so logic that lives in one is logic nothing can test behaviourally.

/** Does this canvas item, in this conversation, get the affordance? */
export function canOpenInDashboards(originPage, cv) {
	return originPage === "dashboards" && !!cv && cv.type === "html";
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
 * @param {{dashboard: {name?: string, creation?: string}|null, conversation: string, messageId: string, messageCreation?: string}} arg
 * @returns {{path: string, query: object}} a vue-router location
 */
export function dashboardOpenRoute({ dashboard, conversation, messageId, messageCreation }) {
	const saved = (dashboard && dashboard.name) || "";
	if (saved && !isNewerStamp(messageCreation, dashboard.creation)) {
		return { path: "/dashboards", query: { edit: saved } };
	}
	return {
		path: "/dashboards",
		query: { chat: conversation || "", canvas: messageId || "" },
	};
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
 * @param {{conv: string, chatConv: string, canvasMsg: string, unsavedCanvas: boolean, editing: boolean}} arg
 */
export function wouldDiscardOnPromotion({ conv, chatConv, canvasMsg, unsavedCanvas, editing }) {
	if (chatConv && chatConv === conv) return !!editing;
	if (unsavedCanvas) return true;
	if (editing) return true;
	return !!(chatConv && chatConv !== conv && canvasMsg);
}
