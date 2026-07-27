// "Open in Dashboards" — the owner-reported gap between main chat and the
// Dashboards builder, fenced by a real executable test. Plain node built-ins
// (node:test + node:assert), like dashboardRestore.test.js. Run directly
// (`node --test dashboardOpen.test.js`) or via the python suite
// (jarvis/tests/test_dashboard_open_from_chat_client.py subprocess-runs it
// every CI run).
//
// The builder's conversation shows up in the main chat list. Opening it there
// shows the prompt and the dashboard, but that canvas never runs its queries —
// accepted — so what was missing was a way back to the page where it does.
// Every DECISION the feature makes is a pure function in dashboardOpen.js and
// is tested by behaviour below: who gets the affordance, where a click goes
// (including which of two documents wins on time), and what a promotion would
// cost the user. Only component WIRING is fenced by source assertions (the
// dashboardRestore precedent), because a .vue SFC cannot be imported into a
// plain node runner.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
	canOpenInDashboards,
	dashboardOpenRoute,
	isNewerStamp,
	wouldDiscardOnPromotion,
} from "./dashboardOpen.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const read = (...p) => fs.readFileSync(path.join(HERE, ...p), "utf8");
const chatSrc = read("..", "views", "ChatView.vue");
const pageSrc = read("..", "pages", "dashboards", "DashboardsPage.vue");
const paneSrc = read("..", "pages", "dashboards", "DashboardChatPane.vue");
const apiSrc = read("..", "api", "dashboards.js");

// The body of a named declaration: everything up to its closing brace, which in
// these tab-indented sources is the first `}` in column 0 (dashboardRestore's
// helper — a loose slice would let an assertion pass on the NEXT declaration).
const fnBody = (src, decl) => {
	const start = src.indexOf(decl);
	assert.notEqual(start, -1, `source must still define ${decl}`);
	const end = src.indexOf("\n}", start + decl.length);
	assert.notEqual(end, -1, `${decl} must close at the top level`);
	return src.slice(start, end);
};

// ---- who gets the affordance ---------------------------------------------

// A conversation whose origin has been read and belongs to the thread on screen.
const gate = (o) =>
	canOpenInDashboards({
		originPage: "dashboards",
		originOf: "c1",
		conversation: "c1",
		cv: { type: "html" },
		...o,
	});

test("only an html artifact of a DASHBOARDS conversation gets the button", () => {
	assert.equal(gate({}), true);
	// same conversation, other artifact types: untouched
	assert.equal(gate({ cv: { type: "image" } }), false);
	assert.equal(gate({ cv: { type: "pdf" } }), false);
	assert.equal(gate({ cv: { type: "svg" } }), false);
	assert.equal(gate({ cv: { type: "file" } }), false);
	// same artifact, other conversations: untouched. Triggers conversations are
	// stamped too, but this change ships the dashboards button only.
	assert.equal(gate({ originPage: "triggers" }), false);
	assert.equal(gate({ originPage: "" }), false);
	assert.equal(gate({ originPage: undefined }), false);
});

test("a junk canvas item never throws", () => {
	assert.equal(gate({ cv: null }), false);
	assert.equal(gate({ cv: undefined }), false);
	assert.equal(gate({ cv: {} }), false);
});

test("the origin is BOUND to the conversation it was read for", () => {
	// Refreshing the conversation already on screen (message:enriched fires one
	// immediately after every canvas enrichment; onResync on every socket connect
	// and every tab focus) writes nothing until its answer lands, and what it then
	// writes is the same pair — so the button never blinks out from under a user
	// at the moment they are most likely to click it.
	assert.equal(gate({}), true);
	// A SWITCH moves currentId a full round trip ahead of the new origin. Until
	// that fetch lands the pair still describes the PREVIOUS conversation — whose
	// transcript is still the one on screen — so the gate is closed.
	assert.equal(gate({ conversation: "c2" }), false);
	// ...and opens for c2 only once c2's own fetch has written its own pair
	assert.equal(gate({ originOf: "c2", conversation: "c2" }), true);
	assert.equal(gate({ originPage: "", originOf: "c2", conversation: "c2" }), false);
	// A fetch that REJECTS (a blip, a 500, an abort on tab wake) writes neither
	// half, so the binding stays on the conversation it was read for and the gate
	// stays closed for the one switched to — instead of offering the way back over
	// a thread nothing has vouched for.
	assert.equal(gate({ originOf: "c1", conversation: "c2" }), false);
	// the id-less states (welcome screen, a dropped conversation) satisfy nothing
	assert.equal(gate({ originOf: "", conversation: "" }), false);
	assert.equal(gate({ originOf: null, conversation: null }), false);
	assert.equal(gate({ originOf: undefined, conversation: undefined }), false);
});

// ---- where it goes --------------------------------------------------------

const SAVED = { name: "DASH-1", dashboard_title: "Sales", creation: "2026-07-27 10:00:00.000000" };
const BEFORE = "2026-07-27 09:59:59.999999";
const AFTER = "2026-07-27 10:00:00.000001";

