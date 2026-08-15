// Slice B ("auto-tell import completion") — the PWA half.
//
// When a background Data Import finishes, the bench posts a "✓ ..." message
// into the originating conversation and publishes
// { kind: "import:finished", conversation_id, message_id } to the owner's
// socket. Three files split the work (design FE4):
//   - views/ChatView.vue's local onEvent: live-append via load() when that
//     conversation is the one open on screen.
//   - App.vue's shell-level onEvent: the off-screen unread dot
//     (store.markUnread), with no push notify().
//   - lib/notifications.js's recordEvent: a durable bell-feed entry, also no
//     push toast — just a row the user finds later.
//
// None of the three has a component/behavioural harness here (no vitest, no
// @vue/test-utils in this app — see pumpFence.test.js's note on why), so the
// wiring is asserted against the source, the same crude-but-real pattern.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatViewSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");
const appSrc = fs.readFileSync(path.join(HERE, "..", "App.vue"), "utf8");
const notificationsSrc = fs.readFileSync(path.join(HERE, "notifications.js"), "utf8");

function chatViewOnEventBody() {
	const start = chatViewSrc.indexOf("function onEvent(p) {");
	assert.notEqual(start, -1, "ChatView must still define onEvent(p)");
	const end = chatViewSrc.indexOf("\nfunction ", start + 1);
	assert.notEqual(end, -1, "could not find the end of onEvent");
	return chatViewSrc.slice(start, end);
}

test("PWA ChatView: onEvent's switch has a dedicated import:finished case", () => {
	const body = chatViewOnEventBody();
	assert.match(body, /case "import:finished":/);
});

test("PWA ChatView: the case live-appends via load(), not loadConversation (this frontend has no such helper)", () => {
	const body = chatViewOnEventBody();
	const at = body.indexOf('case "import:finished":');
	const rest = body.slice(at);
	const end = rest.indexOf('\n\t\tcase "', 1);
	const caseBody = end === -1 ? rest : rest.slice(0, end);
	assert.match(caseBody, /\bload\(\);/);
	assert.doesNotMatch(caseBody, /loadConversation/, "the PWA has no loadConversation helper");
});

test("PWA ChatView: the case sits behind the open-conversation guard, like every other case", () => {
	const body = chatViewOnEventBody();
	const guardAt = body.indexOf("if (conv !== convId.value) return;");
	const switchAt = body.indexOf("switch (p.kind) {");
	const caseAt = body.indexOf('case "import:finished":');
	assert.notEqual(guardAt, -1);
	assert.ok(guardAt < switchAt, "the conversation guard must precede the switch");
	assert.ok(switchAt < caseAt, "the case lives inside the switch, behind the guard");
});

test("PWA App.vue: the shell-level handler has an import:finished branch", () => {
	assert.match(appSrc, /p\.kind === "import:finished"/);
});

test("PWA App.vue: the off-screen path calls store.markUnread directly, guarded off the open conversation", () => {
	const at = appSrc.indexOf('p.kind === "import:finished"');
	assert.notEqual(at, -1);
	const rest = appSrc.slice(at);
	const end = rest.indexOf("} else if (", 1);
	const branch = end === -1 ? rest : rest.slice(0, end);
	assert.match(branch, /store\.markUnread\(conv\)/);
	assert.match(branch, /conv !== openConversationId\(\)/);
});

test("PWA App.vue: import:finished never calls notify() — no push toast/browser notification this wave", () => {
	const at = appSrc.indexOf('p.kind === "import:finished"');
	const rest = appSrc.slice(at);
	const end = rest.indexOf("} else if (", 1);
	const branch = end === -1 ? rest : rest.slice(0, end);
	assert.doesNotMatch(branch, /\bnotify\(/);
});

test("PWA notifications.js: recordEvent folds import:finished into the bell feed", () => {
	assert.match(notificationsSrc, /e\.kind === "import:finished"/);
	const at = notificationsSrc.indexOf('e.kind === "import:finished"');
	const rest = notificationsSrc.slice(at);
	const end = rest.indexOf("} else if (", 1);
	const branch = end === -1 ? rest : rest.slice(0, end);
	assert.match(branch, /push\(\{/);
});
