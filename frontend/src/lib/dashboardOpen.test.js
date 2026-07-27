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
// canOpenInDashboards()/dashboardOpenRoute() are the real logic; the rest is
// component wiring, fenced here by source assertions (the dashboardRestore
// precedent) because a .vue SFC cannot be imported into a plain node runner.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { canOpenInDashboards, dashboardOpenRoute } from "./dashboardOpen.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const read = (...p) => fs.readFileSync(path.join(HERE, ...p), "utf8");
const chatSrc = read("..", "views", "ChatView.vue");
const pageSrc = read("..", "pages", "dashboards", "DashboardsPage.vue");
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

test("only an html artifact of a DASHBOARDS conversation gets the button", () => {
	assert.equal(canOpenInDashboards("dashboards", { type: "html" }), true);
	// same conversation, other artifact types: untouched
	assert.equal(canOpenInDashboards("dashboards", { type: "image" }), false);
	assert.equal(canOpenInDashboards("dashboards", { type: "pdf" }), false);
	assert.equal(canOpenInDashboards("dashboards", { type: "svg" }), false);
	assert.equal(canOpenInDashboards("dashboards", { type: "file" }), false);
	// same artifact, other conversations: untouched. Triggers conversations are
	// stamped too, but this change ships the dashboards button only.
	assert.equal(canOpenInDashboards("triggers", { type: "html" }), false);
	assert.equal(canOpenInDashboards("", { type: "html" }), false);
	assert.equal(canOpenInDashboards(undefined, { type: "html" }), false);
});

test("a junk canvas item never throws", () => {
	assert.equal(canOpenInDashboards("dashboards", null), false);
	assert.equal(canOpenInDashboards("dashboards", undefined), false);
	assert.equal(canOpenInDashboards("dashboards", {}), false);
});

// ---- where it goes --------------------------------------------------------

test("a saved dashboard opens IN PLACE, so Save keeps updating that row", () => {
	assert.deepEqual(
		dashboardOpenRoute({
			dashboard: { name: "DASH-1", dashboard_title: "Sales" },
			conversation: "c1",
			messageId: "m1",
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

// ---- main chat: the affordance is conversation state, not html-sniffing ----

test("ChatView reads the conversation's origin_page and clears it on switch", () => {
	assert.match(
		chatSrc,
		/import \{ canOpenInDashboards, dashboardOpenRoute \} from "@\/lib\/dashboardOpen";/
	);
	assert.match(chatSrc, /const originPage = ref\(""\);/);
	const load = fnBody(chatSrc, "async function loadConversation(");
	assert.match(load, /originPage\.value = d\?\.conversation\?\.origin_page \|\| "";/);
	// the no-conversation arm must reset it, or the button survives onto a chat
	// that has no dashboards at all
	assert.match(load, /originPage\.value = "";/);
	assert.ok(
		load.indexOf('originPage.value = "";') <
			load.indexOf("originPage.value = d?.conversation?.origin_page"),
		"the reset belongs to the `if (!id)` arm, above the load"
	);
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

test("the open preview panel offers the same hand-off for the open artifact", () => {
	const head = chatSrc.slice(
		chatSrc.indexOf('<div class="jv-artifact-head">'),
		chatSrc.indexOf('<div class="jv-artifact-body">')
	);
	assert.notEqual(head, "", "the artifact preview header must still exist");
	assert.match(head, /v-if="canOpenDash\(artifact\.cv\)"/);
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
	assert.match(
		open,
		/router\.push\(dashboardOpenRoute\(\{ dashboard, conversation: conv, messageId: m\.name \}\)\);/
	);
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
	const watcher = pageSrc.slice(
		pageSrc.indexOf("watch(\n\t() => [route.query.chat, route.query.canvas],")
	);
	assert.notEqual(watcher, "", "?chat=&canvas= must be watched, not read once at setup");
	assert.match(watcher.slice(0, watcher.indexOf("\n);")), /promoteFromChat\(conv, msg\);/);
	// and the mount path runs it too
	assert.match(
		pageSrc,
		/promoteFromChat\(routeChat, routeCanvas, \{ fallback: normalMount \}\)/
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

test("accepting clears the editing identity before repointing the builder", () => {
	// Without this, Save writes the promoted canvas back over whatever dashboard
	// the builder happened to be editing.
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	const accept = promote.slice(promote.indexOf("const accept = async () => {"));
	for (const line of [
		/editingSticky\.value = "";/,
		/editingDetail\.value = null;/,
		/editSeed\.value = "";/,
		/savedName\.value = "";/,
		/chatConv\.value = conversation;/,
		/canvasMsg\.value = messageId;/,
	]) {
		assert.match(accept, line);
	}
	// the canvas is rebuilt through the normal frame path — that is what re-runs
	// the html with the query tools, i.e. "shown with data"
	assert.match(
		accept,
		/const rendered = await onCanvas\(\{ message_id: frame\.message_id, items: frame\.items \}\);/
	);
	// ...and an artifact whose File has since gone must not leave the PREVIOUS
	// document on the canvas with Save armed over the new thread
	assert.match(accept, /if \(!rendered\) builderHtml\.value = "";/);
	const onCanvas = fnBody(pageSrc, "async function onCanvas(");
	assert.match(onCanvas, /canvasMsg\.value = message_id;\n\t\treturn true;/);
	// every other exit says "nothing was rendered"
	assert.doesNotMatch(onCanvas, /(?<!return false;\n\t)\breturn;/);
	// clearing builderHtml BEFORE the fetch would unblock the pane's own
	// transcript restore and let an older frame race this one onto the canvas —
	// the only clear allowed is the post-fetch one above
	assert.ok(
		accept.indexOf("await onCanvas(") < accept.indexOf('builderHtml.value = "";'),
		"builderHtml is only cleared after the artifact failed to arrive"
	);
	assert.equal((accept.match(/builderHtml\.value = "";/g) || []).length, 1);
});

test("a promotion that would cost the user something confirms first", () => {
	const guard = fnBody(pageSrc, "function promotionWouldDiscard(");
	assert.match(guard, /if \(unsavedCanvas\.value\) return true;/);
	assert.match(
		guard,
		/if \(editingSticky\.value \|\| editingDetail\.value \|\| editSeed\.value\) return true;/
	);
	assert.match(
		guard,
		/return !!\(chatConv\.value && chatConv\.value !== conv && canvasMsg\.value\);/
	);
	const promote = fnBody(pageSrc, "async function promoteFromChat(");
	// `force` is required: confirmDiscard short-circuits on !unsavedCanvas, so an
	// editing target alone would never reach the dialog
	assert.match(
		promote,
		/confirmDiscard\(accept, \(\) => giveUp\(""\), \{ force: true, copy: PROMOTE_COPY \}\);/
	);
	assert.match(
		promote,
		/if \(!promotionWouldDiscard\(conversation\)\) \{\n\t\tawait accept\(\);/
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
});