test("a saved dashboard opens IN PLACE, so Save keeps updating that row", () => {
	assert.deepEqual(
		dashboardOpenRoute({
			dashboard: SAVED,
			conversation: "c1",
			messageId: "m1",
			messageCreation: BEFORE,
		}),
		{ path: "/dashboards", query: { edit: "DASH-1" } }
	);
	// the artifact that IS the saved document (same instant) is not "newer"
	assert.deepEqual(
		dashboardOpenRoute({
			dashboard: SAVED,
			conversation: "c1",
			messageId: "m1",
			messageCreation: SAVED.creation,
		}),
		{ path: "/dashboards", query: { edit: "DASH-1" } }
	);
});

test("an unsaved build is promoted from the transcript instead", () => {
	const promote = { path: "/dashboards", query: { chat: "c1", canvas: "m1" } };
	assert.deepEqual(
		dashboardOpenRoute({ dashboard: null, conversation: "c1", messageId: "m1" }),
		promote
	);
	// {} is the server's ordinary "no saved dashboard" answer, not an error
	assert.deepEqual(
		dashboardOpenRoute({ dashboard: {}, conversation: "c1", messageId: "m1" }),
		promote
	);
	assert.deepEqual(
		dashboardOpenRoute({ dashboard: { name: "" }, conversation: "c1", messageId: "m1" }),
		promote
	);
	// ...and a lookup that never resolved (undefined) still routes somewhere
	assert.deepEqual(
		dashboardOpenRoute({ dashboard: undefined, conversation: "c1", messageId: "m1" }),
		promote
	);
});

test("a build made AFTER the save wins the click, saved row or not", () => {
	// The thread keeps iterating after its first save: "this conversation has a
	// dashboard" is not "this artifact IS that dashboard". Routing a newer
	// artifact to ?edit= answers with a different document and leaves the one
	// the user pointed at unreachable (every later click routes the same way).
	assert.deepEqual(
		dashboardOpenRoute({
			dashboard: SAVED,
			conversation: "c1",
			messageId: "m2",
			messageCreation: AFTER,
		}),
		{ path: "/dashboards", query: { chat: "c1", canvas: "m2", dash: "DASH-1" } }
	);
});

test("editing the saved row later must not re-hijack clicks on newer builds", () => {
	// The comparison is on `creation`, never `modified` — otherwise touching the
	// old dashboard once puts it back in front of every artifact built since.
	const editedJustNow = { ...SAVED, modified: "2099-01-01 00:00:00.000000" };
	assert.deepEqual(
		dashboardOpenRoute({
			dashboard: editedJustNow,
			conversation: "c1",
			messageId: "m2",
			messageCreation: AFTER,
		}),
		{ path: "/dashboards", query: { chat: "c1", canvas: "m2", dash: "DASH-1" } }
	);
});

test("a promotion DESPITE a saved row carries that row's identity", () => {
	// The build being promoted is a later iteration of the same dashboard, not a
	// different one. Without the name the builder promotes with no identity, so
	// the next "Save" writes a SECOND row for content the first already holds —
	// which on the live-tweak path (no stamp yet) is every single click.
	for (const messageCreation of [AFTER, undefined, "", null]) {
		assert.equal(
			dashboardOpenRoute({
				dashboard: SAVED,
				conversation: "c1",
				messageId: "m2",
				messageCreation,
			}).query.dash,
			"DASH-1"
		);
	}
	// nothing to adopt with no saved row, and ?edit= opens the row itself
	assert.equal(
		"dash" in
			dashboardOpenRoute({ dashboard: null, conversation: "c1", messageId: "m1" }).query,
		false
	);
	assert.equal(
		"dash" in
			dashboardOpenRoute({
				dashboard: SAVED,
				conversation: "c1",
				messageId: "m1",
				messageCreation: BEFORE,
			}).query,
		false
	);
});

test("the build still being streamed promotes: no stamp yet means it IS the newest", () => {
	// Main chat mints the assistant row from the first delta ({name, role,
	// content, streaming}) and the realtime `canvas` frame hangs the artifact on
	// that same row, so it carries no server `creation` until the next transcript
	// load. That is the normal state at the exact moment the card appears — the
	// likeliest moment to click it — and ?edit= there answers with the older
	// saved row instead of the build the user just watched being drawn.
	for (const messageCreation of [undefined, "", null]) {
		assert.deepEqual(
			dashboardOpenRoute({
				dashboard: SAVED,
				conversation: "c1",
				messageId: "m2",
				messageCreation,
			}),
			{ path: "/dashboards", query: { chat: "c1", canvas: "m2", dash: "DASH-1" } }
		);
	}
});

