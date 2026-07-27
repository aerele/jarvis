// The three owner-reported Dashboards builder defects, fenced by a real
// executable test. Plain node built-ins (node:test + node:assert), like
// eventFence.test.js. Run directly (`node --test dashboardRestore.test.js`) or
// via the python suite (jarvis/tests/test_dashboard_builder_ux_client.py
// subprocess-runs it every CI run).
//
// D1 the composer kept the text it had just sent. D3 the canvas vanished on a
// tab switch or a navigation, with no way back and no warning that it was gone.
// lastCanvasFrame() is the real logic behind the rehydration; the rest of both
// fixes is component wiring, fenced here by source assertions (the
// voiceDictationLifecycle precedent) because a .vue SFC cannot be imported into
// a plain node runner.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { lastCanvasFrame } from "./dashboardRestore.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const read = (...p) => fs.readFileSync(path.join(HERE, ...p), "utf8");
const paneSrc = read("..", "pages", "dashboards", "DashboardChatPane.vue");
const pageSrc = read("..", "pages", "dashboards", "DashboardsPage.vue");
const triggerPaneSrc = read("..", "pages", "triggers", "TriggerChatPane.vue");

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

// The body of a named function declaration, up to the next top-level `function`.
const fnBody = (src, decl) => {
	const start = src.indexOf(decl);
	assert.notEqual(start, -1, `source must still define ${decl}`);
	const end = src.indexOf("\nfunction ", start + decl.length);
	const alt = src.indexOf("\nasync function ", start + decl.length);
	const stop = Math.min(end === -1 ? src.length : end, alt === -1 ? src.length : alt);
	return src.slice(start, stop);
};

// ---- D3: the canvas is rebuildable from the transcript alone -------------

const html = (name) => ({ name: "documents/x/index.html", title: "T", type: "html" });

test("the NEWEST message with an html artifact is the frame to restore", () => {
	const frame = lastCanvasFrame([
		{ name: "m1", role: "assistant", canvas: [html()] },
		{ name: "m2", role: "user" },
		{ name: "m3", role: "assistant", canvas: [html()] },
	]);
	assert.deepEqual(frame, { message_id: "m3", items: [html()] });
});

test("messages with no canvas, or only non-html artifacts, are skipped", () => {
	assert.equal(lastCanvasFrame([{ name: "m1", role: "user" }]), null);
	assert.equal(lastCanvasFrame([{ name: "m1", canvas: [] }]), null);
	assert.equal(
		lastCanvasFrame([{ name: "m1", canvas: [{ name: "a.png", type: "image" }] }]),
		null
	);
	// ...but an html item ALONGSIDE an image still restores.
	const mixed = [{ name: "a.png", type: "image" }, html()];
	assert.deepEqual(lastCanvasFrame([{ name: "m1", canvas: mixed }]), {
		message_id: "m1",
		items: mixed,
	});
});

test("an image-only newest turn falls back to the last html turn", () => {
	const frame = lastCanvasFrame([
		{ name: "m1", canvas: [html()] },
		{ name: "m2", canvas: [{ name: "a.png", type: "image" }] },
	]);
	assert.equal(frame.message_id, "m1");
});

test("junk transcripts never throw (canvas is whatever the server parsed)", () => {
	assert.equal(lastCanvasFrame(null), null);
	assert.equal(lastCanvasFrame(undefined), null);
	assert.equal(lastCanvasFrame([]), null);
	assert.equal(lastCanvasFrame([null, undefined]), null);
	// canvas arrives as a JSON string only if parsing failed server-side
	assert.equal(lastCanvasFrame([{ name: "m1", canvas: "[]" }]), null);
	// a row with no name cannot be replayed through get_canvas(message, …)
	assert.equal(lastCanvasFrame([{ canvas: [html()] }]), null);
});

test("the pane replays the transcript's newest canvas on every load", () => {
	assert.match(paneSrc, /import \{ lastCanvasFrame \} from "@\/lib\/dashboardRestore"/);
	const load = fnBody(paneSrc, "async function loadTranscript(");
	assert.match(load, /lastCanvasFrame\(messages\.value\)/);
	assert.match(load, /emit\("canvas", \{ \.\.\.frame, restore: true \}\)/);
});

test("a restore never overwrites a live canvas or an explicit ?edit target", () => {
	const onCanvas = fnBody(pageSrc, "async function onCanvas(");
	assert.match(
		onCanvas,
		/if \(restore && \(builderHtml\.value \|\| editSeed\.value\)\) return;/
	);
	// re-checked AFTER the get_canvas round trip, not just before it
	assert.match(
		onCanvas,
		/if \(content && !\(restore && \(builderHtml\.value \|\| editSeed\.value\)\)\)/
	);
	// a failed replay is silent; only a live frame's failure is surfaced
	assert.match(onCanvas, /if \(!restore\) toast\.error/);
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

// ---- D3: discard is explicit -------------------------------------------

test("every path that drops an unsaved canvas confirms first", () => {
	assert.match(pageSrc, /confirmDialog/);
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

test("New chat asks the page instead of clearing itself behind a confirm", () => {
	// The pane owns no canvas, so it must not act before the page has asked.
	assert.match(fnBody(paneSrc, "function newChat("), /emit\("reset"\)/);
	assert.doesNotMatch(fnBody(paneSrc, "function newChat("), /conversation\.value = ""/);
	assert.match(paneSrc, /defineExpose\(\{ resetChat, sendText \}\)/);
	assert.match(fnBody(pageSrc, "function clearBuilder("), /chatPane\.value\.resetChat\(\)/);
});

test("the pane follows the sticky conversation slot when the page repoints it", () => {
	assert.match(paneSrc, /watch\(conversation, \(id, prev\) => \{/);
	// "" -> id is the pane's own first send; only a genuine repoint reloads
	assert.match(paneSrc, /if \(!prev \|\| id === prev\) return;/);
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
