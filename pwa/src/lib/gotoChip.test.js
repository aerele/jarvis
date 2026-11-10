// The "Open in web chat" chip for a ```jarvis-goto block, on the phone.
//
// The block itself is stripped and parsed in lib/blocks.js (blocks.test.js). This pins
// that ChatView feeds the chip from that parser and reuses the diagram chip, which
// already links to the web chat. No component harness in this app (see
// macroClosedRender.test.js), so the wiring is asserted against the source.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatViewSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

// The <a class="jv-diagram-chip"> element: from its opening tag to its closing tag.
function chip() {
	const at = chatViewSrc.indexOf('class="jv-diagram-chip"');
	assert.notEqual(at, -1, "ChatView must still draw the web chat chip");
	const start = chatViewSrc.lastIndexOf("<a", at);
	return chatViewSrc.slice(start, chatViewSrc.indexOf("</a>", at));
}

test("PWA ChatView: the message view carries its chips from messageChips", () => {
	assert.match(chatViewSrc, /\bmessageChips,/, "ChatView must import messageChips");
	assert.match(chatViewSrc, /chips: messageChips\(content\)/);
});

test("PWA ChatView: one <a> per chip (diagram and goto apart), each opening the web chat", () => {
	const el = chip();
	assert.match(el, /v-for="chip in it\.view\.chips"/);
	assert.match(el, /:key="chip\.kind"/);
	assert.match(el, /:href="webChatUrl"/);
	assert.match(el, /Open in web chat/);
});

// A0 pinned "no chip for the calendar"; replaced on purpose (a lead-in needs a link).
test("PWA ChatView: the chip is titled by the chip model, not by a local helper", () => {
	assert.match(chip(), /\{\{ chip\.title \}\}/);
	assert.equal(chatViewSrc.indexOf("function chipTitle("), -1, "chipTitle is replaced");
});

test("PWA ChatView: each chip draws its own icon (diagram, chart, calendar)", () => {
	const el = chip();
	assert.match(el, /chip\.icon === 'diagram'/);
	assert.match(el, /chip\.icon === 'calendar'/);
	assert.match(el, /M18 20V10M12 20V4M6 20v-6/, "chart icon");
	assert.match(el, /M16 2v4M8 2v4M3 10h18/, "calendar icon");
	assert.match(el, /aria-hidden="true"/);
});

test("PWA ChatView: no goto JSON is rendered anywhere: the chip reads only the page key, never the block's text", () => {
	assert.doesNotMatch(chip(), /it\.view\.goto\b|\.prompt|jarvis-goto/);
});