test("an unreadable stamp keeps the pre-existing ?edit= behaviour", () => {
	// A stamp that is present but unparseable is an old/odd server, not a live
	// frame: degrade to what shipped rather than guessing.
	assert.deepEqual(
		dashboardOpenRoute({
			dashboard: SAVED,
			conversation: "c1",
			messageId: "m2",
			messageCreation: "nonsense",
		}),
		{ path: "/dashboards", query: { edit: "DASH-1" } }
	);
	// ...and so does a saved row that reports no `creation` of its own
	assert.deepEqual(
		dashboardOpenRoute({
			dashboard: { name: "DASH-1" },
			conversation: "c1",
			messageId: "m2",
			messageCreation: AFTER,
		}),
		{ path: "/dashboards", query: { edit: "DASH-1" } }
	);
});

test("frappe timestamps compare exactly, microseconds and all", () => {
	assert.equal(isNewerStamp("2026-07-27 10:00:00.000002", "2026-07-27 10:00:00.000001"), true);
	assert.equal(isNewerStamp("2026-07-27 10:00:00.000001", "2026-07-27 10:00:00.000002"), false);
	// equal is not newer, however the two sides spell it
	assert.equal(isNewerStamp("2026-07-27 10:00:00", "2026-07-27 10:00:00.000000"), false);
	assert.equal(isNewerStamp("2026-07-27 10:00:01", "2026-07-27 10:00:00.999999"), true);
	// dates and months carry, not just the time
	assert.equal(isNewerStamp("2026-08-01 00:00:00", "2026-07-31 23:59:59.999999"), true);
	assert.equal(isNewerStamp("2026-07-31 23:59:59.999999", "2026-08-01 00:00:00"), false);
	// the ISO separator the wire sometimes uses
	assert.equal(isNewerStamp("2026-07-27T10:00:01", "2026-07-27 10:00:00"), true);
	// anything unreadable answers "not newer" on either side
	for (const bad of ["", null, undefined, "not a date", 12345, {}]) {
		assert.equal(isNewerStamp(bad, "2026-07-27 10:00:00"), false);
		assert.equal(isNewerStamp("2026-07-27 10:00:00", bad), false);
	}
});

// ---- what a promotion would cost -----------------------------------------

const SAME = { conv: "C", chatConv: "C", canvasMsg: "M1", unsavedCanvas: false, editing: false };

test("re-opening the builder's OWN thread never confirms", () => {
	// The feature's primary path: the user built here, went to main chat, and
	// clicked the way back. Both messages live in one transcript, so repointing
	// the canvas loses nothing — and a confirm here is worse than noise, since
	// its "Open its chat" action lands back in the chat they clicked from.
	assert.equal(wouldDiscardOnPromotion(SAME), false);
	// the restored canvas of that same thread reads as "unsaved" on a fresh
	// mount (editingDetail is null) — still nothing to lose
	assert.equal(wouldDiscardOnPromotion({ ...SAME, unsavedCanvas: true }), false);
	// a DIFFERENT message of the same thread, and a builder with no canvas yet
	assert.equal(
		wouldDiscardOnPromotion({ ...SAME, canvasMsg: "M2", unsavedCanvas: true }),
		false
	);
	assert.equal(wouldDiscardOnPromotion({ ...SAME, canvasMsg: "" }), false);
});

test("the same thread WITH an editing identity still confirms", () => {
	// Accepting flips Save from update-in-place to create-new. That is a real
	// identity change and the user has to own it.
	assert.equal(wouldDiscardOnPromotion({ ...SAME, editing: true }), true);
	assert.equal(wouldDiscardOnPromotion({ ...SAME, editing: true, unsavedCanvas: true }), true);
});

test("a different conversation keeps the full guard", () => {
	const other = {
		conv: "D",
		chatConv: "C",
		canvasMsg: "M1",
		unsavedCanvas: false,
		editing: false,
	};
	// another thread's restored canvas
	assert.equal(wouldDiscardOnPromotion(other), true);
	// an unsaved canvas, or an editing target, on their own
	assert.equal(wouldDiscardOnPromotion({ ...other, canvasMsg: "", unsavedCanvas: true }), true);
	assert.equal(wouldDiscardOnPromotion({ ...other, canvasMsg: "", editing: true }), true);
	// ...but a builder holding nothing is not worth a dialog
	assert.equal(wouldDiscardOnPromotion({ ...other, canvasMsg: "" }), false);
	assert.equal(
		wouldDiscardOnPromotion({
			conv: "D",
			chatConv: "",
			canvasMsg: "",
			unsavedCanvas: false,
			editing: false,
		}),
		false
	);
	// a fresh builder that has an unsaved canvas but no thread still asks
	assert.equal(
		wouldDiscardOnPromotion({
			conv: "D",
			chatConv: "",
			canvasMsg: "",
			unsavedCanvas: true,
			editing: false,
		}),
		true
	);
});

