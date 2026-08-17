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
	adoptionIdentity,
	agentRevisionTarget,
	canOpenInDashboards,
	dashboardOpenRoute,
	isNewerStamp,
	resumesAdoption,
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

// ---- adoption: the SAVE identity is not the REVISION identity -------------

const DETAIL = { name: "DASH-1", can_edit: true, theme: "Graphite", html: "<h1>old</h1>" };

test("an adopted promotion tells the agent NOTHING about the row it adopted", () => {
	// The canvas holds a build the adopted row is BEHIND. Naming that row in the
	// send makes the turn handler instruct the agent to call jarvis__get_doc and
	// "produce the full revised document" from the STORED html — so the next
	// "make the totals bold" republishes the older document, reverting the build
	// the user promoted, and the Save the adoption armed then writes that
	// reversion over the row. The transcript is the current document here.
	assert.equal(agentRevisionTarget({ editingName: "DASH-1", adoptionActive: true }), "");
	// An ordinary edit session is untouched: there the row IS what is on the
	// canvas, and reading it back is exactly right.
	assert.equal(agentRevisionTarget({ editingName: "DASH-1", adoptionActive: false }), "DASH-1");
	// ...so the name comes back the moment the two documents are one — the first
	// successful Save, or an ?edit= that loads the row's own html (both clear the
	// adoption, which is all this reads).
	assert.equal(agentRevisionTarget({ editingName: "DASH-2", adoptionActive: false }), "DASH-2");
	// nothing to name, either way
	assert.equal(agentRevisionTarget({ editingName: "", adoptionActive: true }), "");
	assert.equal(agentRevisionTarget({ editingName: "", adoptionActive: false }), "");
	assert.equal(agentRevisionTarget({ editingName: undefined, adoptionActive: false }), "");
});

test("a remount mid-adoption resumes it instead of re-opening the saved row", () => {
	// Every route change remounts the builder (no <KeepAlive> anywhere), so
	// "promote → hop to the chat to re-read it → come back" is an ordinary move.
	const adopted = {
		routeEdit: "",
		adoptedRow: "DASH-1",
		editingSticky: "DASH-1",
		canvasMsg: "M2",
		chatConv: "C",
	};
	assert.equal(resumesAdoption(adopted), true);
	// An ordinary edit session cannot wear this signature: loadEdit clears the
	// canvas message precisely because the canvas becomes the row's document.
	assert.equal(resumesAdoption({ ...adopted, adoptedRow: "", canvasMsg: "" }), false);
	// ...nor can a sticky edit target with no adoption behind it
	assert.equal(resumesAdoption({ ...adopted, adoptedRow: "" }), false);
	// half-states are not the signature: all four, or the ?edit= path
	assert.equal(resumesAdoption({ ...adopted, canvasMsg: "" }), false);
	assert.equal(resumesAdoption({ ...adopted, chatConv: "" }), false);
	// the sticky target moved on (another dashboard was opened for editing)
	assert.equal(resumesAdoption({ ...adopted, editingSticky: "DASH-2" }), false);
	assert.equal(resumesAdoption({ ...adopted, editingSticky: "" }), false);
	// an explicit ?edit= is the user asking for a document, and always wins
	assert.equal(resumesAdoption({ ...adopted, routeEdit: "DASH-3" }), false);
	assert.equal(resumesAdoption({ ...adopted, routeEdit: "DASH-1" }), false);
});

test("only an EDITABLE row is adopted", () => {
	assert.equal(adoptionIdentity({ dash: "DASH-1", detail: DETAIL }).name, "DASH-1");
	assert.equal(adoptionIdentity({ dash: "DASH-1", detail: DETAIL }).adopted, DETAIL);
	// get_dashboard is read-gated while save_dashboard demands owner/admin, and
	// `dash` arrives on a URL — so an Org/Role row this user may merely READ would
	// otherwise get the "Editing <their title>" badge and a "Save changes" that
	// throws a PermissionError only after the dialog has been filled in.
	for (const can_edit of [false, 0, undefined, null]) {
		const r = adoptionIdentity({ dash: "DASH-1", detail: { ...DETAIL, can_edit } });
		assert.equal(r.adopted, null);
		assert.equal(r.name, "");
	}
	// nothing named, nothing answered
	assert.equal(adoptionIdentity({ dash: "", detail: DETAIL }).name, "");
	assert.equal(adoptionIdentity({ dash: "DASH-1", detail: null }).name, "");
	assert.equal(adoptionIdentity({ dash: "DASH-1", detail: {} }).name, "");
	assert.equal(adoptionIdentity({ dash: "DASH-1", detail: undefined }).adopted, null);
});

