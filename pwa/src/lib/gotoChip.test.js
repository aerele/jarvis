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

test("PWA ChatView: the message view carries the goto page from gotoChipPage", () => {
	assert.match(chatViewSrc, /\bgotoChipPage,/, "ChatView must import gotoChipPage");
	assert.match(chatViewSrc, /gotoPage: gotoChipPage\(content\)/);
});

test("PWA ChatView: the chip shows for a diagram or a goto of either page, and opens the web chat", () => {
	const el = chip();
	assert.match(el, /v-if="it\.view\.diagrams \|\| it\.view\.gotoPage"/);
	assert.match(el, /:href="webChatUrl"/);
});

// A0 pinned "no chip for the calendar"; replaced on purpose (a lead-in needs a link).
test("PWA ChatView: the chip is titled by the page the goto names", () => {
	const at = chatViewSrc.indexOf("function chipTitle(v)");
	assert.notEqual(at, -1);
	const fn = chatViewSrc.slice(at, chatViewSrc.indexOf("\n}", at));
	assert.match(fn, /return v\.gotoPage === "payment-calendar" \? "Payment Calendar" : "Dashboards";/);
	assert.match(fn, /return "Diagram";/);
});

test("PWA ChatView: no goto JSON is rendered anywhere: the chip reads only the page key, never the block's text", () => {
	assert.doesNotMatch(chip(), /it\.view\.goto\b|\.prompt|jarvis-goto/);
});