test("an ADOPTION does not discard the identity it names, so it does not ask", () => {
	// The live-tweak path: the thread already has a saved dashboard, the user
	// clicks the build they just watched appear, and the promotion carries that
	// row's name. Nothing is lost — the session either continues (same row) or
	// upgrades from no identity to update-in-place — so the "Discard" modal here
	// would be asking the user to own something that is not happening.
	const editing = { ...SAME, editing: true, editingName: "DASH-1" };
	assert.equal(wouldDiscardOnPromotion({ ...editing, dash: "DASH-1" }), false);
	// a builder with no identity at all, adopting one
	assert.equal(
		wouldDiscardOnPromotion({ ...SAME, editing: false, editingName: "", dash: "DASH-1" }),
		false
	);
	// a DIFFERENT row IS a real editing session being replaced
	assert.equal(wouldDiscardOnPromotion({ ...editing, dash: "DASH-2" }), true);
	// ...and with no `dash` at all the shipped rules stand unchanged: the same
	// thread with an identity confirms, because that identity is foreign by
	// construction (a row belonging to this conversation would have been the one
	// main chat looked up).
	assert.equal(wouldDiscardOnPromotion({ ...editing }), true);
	assert.equal(wouldDiscardOnPromotion({ ...SAME, editingName: "" }), false);
});

test("an adoption does not licence discarding the OTHER legs of the guard", () => {
	// `dash` answers "is the editing identity lost?", nothing else. Another
	// thread's unsaved canvas is still another thread's unsaved canvas.
	const other = {
		conv: "D",
		chatConv: "C",
		canvasMsg: "M1",
		unsavedCanvas: false,
		editing: false,
		editingName: "",
		dash: "DASH-1",
	};
	assert.equal(wouldDiscardOnPromotion(other), true);
	assert.equal(wouldDiscardOnPromotion({ ...other, canvasMsg: "", unsavedCanvas: true }), true);
	// ...but a builder holding nothing still does not need a dialog
	assert.equal(wouldDiscardOnPromotion({ ...other, canvasMsg: "" }), false);
});

// ---- main chat: the affordance is conversation state, not html-sniffing ----

test("ChatView binds the origin to the conversation it was read for", () => {
	assert.match(
		chatSrc,
		/import \{ canOpenInDashboards, dashboardOpenRoute \} from "@\/lib\/dashboardOpen";/
	);
	assert.match(chatSrc, /const originPage = ref\(""\);/);
	assert.match(chatSrc, /const originOf = ref\(""\);/);
	const load = fnBody(chatSrc, "async function loadConversation(");
	// the two halves are written together, and only once the fetch has answered
	assert.match(
		load,
		/originPage\.value = d\?\.conversation\?\.origin_page \|\| "";\n\toriginOf\.value = id;/
	);
	assert.ok(
		load.indexOf("await api.getConversation(id)") < load.indexOf("originOf.value = id;"),
		"the pair is written from the response, not before it"
	);
	// NOTHING blanks the pair ahead of the round trip. Eight of loadConversation's
	// call sites refresh the conversation already on screen (message:enriched
	// after every canvas enrichment, onResync on every socket connect and tab
	// focus) — a pre-fetch blanking removes "Open in Dashboards" from the DOM for
	// a full RTT there, and for good when that refetch rejects.
	const preFetch = load.slice(0, load.indexOf("const d = await api.getConversation(id)"));
	assert.equal(
		(preFetch.match(/originPage\.value = "";/g) || []).length,
		1,
		"the only pre-fetch reset is the id-less arm's own"
	);
	const idArm = preFetch.slice(preFetch.indexOf("if (!id) {"));
	assert.match(idArm, /originPage\.value = "";\n\t\toriginOf\.value = "";/);
	// the gate reads the binding, exactly as the preview header reads artifact.conv
	const gate = fnBody(chatSrc, "function canOpenDash(");
	assert.match(gate, /originPage: originPage\.value,/);
	assert.match(gate, /originOf: originOf\.value,/);
	assert.match(gate, /conversation: currentId\.value,/);
});