test("the adoption designs for, and saves against, the ROW's theme", () => {
	// The picker sits on the product default on a fresh mount, and the save-time
	// validator re-lints the html against whatever theme the save sends: a Slate
	// row promoted and saved is otherwise rejected outright, or silently
	// converted. The agent is told the same theme, so it designs for it too.
	assert.equal(adoptionIdentity({ dash: "DASH-1", detail: DETAIL }).theme, "graphite");
	// the capitalised label the DocType stores → the lowercase key the SPA uses
	const themed = (theme) => adoptionIdentity({ dash: "DASH-1", detail: { ...DETAIL, theme } });
	assert.equal(themed("Jarvis").theme, "jarvis");
	assert.equal(themed("Custom").theme, "custom");
	// an unknown/absent label falls back to the product default, never to nothing
	assert.equal(themed("").theme, "jarvis");
	assert.equal(themed(undefined).theme, "jarvis");
	// no adoption, no theme: "" leaves the picker exactly where the user left it
	assert.equal(adoptionIdentity({ dash: "DASH-1", detail: null }).theme, "");
	assert.equal(
		adoptionIdentity({ dash: "DASH-1", detail: { ...DETAIL, can_edit: false } }).theme,
		""
	);
});

test("a fetch blip keeps the identity the skipped confirm promised to keep", () => {
	// wouldDiscardOnPromotion skips the dialog when the builder is already editing
	// the row being adopted, on the promise that the identity is KEPT. A transient
	// get_dashboard failure must not break that promise silently: the next Save
	// would write the very duplicate the adoption exists to prevent, and no
	// confirm was ever shown.
	const blip = adoptionIdentity({ dash: "DASH-1", detail: null, priorName: "DASH-1" });
	assert.equal(blip.keepPrior, true);
	assert.equal(blip.name, "DASH-1");
	assert.equal(blip.adopted, null); // no detail to show — the name is what survives
	// a DIFFERENT prior identity was confirmed away, so it does not survive
	const other = adoptionIdentity({ dash: "DASH-1", detail: null, priorName: "DASH-2" });
	assert.equal(other.keepPrior, false);
	assert.equal(other.name, "");
	// no prior identity, nothing to keep
	assert.equal(
		adoptionIdentity({ dash: "DASH-1", detail: null, priorName: "" }).keepPrior,
		false
	);
	// a successful adoption always answers with the fetched row
	const ok = adoptionIdentity({ dash: "DASH-1", detail: DETAIL, priorName: "DASH-1" });
	assert.equal(ok.keepPrior, false);
	assert.equal(ok.adopted, DETAIL);
	// ...and a fetch that ANSWERED "you may not edit this" is not a blip: it is
	// the can_edit rule, and it wins over the promise
	const denied = adoptionIdentity({
		dash: "DASH-1",
		detail: { ...DETAIL, can_edit: false },
		priorName: "DASH-1",
	});
	assert.equal(denied.keepPrior, false);
	assert.equal(denied.name, "");
});

