// The owner-reported Dashboards builder defects, fenced by a real executable
// test. Plain node built-ins (node:test + node:assert), like eventFence.test.js.
// Run directly (`node --test dashboardRestore.test.js`) or via the python suite
// (jarvis/tests/test_dashboard_builder_ux_client.py subprocess-runs it every CI
// run).
//
// D1 the composer kept the text it had just sent. D3 the canvas vanished on a
// tab switch or a navigation, with no way back and no warning that it was gone
// — and, once it came back, came back as a canvas the builder was no longer
// bound to (Save wrote a duplicate) or as a frame that had rendered at 0x0.
// builderCanvasFrame() is the real logic behind the rehydration; the rest of the
// fixes is component wiring, fenced here by source assertions (the
// voiceDictationLifecycle precedent) because a .vue SFC cannot be imported into
// a plain node runner.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { builderCanvasFrame } from "./dashboardRestore.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const read = (...p) => fs.readFileSync(path.join(HERE, ...p), "utf8");
const paneSrc = read("..", "pages", "dashboards", "DashboardChatPane.vue");
const pageSrc = read("..", "pages", "dashboards", "DashboardsPage.vue");
const canvasSrc = read("..", "pages", "dashboards", "DashboardCanvas.vue");
const saveDialogSrc = read("..", "pages", "dashboards", "SaveDashboardDialog.vue");
const triggerPaneSrc = read("..", "pages", "triggers", "TriggerChatPane.vue");
const composerSrc = read("..", "components", "chat", "Composer.vue");

// The composer block of an SFC: from the composer comment to the end of the
// template. Assertions about `disabled` must not accidentally read the pending
// approval card's Dismiss button, which is legitimately gated.
const composerOf = (src) => {
	const start = src.indexOf("<!-- composer:");
	assert.notEqual(start, -1, "the pane must still have its composer block");
	const end = src.indexOf("</template>", start);
	assert.notEqual(end, -1);
	return src.slice(start, end);
};

// The body of a named function declaration: everything up to its closing brace,
// which in these tab-indented sources is the first `}` in column 0. Tight on
// purpose — a loose slice would let an assertion pass on code that lives in the
// NEXT declaration.
const fnBody = (src, decl) => {
	const start = src.indexOf(decl);
	assert.notEqual(start, -1, `source must still define ${decl}`);
	const end = src.indexOf("\n}", start + decl.length);
	assert.notEqual(end, -1, `${decl} must close at the top level`);
	return src.slice(start, end);
};

// ---- D3: the canvas is rebuildable from the transcript alone -------------

const html = () => ({ name: "documents/x/index.html", title: "T", type: "html" });

test("the frame restored is the one the BUILDER recorded, not the newest html", () => {
	const rows = [
		{ name: "m1", role: "assistant", canvas: [html()] },
		{ name: "m2", role: "user" },
		// main chat, sharing the thread via "Open in chat", drew this one
		{ name: "m3", role: "assistant", canvas: [html()] },
	];
	assert.deepEqual(builderCanvasFrame(rows, "m1"), { message_id: "m1", items: [html()] });
	assert.deepEqual(builderCanvasFrame(rows, "m3"), { message_id: "m3", items: [html()] });
});

test("no recorded message means no restore at all", () => {
	const rows = [{ name: "m1", role: "assistant", canvas: [html()] }];
	assert.equal(builderCanvasFrame(rows, ""), null);
	assert.equal(builderCanvasFrame(rows, null), null);
	assert.equal(builderCanvasFrame(rows, undefined), null);
});

test("a recorded message that is gone (or drew no html) restores nothing", () => {
	assert.equal(builderCanvasFrame([{ name: "m1", canvas: [html()] }], "m9"), null);
	assert.equal(builderCanvasFrame([{ name: "m1", role: "user" }], "m1"), null);
	assert.equal(builderCanvasFrame([{ name: "m1", canvas: [] }], "m1"), null);
	assert.equal(
		builderCanvasFrame([{ name: "m1", canvas: [{ name: "a.png", type: "image" }] }], "m1"),
		null
	);
	// ...but an html item ALONGSIDE an image still restores.
	const mixed = [{ name: "a.png", type: "image" }, html()];
	assert.deepEqual(builderCanvasFrame([{ name: "m1", canvas: mixed }], "m1"), {
		message_id: "m1",
		items: mixed,
	});
});