test("every path that swaps the conversation out resets the pair too", () => {
	// Defence in depth behind the binding: loadConversation is the only OTHER
	// writer and it does not run on any of these (the route watcher no-ops when
	// currentId already equals the new id; the boot arm is the failure of the load
	// itself). A stale "dashboards" left here would need only a matching id to put
	// the button on an ordinary chat's html — with Save armed over it once the
	// builder promotes it.
	const nc = fnBody(chatSrc, "async function newChat(");
	assert.match(nc, /messages\.value = \[\];\n\t\/\/[^]*?originPage\.value = "";\n\toriginOf/);
	const clear = fnBody(chatSrc, "async function clearAllHistory(");
	assert.match(
		clear,
		/messages\.value = \[\];\s*originPage\.value = "";\s*originOf\.value = "";/
	);
	// the boot arm that drops a conversation which has since vanished
	assert.match(
		chatSrc,
		/currentId\.value = null;\s*messages\.value = \[\];\s*originPage\.value = "";\s*originOf\.value = "";/
	);
	// ...and the watcher that drops it when it is deleted from the sidebar while
	// open: the user's next send adopts the server id directly, so nothing
	// re-reads the conversation and the origin would follow them onto it
	const vanish = chatSrc.slice(chatSrc.indexOf("watch(\n\t() => store.conversations,"));
	const vanishBody = vanish.slice(0, vanish.indexOf("\n);"));
	assert.notEqual(vanishBody, "", "the vanished-conversation watcher must still exist");
	assert.match(vanishBody, /currentId\.value = null;/);
	assert.match(vanishBody, /originPage\.value = "";/);
	// ...and the send that ADOPTS a different id: a human send into a conversation
	// the server can no longer find falls back to a fresh one and returns its id
	// (jarvis/chat/api.py), and loadConversation runs on neither side of that
	const send = fnBody(chatSrc, "async function send(");
	const adopt = send.slice(send.indexOf("if (r.conversation_id !== currentId.value) {"));
	assert.notEqual(adopt, "", "the send-adopt fallback must still exist");
	assert.match(adopt, /currentId\.value = r\.conversation_id;[^]*?originPage\.value = "";/);
	assert.match(adopt, /originOf\.value = "";/);
	// six writers of the empty value, no more: the id-less arm plus the five swap
	// sites above (a seventh would mean a path nobody reviewed)
	assert.equal((chatSrc.match(/originPage\.value = "";/g) || []).length, 6);
	// and the binding is dropped with it, every time — one write is never enough
	assert.equal(
		(chatSrc.match(/originOf\.value = "";/g) || []).length,
		(chatSrc.match(/originPage\.value = "";/g) || []).length
	);
});

test("the open artifact carries the conversation it was opened from", () => {
	// The preview overlay is absolute inside ChatView, so the AppShell sidebar
	// stays clickable behind it: without the stamp the header's hand-off pairs
	// the CURRENT conversation with the PREVIOUS one's message id.
	const open = fnBody(chatSrc, "async function openArtifact(");
	assert.match(open, /const conv = currentId\.value;/);
	const assigns = open.match(/artifact\.value = \{[^}]*\}/g) || [];
	assert.equal(assigns.length, 7, "every artifact.value assignment must be accounted for");
	for (const a of assigns) assert.match(a, /\bconv,/);
	// the sheet switcher re-assigns a spread copy, so it keeps the stamp
	assert.match(chatSrc, /artifact\.value = \{ \.\.\.artifact\.value, sheetIdx: si \};/);
});

test("the inline card offers it as a SIBLING button, never nested in the card", () => {
	// .jv-artifact is itself a <button>; a button inside a button is invalid
	// HTML and browsers drop it out of the card entirely.
	assert.match(chatSrc, /<div v-else class="jv-artifact-group">/);
	const group = chatSrc.slice(
		chatSrc.indexOf('<div v-else class="jv-artifact-group">'),
		chatSrc.indexOf("</template>", chatSrc.indexOf('<div v-else class="jv-artifact-group">'))
	);
	assert.match(group, /class="jv-artifact"/);
	assert.match(group, /v-if="canOpenDash\(cv\)"/);
	assert.match(group, /@click="openInDashboards\(m, cv\)"/);
	assert.match(group, /Open in Dashboards/);
	// the button closes the card's <button> first
	assert.ok(
		group.indexOf("</button>") < group.indexOf('v-if="canOpenDash(cv)"'),
		"the affordance must follow the card, not live inside it"
	);
});

test("the open preview panel offers the same hand-off, for its OWN conversation", () => {
	const head = chatSrc.slice(
		chatSrc.indexOf('<div class="jv-artifact-head">'),
		chatSrc.indexOf('<div class="jv-artifact-body">')
	);
	assert.notEqual(head, "", "the artifact preview header must still exist");
	assert.match(head, /v-if="canOpenDash\(artifact\.cv\) && artifact\.conv === currentId"/);
	assert.match(head, /@click="openInDashboards\(artifact\.m, artifact\.cv\)"/);
	assert.match(head, /aria-label="Open in Dashboards"/);
	// alongside the existing open/download actions, not instead of them
	assert.match(head, /title="Open in new tab"/);
	assert.match(head, /title="Download"/);
});

