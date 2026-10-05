// A macro run's closing message, on the phone.
//
// When a run ends with something to say (it failed, it stopped at a card), the
// bench posts that as the last message of the run's conversation and publishes
// { kind: "macro:closed", conversation_id, message_id } to the owner's socket
// (macros._post_closing_message). The desktop chat re-reads the conversation on
// that event. The PWA had no macro handling at all, so with the chat open on the
// phone the reason only showed up after a reload.
//
// No component harness in this app (see pumpFence.test.js's note on why), so the
// wiring is asserted against the source, as importFinishedRender.test.js does.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatViewSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

function onEventBody() {
	const start = chatViewSrc.indexOf("function onEvent(p) {");
	assert.notEqual(start, -1, "ChatView must still define onEvent(p)");
	const end = chatViewSrc.indexOf("\nfunction ", start + 1);
	assert.notEqual(end, -1, "could not find the end of onEvent");
	return chatViewSrc.slice(start, end);
}

// From `case "macro:closed":` to the `break;` that ends it. The case may share
// its body with a neighbour (a fall-through), so it is cut at the break, not at
// the next case label.
function macroClosedCase() {
	const body = onEventBody();
	const at = body.indexOf('case "macro:closed":');
	assert.notEqual(at, -1, "onEvent's switch must have a macro:closed case");
	const rest = body.slice(at);
	const end = rest.indexOf("break;");
	assert.notEqual(end, -1, "the macro:closed case must end in a break");
	return rest.slice(0, end);
}

test("PWA ChatView: onEvent's switch has a macro:closed case", () => {
	assert.match(onEventBody(), /case "macro:closed":/);
});

test("PWA ChatView: macro:closed re-reads the conversation with load()", () => {
	const caseBody = macroClosedCase();
	assert.match(caseBody, /\bload\(\);/);
	assert.doesNotMatch(caseBody, /loadConversation/, "the PWA has no loadConversation helper");
});

test("PWA ChatView: the case sits behind the open-conversation guard", () => {
	const body = onEventBody();
	const guardAt = body.indexOf("if (conv !== convId.value) return;");
	const switchAt = body.indexOf("switch (p.kind) {");
	const caseAt = body.indexOf('case "macro:closed":');
	assert.notEqual(guardAt, -1);
	assert.ok(guardAt < switchAt, "the conversation guard must precede the switch");
	assert.ok(switchAt < caseAt, "the case lives inside the switch, behind the guard");
});