test("junk transcripts never throw (canvas is whatever the server parsed)", () => {
	assert.equal(builderCanvasFrame(null, "m1"), null);
	assert.equal(builderCanvasFrame(undefined, "m1"), null);
	assert.equal(builderCanvasFrame([], "m1"), null);
	assert.equal(builderCanvasFrame([null, undefined], "m1"), null);
	// canvas arrives as a JSON string only if parsing failed server-side
	assert.equal(builderCanvasFrame([{ name: "m1", canvas: "[]" }], "m1"), null);
});

test("the pane replays the recorded canvas message on every transcript load", () => {
	assert.match(paneSrc, /import \{ builderCanvasFrame \} from "@\/lib\/dashboardRestore"/);
	assert.match(paneSrc, /const canvasMsg = useStorage\(\s*`jarvis-dash-canvasmsg-\$\{/);
	const load = fnBody(paneSrc, "async function loadTranscript(");
	assert.match(load, /builderCanvasFrame\(messages\.value, canvasMsg\.value\)/);
	assert.match(load, /emit\("canvas", \{ \.\.\.frame, restore: true \}\)/);
});

test("the page records WHICH message is on the canvas, under the same key", () => {
	assert.match(pageSrc, /const canvasMsg = useStorage\(`jarvis-dash-canvasmsg-\$\{/);
	const onCanvas = fnBody(pageSrc, "async function onCanvas(");
	// stamped only after the artifact actually reached the canvas
	assert.ok(
		onCanvas.indexOf("builderHtml.value = content;") <
			onCanvas.indexOf("canvasMsg.value = message_id;"),
		"the id is recorded for the html that was rendered"
	);
	// and dropped whenever the canvas stops being a chat artifact
	assert.match(fnBody(pageSrc, "function clearBuilder("), /canvasMsg\.value = "";/);
	assert.match(fnBody(pageSrc, "async function loadEdit("), /canvasMsg\.value = "";/);
});

test("a restore never overwrites a live canvas, an ?edit target or a promotion", () => {
	const onCanvas = fnBody(pageSrc, "async function onCanvas(");
	const guard =
		/if \(restore && \(builderHtml\.value \|\| editSeed\.value \|\| promotionPending\.value\)\)\s*\n?\s*return false;/;
	assert.match(onCanvas, guard);
	// re-checked AFTER the get_canvas round trip, not just before it
	assert.equal((onCanvas.match(new RegExp(guard.source, "g")) || []).length, 2);
	// a failed replay is silent; only a live frame's failure is surfaced
	assert.match(onCanvas, /else toast\.error\(errMsg\(e\)\);/);
});

test("an unreadable artifact is retried never, not on every transcript load", () => {
	// loadTranscript emits a restore frame on EVERY load, including the debounced
	// refetch behind each realtime frame — an unlatched failure is an unbounded
	// run of silent get_canvas calls.
	assert.match(pageSrc, /const failedRestores = new Set\(\);/);
	const onCanvas = fnBody(pageSrc, "async function onCanvas(");
	assert.match(onCanvas, /if \(restore && failedRestores\.has\(message_id\)\) return false;/);
	assert.match(onCanvas, /if \(restore\) failedRestores\.add\(message_id\);/);
});

// ---- D3: switching tabs must not tear the build pipeline down -----------

test("the builder tab is v-show, so the chat pane keeps its socket listener", () => {
	assert.match(pageSrc, /v-show="activeTab === 'builder'"/);
	assert.doesNotMatch(pageSrc, /v-if="activeTab === 'builder'"\s*\n\s*ref="builderEl"/);
	// the Saved tab is no longer a v-else of the builder (that chain is what
	// unmounted the pane)
	assert.match(pageSrc, /<SavedDashboardsTab v-if="activeTab === 'saved'"/);
	assert.doesNotMatch(pageSrc, /<SavedDashboardsTab v-else/);
});

test("coming back to the builder re-drives the canvas that rendered hidden", () => {
	// v-show means the iframe loads at 0x0 behind display:none; ECharts sized
	// against a zero-width container draws nothing and never self-heals.
	assert.match(canvasSrc, /defineExpose\(\{ exportAs, rebuild \}\)/);
	assert.match(pageSrc, /<DashboardCanvas\n\t+ref="canvasRef"/);
	const watcher = pageSrc.slice(pageSrc.indexOf("watch(\n\tactiveTab,"));
	assert.notEqual(watcher, "", "the page must watch the active tab");
	assert.match(watcher, /if \(v !== "builder" \|\| !builderHtml\.value\) return;/);
	assert.match(watcher, /canvasRef\.value\.rebuild\(\)/);
	// pre-flush would run while the wrapper is still display:none
	assert.match(watcher.slice(0, watcher.indexOf("\n);")), /flush: "post"/);
});

// ---- D3: the editing identity survives the round trip -------------------

test("a remount restores WHAT WAS BEING EDITED, not just the pixels", () => {
	// Without this the canvas comes back but `editingDetail` does not, so
	// SaveDashboardDialog sends no `name` and save_dashboard INSERTS a second
	// "Jarvis Dashboard" pointing at the same conversation.
	assert.match(saveDialogSrc, /if \(e && e\.name\) payload\.name = e\.name;/);
	assert.match(pageSrc, /:editing="editingDetail"/);
	assert.match(pageSrc, /const editingSticky = useStorage\(`jarvis-dash-editing-\$\{/);
	// seeded at setup, BEFORE the chat pane's transcript replay can reach the
	// canvas. The one exception is a builder that went away mid-ADOPTION, where
	// the canvas WAS the transcript's build and the stored row is behind it
	// (dashboardOpen.test.js owns that path) — the identity still comes back.
	assert.match(
		pageSrc,
		/const editSeed = ref\(routeEdit \|\| \(adoptionResume \? "" : editingSticky\.value\)\);/
	);
	assert.match(
		pageSrc,
		/if \(editSeed\.value\) loadEdit\(editSeed\.value, \{ deepLink: !!routeEdit \}\);/
	);
	// written by both paths that make this page "the editor of X"...
	assert.match(fnBody(pageSrc, "async function loadEdit("), /editingSticky\.value = d\.name;/);
	assert.match(fnBody(pageSrc, "function onSaved("), /editingSticky\.value = detail\.name;/);
	// ...and dropped by every path that stops editing it
	assert.match(fnBody(pageSrc, "function clearBuilder("), /editingSticky\.value = "";/);
});

// ---- D3: discard is explicit -------------------------------------------

test("every path that drops an unsaved canvas confirms first", () => {
	assert.match(pageSrc, /Discard this unsaved dashboard\?/);
	assert.match(pageSrc, /Its chat stays in your conversations\./);
	// unsaved == on the canvas and not (still) the saved document's html
	assert.match(
		pageSrc,
		/const unsavedCanvas = computed\(\s*\(\) =>\s*!!builderHtml\.value && builderHtml\.value !== /
	);
	for (const fn of [
		"function newDashboard(",
		"function resetBuilder(",
		"async function loadEdit(",
	]) {
		assert.match(fnBody(pageSrc, fn), /confirmDiscard\(/, `${fn} must confirm the discard`);
	}
});

test("the discard confirm is REACHABLE from loadEdit, not dead code", () => {
	// It only ever fires when an unsaved canvas is already on the builder, and a
	// fresh mount has none — so without a live watcher on ?edit= the confirm
	// inside loadEdit can never run and opening another dashboard silently
	// repoints the single sticky slot.
	const watcher = pageSrc.slice(pageSrc.indexOf("watch(\n\t() => route.query.edit,"));
	assert.notEqual(watcher, "", "?edit= must be watched, not read once at setup");
	assert.match(watcher.slice(0, watcher.indexOf("\n);")), /loadEdit\(name\);/);
	// and the seed must outlive the dialog: clearing it up front (the old
	// `finally`) lets a transcript restore land while the confirm is still up
	const load = fnBody(pageSrc, "async function loadEdit(");
	assert.doesNotMatch(load, /finally \{/);
	assert.match(load, /confirmDiscard\(\(\) => \{[\s\S]*?settle\(\);\n\t\t\}, settle\);/);
});

test("the discard confirm offers the surviving chat instead of just naming it", () => {
	assert.match(
		pageSrc,
		/label: "Discard", variant: "solid", onClick: \(\) => settleDiscard\(true\)/
	);
	const actions = pageSrc.slice(pageSrc.indexOf("const discardActions = computed("));
	assert.match(actions, /label: "Open its chat"/);
	assert.match(actions, /router\.push\("\/c\/" \+ id\)/);
	// ...only when there IS one, and only when going there is actually an escape
	// (a caller already acting on that same conversation drops it — see
	// dashboardOpen.test.js for the promotion that would otherwise loop)
	assert.match(actions, /if \(chatConv\.value && discardOfferChat\.value\) \{/);
});

test("New chat asks the page instead of clearing itself behind a confirm", () => {
	// The pane owns no canvas, so it must not act before the page has asked.
	assert.match(fnBody(paneSrc, "function newChat("), /emit\("reset"\)/);
	assert.doesNotMatch(fnBody(paneSrc, "function newChat("), /conversation\.value = ""/);
	assert.match(paneSrc, /defineExpose\(\{ resetChat, sendText, restoreCanvas \}\)/);
	assert.match(fnBody(pageSrc, "function clearBuilder("), /chatPane\.value\.resetChat\(\)/);
});

test("the pane follows the sticky conversation slot when the page repoints it", () => {
	assert.match(paneSrc, /watch\(conversation, \(id, prev\) => \{/);
	// "" -> id is the pane's own first send; only a genuine repoint reloads
	assert.match(paneSrc, /if \(!prev \|\| id === prev\) return;/);
	// ...and a repoint caused by our OWN in-flight send must not cancel the run
	// it just started (clearThread would drop the Thinking indicator)
	assert.match(fnBody(paneSrc, "async function send("), /ownRepoint = true;/);
	assert.match(paneSrc, /clearThread\(\{ keepRun: own \}\);/);
	assert.match(
		fnBody(paneSrc, "function clearThread("),
		/if \(!keepRun\) runActive\.value = false;/
	);
});

// ---- D2: the clarifying questions are answerable on every surface -------

test("both embedded panes render the ask card instead of deleting the block", () => {
	for (const [label, src] of [
		["dashboards", paneSrc],
		["triggers", triggerPaneSrc],
	]) {
		assert.match(src, /import AskCard from "@\/components\/chat\/AskCard\.vue";/, label);
		assert.match(src, /import \{ parseAsk \} from "@\/lib\/chatAsk";/, label);
		assert.match(src, /<AskCard\n\t+v-if="askFor === m\.name && activeAsk"/, label);
		assert.match(src, /:key="m\.name"/, label);
		assert.match(src, /@submit="sendText"/, label);
		// the jv-* tokens AskCard styles with are not global on a frappe-ui page
		assert.match(src, /:style="paletteVars"/, label);
	}
});

test("submitting the ask keeps the picks until the answer is actually posted", () => {
	// The host may refuse the text (a send in flight, a dictation transcribing).
	// Clearing before the emit lost the answers with no way to re-submit.
	const askCardSrc = read("..", "components", "chat", "AskCard.vue");
	const submit = fnBody(askCardSrc, "function submit()");
	assert.match(
		submit,
		/emit\("submit", askAnswerText\(props\.spec, sel\.value, other\.value\)\);/
	);
	assert.doesNotMatch(submit, /sel\.value = \{\};/);
	assert.doesNotMatch(submit, /other\.value = \{\};/);
});

// ---- D1: the composer clears and stays cleared --------------------------

for (const [label, src] of [
	["dashboards", paneSrc],
	["triggers", triggerPaneSrc],
]) {
	test(`${label} composer: a raw v-model textarea, never a FormControl`, () => {
		const composer = composerOf(src);
		assert.match(composer, /<textarea/);
		assert.match(composer, /v-model="draft"/);
		// FormControl type="textarea" is frappe-ui's Textarea, which emits its
		// update on `change` as well as `input` - the re-emit that restored the
		// draft. Nothing in the pane may reintroduce it.
		assert.doesNotMatch(src, /FormControl/);
		// One-way :modelValue + a manual @update handler also breaks IME
		// composition (Composer.vue documents why v-model is required).
		assert.doesNotMatch(composer, /:modelValue="draft"/);
		assert.doesNotMatch(composer, /@update:modelValue/);
	});

	test(`${label} composer: the textarea is never disabled by \`sending\``, () => {
		const composer = composerOf(src);
		const disabledBindings = composer.match(/:disabled="[^"]*"/g) || [];
		const textareaStart = composer.indexOf("<textarea");
		const textareaEnd = composer.indexOf("/>", textareaStart);
		const textarea = composer.slice(textareaStart, textareaEnd);
		assert.doesNotMatch(
			textarea,
			/sending/,
			"disabling a focused, dirty textarea mid-send is exactly the bug"
		);
		// but the SEND control still is gated, or a double-send is possible
		assert.ok(
			disabledBindings.some((b) => b.includes("sending")),
			"the send button must still be gated on `sending`"
		);
	});

	test(`${label} composer: autoGrow measures the textarea itself, post-flush`, () => {
		// It used to querySelector a textarea out of a wrapper div; with a real
		// ref there is no wrapper. And a programmatic clear only reaches the DOM
		// on the next flush, so the watcher is what shrinks the box after a send.
		assert.match(src, /watch\(draft, autoGrow, \{ flush: "post" \}\)/);
		assert.doesNotMatch(src, /box\.value\.querySelector\("textarea"\)/);
		assert.doesNotMatch(src, /nextTick\(autoGrow\)/);
	});
}

test("every Enter-to-send handler ignores an IME composition commit", () => {
	// Confirming a Japanese/Chinese/Korean candidate with Enter must not send the
	// half-converted reading. keyCode 229 covers the browsers with no isComposing.
	for (const [label, src, decl] of [
		["dashboards", paneSrc, "function onKeydown(e) {"],
		["triggers", triggerPaneSrc, "function onKeydown(e) {"],
		["composer", composerSrc, "function onKeydownInternal(e) {"],
	]) {
		const body = fnBody(src, decl);
		assert.match(body, /if \(e\.isComposing \|\| e\.keyCode === 229\) return;/, label);
		assert.ok(
			body.indexOf("e.isComposing") < body.indexOf('e.key === "Enter"'),
			`${label}: the guard must precede the Enter branch`
		);
	}
});

test("send() clears the draft up front and restores it only on rejection", () => {
	const send = fnBody(paneSrc, "async function send(");
	// guard, clear, optimistic bubble - in that order
	assert.ok(
		send.indexOf("if (!text || sending.value) return;") < send.indexOf('draft.value = "";'),
		"the in-flight guard must precede the clear"
	);
	assert.match(send, /const text = draft\.value\.trim\(\);/);
	assert.match(send, /draft\.value = "";/);
	// a server reject (single-flight guard / usage cap) puts the text back, and
	// only when the user has not started typing something else
	const restores = send.match(/if \(!draft\.value\) draft\.value = text;/g) || [];
	assert.equal(restores.length, 2, "restore on BOTH the ok:false and the throw path");
	assert.match(
		send,
		/messages\.value = messages\.value\.filter\(\(m\) => m\.name !== tmpName\)/
	);
});