test("a failed lookup degrades to the promotion route — never a dead button", () => {
	const open = fnBody(chatSrc, "async function openInDashboards(");
	assert.match(open, /dashboard = await dashboardForConversation\(conv\);/);
	assert.match(open, /catch \(e\) \{/);
	// the catch must not return: it falls through with no saved dashboard
	const afterCatch = open.slice(open.indexOf("catch (e) {"));
	assert.doesNotMatch(afterCatch, /\breturn;/);
	assert.match(open, /dashboard = null;/);
	// the clicked message's own creation decides the fork
	assert.match(open, /messageId: m\.name,/);
	assert.match(open, /messageCreation: m\.creation,/);
	assert.match(open, /router\.push\(\s*dashboardOpenRoute\(\{/);
	// the panel is closed on the way out; leaving it up over the new page is a
	// modal stranded on a route that never opened it
	assert.ok(
		open.indexOf("closeArtifact();") < open.indexOf("router.push("),
		"close the preview before navigating"
	);
	assert.match(apiSrc, /export const dashboardForConversation = \(conversation\) =>/);
	assert.match(
		apiSrc,
		/call\(DB \+ "dashboard_for_conversation", \{ conversation \}\)\.then\(unwrap\)/
	);
});

test("main chat never writes the builder's sticky slots", () => {
	// The builder owns jarvis-dash-* storage; ChatView routes and nothing else,
	// or two surfaces end up fighting over one slot.
	assert.doesNotMatch(chatSrc, /jarvis-dash-/);
});

// ---- the builder: promoting ?chat=&canvas= --------------------------------

test("the deep-link is read at setup AND watched, like ?edit=", () => {
	assert.match(pageSrc, /const routeChat = typeof route\.query\.chat === "string"/);
	assert.match(pageSrc, /const routeCanvas = typeof route\.query\.canvas === "string"/);
	assert.match(pageSrc, /const routeDash = typeof route\.query\.dash === "string"/);
	const watcher = pageSrc.slice(
		pageSrc.indexOf("watch(\n\t() => [route.query.chat, route.query.canvas],")
	);
	assert.notEqual(watcher, "", "?chat=&canvas= must be watched, not read once at setup");
	const body = watcher.slice(0, watcher.indexOf("\n);"));
	assert.match(body, /promoteFromChat\(conv, msg, \{ dash \}\);/);
	// `dash` rides along in the same route push, so it is read at fire time rather
	// than watched — but it must be read, or the live deep-link adopts nothing
	assert.match(
		body,
		/const dash = typeof route\.query\.dash === "string" \? route\.query\.dash : "";/
	);
	// and the mount path runs it too
	assert.match(
		pageSrc,
		/promoteFromChat\(routeChat, routeCanvas, \{ fallback: normalMount, dash: routeDash \}\)/
	);
});

test("a pending promotion holds the pane's restore off, exactly as ?edit= does", () => {
	// The pane's onMounted fires BEFORE this page's, and the promotion waits on
	// the caps probe — so the transcript restore reliably lands first, and its
	// canvas then reads as unsaved work to the discard guard. routeChat/
	// routeCanvas were already read at setup for this; the hold is what uses it.
	assert.match(pageSrc, /const promotionPending = ref\(!!\(routeChat && routeCanvas\)\);/);
	const onCanvas = fnBody(pageSrc, "async function onCanvas(");
	assert.equal(
		(onCanvas.match(/promotionPending\.value/g) || []).length,
		2,
		"both restore guards — before AND after the get_canvas round trip"
	);
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	const giveUp = promote.slice(
		promote.indexOf("const giveUp = "),
		promote.indexOf("// Validate against the transcript")
	);
	assert.match(giveUp, /promotionPending\.value = false;/);
	// a decline must leave the builder AS IT WAS: the frame the pane emitted
	// while the hold was up was dropped, so ask for it again
	assert.match(giveUp, /chatPane\.value\.restoreCanvas\(\)/);
	assert.match(paneSrc, /defineExpose\(\{ resetChat, sendText, restoreCanvas \}\);/);
	assert.match(
		fnBody(paneSrc, "function restoreCanvas("),
		/emit\("canvas", \{ \.\.\.frame, restore: true \}\)/
	);
	const accept = promote.slice(promote.indexOf("const accept = async () => {"));
	assert.match(accept, /promotionPending\.value = false;/);
	// The hold is armed inside promoteFromChat, BELOW its dedupe check. The query
	// watcher wakes on any route write and would re-arm with a pair already in
	// flight — a call that then dedupes away, leaving an armed hold nothing is
	// going to settle and the pane's restore blocked for the life of the page.
	assert.ok(
		promote.indexOf("if (key === promoting) return;") <
			promote.indexOf("promotionPending.value = true;"),
		"arm the hold below the dedupe check"
	);
	// ...and still above the first await, so no restore can slip in ahead of it
	assert.ok(
		promote.indexOf("promotionPending.value = true;") < promote.indexOf("await "),
		"arm the hold synchronously, before anything yields"
	);
	const watcher = pageSrc.slice(
		pageSrc.indexOf("watch(\n\t() => [route.query.chat, route.query.canvas],")
	);
	const body = watcher.slice(0, watcher.indexOf("\n);"));
	assert.doesNotMatch(body, /promotionPending\.value = true;/);
	// ...and a mount that will NOT promote releases the setup-time hold, or the
	// pane's restore stays blocked for the life of the page
	assert.match(pageSrc, /promotionPending\.value = false;\n\t\tnormalMount\(\);/);
});

test("?edit= wins over ?chat=, and says so", () => {
	const watcher = pageSrc.slice(
		pageSrc.indexOf("watch(\n\t() => [route.query.chat, route.query.canvas],")
	);
	const body = watcher.slice(0, watcher.indexOf("\n);"));
	assert.match(body, /if \(route\.query\.edit\) \{/);
	assert.match(body, /console\.warn\(/);
	assert.ok(
		body.indexOf("route.query.edit") < body.indexOf("promoteFromChat("),
		"the ?edit= check must precede the promotion"
	);
	// the mount path makes the same call
	assert.match(pageSrc, /if \(routeEdit && routeChat\) \{\n\t\tconsole\.warn\(/);
	assert.match(pageSrc, /if \(!routeEdit && routeChat && routeCanvas\) \{/);
});

test("the promotion is validated against the transcript, not the link", () => {
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	assert.match(pageSrc, /import \{ builderCanvasFrame \} from "@\/lib\/dashboardRestore";/);
	assert.match(promote, /await getDashboardConversation\(conversation\)/);
	assert.match(promote, /frame = builderCanvasFrame\(d\.messages \|\| \[\], messageId\);/);
	// a message that is gone, or never drew html, says so and leaves the builder
	// as it was
	assert.match(promote, /if \(!frame\) \{\n\t\tgiveUp\(/);
	assert.match(promote, /giveUp\(errMsg\(e\)\);/);
	const giveUp = promote.slice(promote.indexOf("const giveUp = "));
	assert.match(giveUp, /stripPromotionQuery\(\);/);
	assert.match(giveUp, /if \(fallback\) fallback\(\);/);
});

test("accepting takes over the thread, the identity and the stale data-mode", () => {
	// Without this, Save writes the promoted canvas back over whatever dashboard
	// the builder happened to be editing — and the next send declares a
	// static/live intent the user never expressed for THIS dashboard.
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	const accept = promote.slice(promote.indexOf("const accept = async () => {"));
	for (const line of [
		/editingSticky\.value = adopted \? adopted\.name : "";/,
		/editingDetail\.value = adopted;/,
		/editSeed\.value = "";/,
		/savedName\.value = adopted \? adopted\.name : "";/,
		/chatConv\.value = conversation;/,
		/canvasMsg\.value = messageId;/,
		/dashDataMode\.value = "auto";/,
	]) {
		assert.match(accept, line);
	}
	// the canvas is rebuilt through the normal frame path — that is what re-runs
	// the html with the query tools, i.e. "shown with data"
	assert.match(
		accept,
		/const rendered = await onCanvas\(\{ message_id: frame\.message_id, items: frame\.items \}\);/
	);
});

test("a promoted artifact whose content is gone says so, and shows nothing", () => {
	// An empty canvas alone is indistinguishable from "nothing built yet", and
	// the query is already stripped by then — there would be no way to tell the
	// click did anything. Leaving the PREVIOUS document up instead would arm
	// Save over html that has nothing to do with the thread now underneath it.
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	const accept = promote.slice(promote.indexOf("const accept = async () => {"));
	assert.match(accept, /if \(!rendered\) \{\n\t\t\tbuilderHtml\.value = "";/);
	assert.match(accept, /toast\.error\("Couldn't load that dashboard's content/);
	// builderHtml is NOT cleared before the fetch: promotionPending already holds
	// the pane's restore off, so the only reason to keep the old document up is
	// to avoid flashing an empty canvas on the way to the new one
	assert.ok(
		accept.indexOf("await onCanvas(") < accept.indexOf('builderHtml.value = "";'),
		"the canvas is cleared only after the artifact failed to arrive"
	);
	assert.equal((accept.match(/builderHtml\.value = "";/g) || []).length, 1);
	const onCanvas = fnBody(pageSrc, "async function onCanvas(");
	assert.match(onCanvas, /canvasMsg\.value = message_id;\n\t\treturn true;/);
	// every other exit says "nothing was rendered"
	assert.doesNotMatch(onCanvas, /(?<!return false;\n\t)\breturn;/);
});

test("the promotion ADOPTS the saved row main chat named", () => {
	// The live-tweak click on a thread that already has a dashboard: the build is
	// newer than the row, so it promotes — but it is the SAME dashboard, and
	// dropping the identity makes the next Save write a duplicate.
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	assert.match(
		promote,
		/async function promoteFromChat\(conversation, messageId, \{ fallback = null, dash = "" \} = \{\}\)/
	);
	const accept = promote.slice(promote.indexOf("const accept = async () => {"));
	assert.match(
		accept,
		/if \(dash\) \{\n\t\t\ttry \{\n\t\t\t\tconst d = await getDashboard\(dash\);/
	);
	assert.match(accept, /if \(d && d\.name\) adopted = d;/);
	// a name that fails the (permission-gated) fetch degrades to the identity-less
	// promotion — a URL-supplied `dash` is never a dead click, and never a trusted
	// one either
	assert.match(accept, /\} catch \(e\) \{\n\t\t\t\tadopted = null;/);
	// the adopted row supplies the IDENTITY only: its html must never reach the
	// canvas, or the promotion answers with the saved document instead of the
	// build the user clicked
	assert.doesNotMatch(accept, /builderHtml\.value = (adopted|d)\b/);
	assert.doesNotMatch(accept, /\.html\b/);
	// ...and the identity is what makes Save update in place — the dialog reads it
	// off editingDetail
	assert.match(pageSrc, /:editing-name="editingDetail \? editingDetail\.name : ''"/);
	assert.match(pageSrc, /:editing="editingDetail"/);
	// the query it arrived on is dropped with the rest of the promotion keys
	assert.match(fnBody(pageSrc, "function stripPromotionQuery("), /delete q\.dash;/);
});

test("a promotion that would cost the user something confirms first", () => {
	// The rules themselves are unit-tested above; this fences the wiring.
	const guard = fnBody(pageSrc, "function promotionWouldDiscard(");
	assert.match(guard, /return wouldDiscardOnPromotion\(\{/);
	assert.match(guard, /chatConv: chatConv\.value,/);
	assert.match(guard, /canvasMsg: canvasMsg\.value,/);
	assert.match(guard, /unsavedCanvas: unsavedCanvas\.value,/);
	assert.match(
		guard,
		/editing: !!\(editingSticky\.value \|\| editingDetail\.value \|\| editSeed\.value\),/
	);
	// the adoption matrix needs the identity by NAME, and the name it is being
	// compared against
	assert.match(guard, /editingName: editingName\(\),/);
	assert.match(guard, /\bdash,/);
	assert.match(
		pageSrc,
		/const editingName = \(\) =>\n\teditingSticky\.value \|\| \(editingDetail\.value \|\| \{\}\)\.name \|\| editSeed\.value \|\| "";/
	);
	assert.match(pageSrc, /import \{ wouldDiscardOnPromotion \} from "@\/lib\/dashboardOpen";/);
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	// `force` is required: confirmDiscard short-circuits on !unsavedCanvas, so an
	// editing target alone would never reach the dialog
	assert.match(promote, /confirmDiscard\(accept, \(\) => giveUp\(""\), \{\n\t\tforce: true,/);
	assert.match(promote, /copy: PROMOTE_COPY,/);
	assert.match(
		promote,
		/if \(!promotionWouldDiscard\(conversation, dash\)\) \{\n\t\tawait accept\(\);/
	);
	// the dialog's copy is per-call now, and the promotion's says the chat lives on
	assert.match(pageSrc, /const discardCopy = ref\(DISCARD_COPY\);/);
	assert.match(pageSrc, /title: discardCopy\.title,/);
	assert.match(pageSrc, /message: discardCopy\.message,/);
	assert.match(pageSrc, /Its chat stays in your conversations\./);
	// and the default is restored when the dialog settles, or the next plain
	// discard inherits the promotion's wording
	assert.match(fnBody(pageSrc, "function settleDiscard("), /discardCopy\.value = DISCARD_COPY;/);
});

test("that confirm never offers to open the chat it was clicked from", () => {
	// "Open its chat" is the escape hatch to the thread the builder is holding —
	// an escape only while that is somewhere else. In the case these rules
	// deliberately still confirm (the builder's OWN thread, with an editing
	// identity at stake), it is the conversation the user clicked "Open in
	// Dashboards" in, so the action lands them straight back there.
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	assert.match(promote, /offerChat: chatConv\.value !== conversation,/);
	assert.match(pageSrc, /const discardOfferChat = ref\(true\);/);
	assert.match(pageSrc, /if \(chatConv\.value && discardOfferChat\.value\) \{/);
	const confirm = fnBody(pageSrc, "function confirmDiscard(");
	assert.match(confirm, /offerChat = true/);
	assert.match(confirm, /discardOfferChat\.value = offerChat;/);
	// per-call, exactly like the copy: the next plain discard gets it back
	assert.match(fnBody(pageSrc, "function settleDiscard("), /discardOfferChat\.value = true;/);
});

test("the query is stripped once the promotion settles, both ways", () => {
	// The ?edit= lesson: clearing the seed before the confirm resolves lets a
	// restore land mid-dialog. Same rule here — the route write happens on the
	// settled path, never up front.
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	const accept = promote.slice(promote.indexOf("const accept = async () => {"));
	assert.match(accept, /stripPromotionQuery\(""\);/);
	assert.ok(
		accept.indexOf("await onCanvas(") < accept.indexOf("stripPromotionQuery("),
		"strip after the canvas has settled"
	);
	const strip = fnBody(pageSrc, "function stripPromotionQuery(");
	assert.match(strip, /delete q\.chat;/);
	assert.match(strip, /delete q\.canvas;/);
	assert.match(strip, /router\.replace\(\{ query: q, hash \}\);/);
	// re-entrancy: the watcher re-fires on unrelated route writes, and a confirm
	// dialog spans several of them
	assert.match(pageSrc, /let promoting = "";/);
	assert.match(promote, /if \(key === promoting\) return;/);
	assert.match(promote, /promoting = key;/);
	// ...but a declined or failed promotion can be asked for again
	const giveUp = promote.slice(promote.indexOf("const giveUp = "));
	assert.match(giveUp, /promoting = "";/);
	// ...and so can an ACCEPTED one: a key left behind dedupes every later
	// request for that same pair into silence for the life of the page
	assert.match(accept, /promoting = "";/);
});