test("a row that is GONE answered — it is not the blip that keeps the identity", () => {
	// The carve-out above ("a fetch that answered 'you may not edit this'") does
	// not cover the case that actually happens: get_dashboard is read-gated, so a
	// deleted row, or one whose access was revoked mid-session, THROWS — it never
	// comes back as can_edit:false. Read as a blip it would keep an identity whose
	// row no longer exists, and "Save changes" then throws DoesNotExistError /
	// PermissionError with the dialog already filled in.
	const gone = { dash: "DASH-1", detail: null, priorName: "DASH-1", gone: true };
	assert.equal(adoptionIdentity(gone).keepPrior, false);
	assert.equal(adoptionIdentity(gone).name, "");
	assert.equal(adoptionIdentity(gone).adopted, null);
	// a fetch that never answered is still a blip, and still keeps the promise
	assert.equal(adoptionIdentity({ ...gone, gone: false }).keepPrior, true);
	assert.equal(adoptionIdentity({ ...gone, gone: false }).name, "DASH-1");
	// absent reads as "not gone": nothing is kept that was not kept before
	assert.equal(adoptionIdentity({ ...gone, gone: undefined }).keepPrior, true);
	// and it never overrides a fetch that DID answer with a row
	assert.equal(adoptionIdentity({ ...gone, detail: DETAIL }).adopted, DETAIL);
	assert.equal(adoptionIdentity({ ...gone, detail: DETAIL }).name, "DASH-1");
	// the funnel classifies with the same isGoneError the remount path uses —
	// without it every error arrives as `detail = null` and is read as a blip
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	assert.match(promote, /gone = isGoneError\(e\);/);
	assert.match(promote, /adoptionIdentity\(\{ dash, detail, priorName, gone \}\)/);
	assert.match(fnBody(pageSrc, "function isGoneError("), /isPermissionError\(e\)/);
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

test("a dashboard build gets its OWN thumbnail card, not the generic file card", () => {
	// canPromoteDashCanvas(cv) (canOpenDash's builder-origin rule OR
	// isDashboardCanvas's hosted-embed-marker signal) branches BEFORE the
	// generic ".jv-artifact-group" card (issue #858's compact preview
	// thumbnail), so a dashboard canvas never reaches the generic card at
	// all — dashboardBuildCard.test.js fences the thumbnail's own markup
	// (jv-dash-thumb, cvOf srcdoc, openInDashboards) and the combined gate
	// itself. This test only pins the ORDER and the exclusivity: the generic
	// card's v-else must come after the dashboard branch, so every other
	// canvas type is untouched.
	assert.match(chatSrc, /v-else-if="canPromoteDashCanvas\(cv\)"\s*\n\s*class="jv-dash-thumb"/);
	assert.match(chatSrc, /<div v-else class="jv-artifact-group">/);
	assert.ok(
		chatSrc.indexOf('v-else-if="canPromoteDashCanvas(cv)"') <
			chatSrc.indexOf('<div v-else class="jv-artifact-group">'),
		"the dashboard thumbnail must be offered before the generic file card falls through to it"
	);
	// the generic card itself no longer carries a canOpenDash branch of its own
	const group = chatSrc.slice(
		chatSrc.indexOf('<div v-else class="jv-artifact-group">'),
		chatSrc.indexOf("</template>", chatSrc.indexOf('<div v-else class="jv-artifact-group">'))
	);
	assert.doesNotMatch(group, /canOpenDash/);
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
	// pane's restore stays blocked for the life of the page. Since the
	// jarvis-goto hand-off (#884), that branch then forks: a FIRST-time goto
	// prefill starts a CLEAN builder (never the sticky editing restore),
	// everything else restores as before - but the hold release always comes
	// first. jarvis#912 adds a third fork ahead of it: a REPEAT hand-off
	// (gotoResume) resumes the recorded conversation instead of either.
	assert.match(
		pageSrc,
		/promotionPending\.value = false;\n\t\tif \(gotoResume\) \{[\s\S]*?resumeGotoHandoff\(gotoResume\.conv, gotoResume\.text, gotoMessageId\);\n\t\t\} else if \(gotoText\) \{[\s\S]*?clearBuilder\(\);\n\t\t\} else \{\n\t\t\tnormalMount\(\);\n\t\t\}/
	);
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

// ---- jarvis#912: a repeat ```jarvis-goto hand-off resumes, not rebuilds ----
//
// One dashboard build used to produce several builder conversations: the
// live auto-redirect (run:end, guarded by the fired stamp) built the first
// one correctly, but the "Continue in Dashboards" card has no per-click
// guard and stayed clickable on every later visit to the transcript. Each
// extra click ran DashboardsPage's onMounted -> clearBuilder() -> sendText(),
// which wiped the sticky builder conversation and posted the seeded prompt as
// a message with conversation="" - a brand-new agent session (~78k tokens)
// every time. The fix stashes which builder conversation the FIRST hand-off
// for a given message landed on (the fired stamp - see chatGoto.test.js for
// its shape) and has every later trigger for that SAME message resume it.

test("gotoDashboards passes the message id through to the fired-stamp lookup", () => {
	// see chatGoto.test.js for gotoDashboards' own body assertions - this only
	// pins that BOTH call sites (the live redirect, the card button) pass the
	// message id the stamp is keyed on, or the lookup has nothing to key off.
	assert.match(chatSrc, /gotoDashboards\(goto\.prompt, m\.name\);/);
	assert.match(chatSrc, /@click="gotoDashboards\(gotoOf\(m\)\.prompt, m\.name\)"/);
});

test("send() learns the conversation a goto hand-off landed on and stamps it", () => {
	assert.match(
		paneSrc,
		/import \{ gotoFiredKey, parseFiredStamp, encodeFiredStamp \} from "@\/lib\/chatGoto";/
	);
	const record = fnBody(paneSrc, "function recordGotoConversation(");
	assert.match(record, /if \(!messageId \|\| !conv\) return;/);
	assert.match(record, /const key = gotoFiredKey\(messageId\);/);
	// the fired-AT timestamp survives being upgraded to carry a conversation -
	// only a stamp this send() itself just wrote (Date.now() fallback) invents
	// a new one
	assert.match(record, /const stamp = parseFiredStamp\(localStorage\.getItem\(key\)\);/);
	assert.match(
		record,
		/localStorage\.setItem\(key, encodeFiredStamp\(stamp \? stamp\.t : Date\.now\(\), conv\)\);/
	);
	const send = fnBody(paneSrc, "async function send(");
	assert.match(send, /async function send\(gotoMessageId = ""\)/);
	// stamped with whatever conversation this send actually landed on - the
	// REPOINTED id when send_message() opened a fresh one, the sticky one
	// otherwise - not blindly the pre-send value
	assert.match(
		send,
		/recordGotoConversation\(gotoMessageId, r\.conversation_id \|\| conversation\.value\);/
	);
	// sendText forwards it, and the Send button/Enter-key paths (message-less
	// ordinary sends) pass none, which is a no-op inside recordGotoConversation
	assert.match(paneSrc, /function sendText\(text, gotoMessageId = ""\)/);
	assert.match(fnBody(paneSrc, "function sendText("), /send\(gotoMessageId\);/);
});

test("the page resolves a goto hand-off into resume/build/plain-restore, in that order", () => {
	// gotoResume only when the STAMP names a conversation - `resume` alone
	// (set by gotoDashboards whenever messageId was passed, even before a
	// conversation exists) is not enough on its own.
	assert.match(
		pageSrc,
		/const gotoResume =\n\t\tgotoClaimsCanvas && dashboardPrefill\.resume && dashboardPrefill\.conv\n\t\t\t\? \{ conv: dashboardPrefill\.conv, text: String\(dashboardPrefill\.text \|\| ""\)\.trim\(\) \}\n\t\t\t: null;/
	);
	// a FIRST-time hand-off (autoSend, no recorded conversation yet) still
	// takes the pre-#912 clearBuilder() + sendText() path
	assert.match(
		pageSrc,
		/const gotoText =\n\t\tgotoClaimsCanvas && !gotoResume && dashboardPrefill\.autoSend\n\t\t\t\? String\(dashboardPrefill\.text \|\| ""\)\.trim\(\)\n\t\t\t: "";/
	);
	const mount = fnBody(pageSrc, "onMounted(async () => {");
	assert.match(mount, /if \(gotoResume\) \{/);
	assert.match(mount, /resumeGotoHandoff\(gotoResume\.conv, gotoResume\.text, gotoMessageId\);/);
	// gotoResume is checked BEFORE gotoText, so a repeat trigger can never fall
	// through to a fresh clearBuilder() build
	assert.ok(
		mount.indexOf("if (gotoResume) {") < mount.indexOf("} else if (gotoText) {"),
		"resume must be checked ahead of the fresh-build branch"
	);
	// the seeded first message (a first-time hand-off only) also carries the
	// message id through, so ITS OWN send() records the stamp
	assert.match(mount, /chatPane\.value\.sendText\(gotoText, gotoMessageId\);/);
});

test("resumeGotoHandoff repoints the sticky conversation, verified against the server first", () => {
	const resume = fnBody(pageSrc, "async function resumeGotoHandoff(");
	assert.match(resume, /await getDashboardConversation\(conv\);/);
	// success (or a transient blip - not isMissingConversation): repoint, the
	// pane's own watch(conversation, ...) tears the old thread down and loads
	// the new one
	assert.match(resume, /chatConv\.value = conv;/);
	assert.match(resume, /dashDataMode\.value = "auto";/);
});

test("a recorded-but-deleted conversation falls back to a fresh build, and forgets the stale mapping", () => {
	const resume = fnBody(pageSrc, "async function resumeGotoHandoff(");
	assert.match(resume, /if \(isMissingConversation\(e\)\) \{/);
	// the stamp named a conversation that is gone - remove the whole stamp
	// (not just its conv half) so this degrades to a first-time hand-off,
	// exactly as if the message had never fired before
	assert.match(resume, /localStorage\.removeItem\(gotoFiredKey\(messageId\)\);/);
	assert.match(resume, /clearBuilder\(\);/);
	assert.match(resume, /chatPane\.value\.sendText\(text, messageId\);/);
	// the fallback branch returns before the repoint below runs - never both
	assert.ok(
		resume.indexOf("return;") < resume.lastIndexOf("chatConv.value = conv;"),
		"the deleted-conversation fallback must not also repoint onto the dead id"
	);
});

// jarvis#912 round 2, finding #1: resumeGotoHandoff used to reuse isGoneError
// (row-deletion semantics: 404 OR a permission error) to decide the
// conversation is gone. A conversation-specific 403 is not evidence of that -
// a transient auth hiccup, a scope change - so it must NOT take the
// fresh-build fallback above; isMissingConversation is narrower on purpose.
test("resumeGotoHandoff's own gone-check excludes permission errors, unlike isGoneError", () => {
	assert.match(
		fnBody(pageSrc, "function isMissingConversation("),
		/return !!\(e && \(e\.status === 404 \|\| e\.exc_type === "DoesNotExistError"\)\);/
	);
	assert.doesNotMatch(fnBody(pageSrc, "function isMissingConversation("), /isPermissionError/);
	// isGoneError itself is untouched - other callers (resumeAdoption, openSave,
	// promoteFromChat's adoption) still want the row-deletion semantics
	assert.match(fnBody(pageSrc, "function isGoneError("), /isPermissionError\(e\)/);
});

// jarvis#912 round 2, finding #2: resumeGotoHandoff repointed chatConv but
// never cleared editSeed - left set from setup (a sticky ?edit target, or
// none), onCanvas's restore guard (builderHtml || editSeed ||
// promotionPending) refused to put the resumed build's canvas up at all.
test("resumeGotoHandoff clears the fields that would block the resumed canvas from restoring", () => {
	const resume = fnBody(pageSrc, "async function resumeGotoHandoff(");
	assert.match(resume, /editSeed\.value = "";\n\tbuilderHtml\.value = "";/);
	// cleared before the repoint, and never via clearBuilder() (that would also
	// wipe chatConv/the conversation this function exists to resume)
	assert.ok(
		resume.indexOf('editSeed.value = "";') < resume.indexOf("chatConv.value = conv;"),
		"editSeed must be cleared before the repoint, or a restore racing it could still be blocked"
	);
	const onCanvasGuard = fnBody(pageSrc, "async function onCanvas(").match(
		/if \(restore && \(builderHtml\.value \|\| editSeed\.value \|\| promotionPending\.value\)\) return false;/
	);
	assert.ok(onCanvasGuard, "onCanvas's restore guard must still be exactly what this resets");
});

test("the promotion is validated against the transcript, not the link", () => {
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	assert.match(pageSrc, /import \{ builderCanvasFrame \} from "@\/lib\/dashboardRestore";/);
	assert.match(promote, /await getDashboardConversation\(conversation\)/);
	assert.match(promote, /frame = builderCanvasFrame\(d\.messages \|\| \[\], messageId\);/);
	// a message that is gone, or never drew html, says so and leaves the builder
	// as it was
	assert.match(promote, /if \(!frame\) \{\n\t\tgiveUp\(/);
	assert.match(promote, /giveUp\(errHtml\(e\)\);/);
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
		/editingSticky\.value = identity\.name;/,
		/adoptedRow\.value = identity\.name;/,
		/if \(!identity\.keepPrior\) \{\n\t\t\teditingDetail\.value = identity\.adopted;/,
		/savedName\.value = identity\.name;/,
		/editSeed\.value = "";/,
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
		/if \(dash\) \{\n\t\t\ttry \{\n\t\t\t\tdetail = await getDashboard\(dash\);/
	);
	// the identity it comes away with is decided by the pure function above, on
	// what the fetch answered and what the builder already had
	assert.match(accept, /const priorName = editingName\(\);/);
	assert.match(
		accept,
		/const identity = adoptionIdentity\(\{ dash, detail, priorName, gone \}\);/
	);
	// a name that fails the (permission-gated) fetch degrades to the identity-less
	// promotion — a URL-supplied `dash` is never a dead click, and never a trusted
	// one either
	assert.match(accept, /gone = isGoneError\(e\);\n\t\t\t\tdetail = null;/);
	// the adopted row supplies the IDENTITY only: its html must never reach the
	// canvas, or the promotion answers with the saved document instead of the
	// build the user clicked
	assert.doesNotMatch(accept, /builderHtml\.value = (identity|detail|adopted)\b/);
	assert.doesNotMatch(accept, /\.html\b/);
	// ...and the identity is what makes Save update in place — the dialog reads it
	// off editingDetail, while the AGENT is told nothing (agentEditingName)
	assert.match(pageSrc, /:editing-name="agentEditingName"/);
	assert.match(pageSrc, /:editing="editingDetail"/);
	// the row's theme rides along, so the first "Save changes" re-lints against
	// the theme the row actually has instead of the picker's default
	assert.match(accept, /if \(identity\.theme\) builderTheme\.value = identity\.theme;/);
	// the query it arrived on is dropped with the rest of the promotion keys
	assert.match(fnBody(pageSrc, "function stripPromotionQuery("), /delete q\.dash;/);
});

test("the two identities are bound separately, and the adoption ends where they meet", () => {
	// Save reads the full detail; the agent reads agentEditingName, which is empty
	// for as long as the canvas is ahead of the row.
	assert.match(pageSrc, /:editing-name="agentEditingName"/);
	assert.match(pageSrc, /:editing="editingDetail"/);
	assert.match(pageSrc, /const agentEditingName = computed\(\(\) =>/);
	assert.match(pageSrc, /editingName: \(editingDetail\.value \|\| \{\}\)\.name \|\| "",/);
	assert.match(pageSrc, /adoptionActive: !!adoptedRow\.value,/);
	// explicit state, not inferred from timestamps — and sticky, because the
	// remount has to be able to tell the two states apart (resumesAdoption)
	assert.match(
		pageSrc,
		/const adoptedRow = useStorage\(`jarvis-dash-adopted-\$\{session\.user \|\| "anon"\}`, ""\);/
	);
	// it ends exactly where the row and the canvas become one document again:
	// the first successful Save, an ?edit= load, or a builder that was cleared.
	// loadEdit's own copy moved into applyEditDetail (jarvis#884), which
	// onDashboardSaved (the pane's own build) also calls directly.
	assert.match(fnBody(pageSrc, "function onSaved("), /adoptedRow\.value = "";/);
	assert.match(fnBody(pageSrc, "function applyEditDetail("), /adoptedRow\.value = "";/);
	assert.match(fnBody(pageSrc, "function clearBuilder("), /adoptedRow\.value = "";/);
	// the send itself takes the pane's prop, so the suppression reaches the agent
	assert.match(paneSrc, /editingName: \{ type: String, default: "" \},/);
	assert.match(paneSrc, /props\.editingName,/);
	assert.match(apiSrc, /if \(editingName\) \{/);
});

test("the adoption resume restores the identity WITHOUT the row's html", () => {
	// loadEdit is the wrong restore here: it puts the STORED document on the
	// canvas and clears the canvas message, so the user comes back to an older
	// build than the one they left and the promoted one is only in the transcript.
	const resume = fnBody(pageSrc, "async function resumeAdoption(");
	assert.match(resume, /d = await getDashboard\(name\);/);
	assert.doesNotMatch(resume, /builderHtml\.value/);
	assert.doesNotMatch(resume, /canvasMsg\.value/);
	assert.match(resume, /if \(d && d\.name && d\.can_edit\) \{/);
	assert.match(resume, /editingDetail\.value = d;/);
	assert.match(resume, /builderTheme\.value = themeKey\(d\.theme\);/);
	assert.match(resume, /adoptedRow\.value = d\.name;/);
	// a deleted (or no-longer-ours) row is forgotten silently, as loadEdit's
	// remount path does; a blip keeps the identity for the next mount to retry
	assert.match(resume, /if \(isGoneError\(e\)\) \{/);
	assert.match(fnBody(pageSrc, "function isGoneError("), /isPermissionError\(e\)/);
	// the decision is taken at SETUP, like the edit seed — the pane mounts first
	assert.match(pageSrc, /const adoptionResume = resumesAdoption\(\{/);
	assert.match(pageSrc, /adoptedRow: adoptedRow\.value,/);
	// ...and the seed is left EMPTY on that path, or onCanvas drops the pane's
	// transcript restore and the canvas comes back blank
	assert.match(
		pageSrc,
		/const editSeed = ref\(routeEdit \|\| \(adoptionResume \? "" : editingSticky\.value\)\);/
	);
	// the mount takes it ahead of the ?edit= path, and so does the fallback a
	// declined/failed promotion runs
	assert.match(pageSrc, /if \(adoptionResume\) \{\n\t\t\tresumeAdoption\(adoptedRow\.value\);/);
	const mount = fnBody(pageSrc, "onMounted(async () => {");
	assert.ok(
		mount.indexOf("resumeAdoption(adoptedRow.value)") <
			mount.indexOf("loadEdit(editSeed.value"),
		"the adopted state is restored instead of the edit seed, not after it"
	);
	assert.match(mount, /promoteFromChat\(routeChat, routeCanvas, \{ fallback: normalMount/);
});

test("an identity discarded while the resume was in flight stays discarded", () => {
	// The window is real and wide: the caps probe ahead of this can sleep a full
	// second and retry before resumeAdoption even starts, and the pane's "New
	// chat" sits on screen throughout. It runs clearBuilder — which short-circuits
	// the confirm, because the canvas is still empty — so the user is looking at a
	// builder they believe is fresh when the fetch lands. Writing the identity
	// then attaches DASH-7 to whatever they build next, the dialog silently reads
	// "Save changes", and the save overwrites DASH-7's html, sources and title.
	const resume = fnBody(pageSrc, "async function resumeAdoption(");
	const bail = "if (adoptedRow.value !== name) return;";
	assert.equal(
		(resume.match(/if \(adoptedRow\.value !== name\) return;/g) || []).length,
		2,
		"both branches re-check after the await, not just one"
	);
	// the catch branch: the re-check comes before it forgets a gone row
	const failure = resume.slice(resume.indexOf("} catch (e) {"));
	assert.ok(
		failure.indexOf(bail) >= 0 &&
			failure.indexOf(bail) < failure.indexOf("if (isGoneError(e))"),
		"the catch branch bails before it writes"
	);
	// the success branch: before every write it makes
	const success = resume.slice(resume.lastIndexOf(bail));
	for (const write of [
		"editingDetail.value = d;",
		"editingSticky.value = d.name;",
		"savedName.value = d.name;",
		"builderTheme.value = themeKey(d.theme);",
		"adoptedRow.value = d.name;",
	]) {
		assert.ok(success.includes(write), `the success writes follow the re-check: ${write}`);
	}
	// ...including the "gone, or no longer editable" tail
	assert.ok(
		success.lastIndexOf('adoptedRow.value = "";') >
			success.indexOf("adoptedRow.value = d.name;"),
		"the not-editable tail is inside the re-checked region too"
	);
});

test("a kept identity repairs itself the moment the user asks to save", () => {
	// The blip branch above keeps a NAME. Nothing reads it: the dialog's title,
	// its "Save changes" label and the `name` on the payload all come off
	// `editingDetail`, which a fresh mount has none of — so the promise "the row
	// is kept" ends in the duplicate it was made to prevent. One repair fetch, at
	// the only moment it matters.
	const open = fnBody(pageSrc, "async function openSave(");
	assert.match(open, /const name = adoptedRow\.value;/);
	assert.match(open, /if \(name && !editingDetail\.value\) \{/);
	assert.match(open, /const d = await getDashboard\(name\);/);
	// success: the detail lands, so Save updates in place
	assert.match(
		open,
		/if \(d && d\.name && d\.can_edit\) \{\n\t\t\t\t\teditingDetail\.value = d;/
	);
	// gone, or no longer editable: drop the identity rather than offer a "Save
	// changes" that throws once the dialog has been filled in
	assert.match(open, /if \(adoptedRow\.value === name && isGoneError\(e\)\) \{/);
	assert.match(open, /adoptedRow\.value = "";/);
	// this await is a window like every other: New chat / ?edit= can land in it
	assert.match(open, /if \(adoptedRow\.value === name\) \{/);
	// ...and a discard confirm raised inside it owns the screen — the Save dialog
	// must not stack on top of it
	assert.match(open, /if \(!builderHtml\.value \|\| discardOpen\.value\) return;/);
	// the repaired row brings its THEME with it (a repair only runs on a fresh
	// mount, DEFAULT_THEME picker) — but never over a theme the user picked this
	// session, or "re-theme my dashboard" would be silently undone
	assert.match(
		open,
		/if \(builderTheme\.value === DEFAULT_THEME\)\n\t\t\t\t\t\tbuilderTheme\.value = themeKey\(d\.theme\);/
	);
	// one attempt, not one per click — and the button says it is working
	assert.match(open, /if \(!builderHtml\.value \|\| repairing\.value\) return;/);
	assert.match(open, /repairing\.value = true;/);
	assert.match(open, /\} finally \{\n\t\t\trepairing\.value = false;/);
	assert.match(pageSrc, /:loading="repairing"/);
	// and the comment that claimed the blip branch kept the promise on its own
	// now points at the mechanism that actually does
	assert.match(fnBody(pageSrc, "async function resumeAdoption("), /openSave\(\)/);
});

test("an adoption whose artifact is gone falls back to the row it adopted", () => {
	// accept() blanks the canvas when the promoted artifact cannot be fetched, but
	// keeps the identity — so the next mount restores "Editing X", replays the
	// same dead message, and latches: empty canvas, badge, Save disabled, every
	// time. The build is unrecoverable; the ROW still holds a document.
	const failed = fnBody(pageSrc, "function restoreFailed(");
	assert.match(failed, /failedRestores\.add\(message_id\);/);
	assert.match(failed, /if \(message_id !== canvasMsg\.value \|\| !adoptedRow\.value\) return;/);
	assert.match(failed, /editSeed\.value = name;/);
	assert.match(failed, /loadEdit\(name, \{ deepLink: false \}\);/);
	// the latch is set FIRST, so the pane's next transcript load cannot re-enter
	// this (onCanvas drops a latched message before it fetches anything)
	assert.ok(
		failed.indexOf("failedRestores.add(message_id);") < failed.indexOf("loadEdit(name,"),
		"the loop guard goes up before the fallback runs"
	);
	const onCanvas = fnBody(pageSrc, "async function onCanvas(");
	assert.equal(
		(onCanvas.match(/if \(restore\) restoreFailed\(message_id\);/g) || []).length,
		2,
		"both failure branches — no content, and a thrown fetch"
	);
	assert.match(onCanvas, /if \(restore && failedRestores\.has\(message_id\)\) return false;/);
	// loadEdit is what ends the adoption: the canvas becomes the row's own html,
	// so the two documents are one again. Its own state-setting moved into
	// applyEditDetail (jarvis#884), which it still reaches via confirmDiscard.
	const edit = fnBody(pageSrc, "function applyEditDetail(");
	assert.match(edit, /adoptedRow\.value = "";/);
	assert.match(edit, /canvasMsg\.value = "";/);
	// a live frame's failure is untouched — it toasts, and there is no stale
	// identity to fall back from
	assert.match(onCanvas, /else toast\.error\(errHtml\(e\)\);/);
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
	assert.match(
		pageSrc,
		/import \{\n\tadoptionIdentity,\n\tagentRevisionTarget,\n\tresumesAdoption,\n\twouldDiscardOnPromotion,\n\} from "@\/lib\/dashboardOpen";/
	);
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
