// Which background-export events reach the phone's bell feed and unread dot. The rule is the
// same as the desktop's (frontend/src/notify/globalNotifier.js). notifications.js cannot be
// imported by node (it pulls in "@/branding"), so the rule lives in this dependency-free file
// and the wiring is asserted against the source, as importFinishedRender.test.js does.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { exportFeedEntry, exportSignals } from "./exportFeed.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const appSrc = fs.readFileSync(path.join(HERE, "..", "App.vue"), "utf8");
const notificationsSrc = fs.readFileSync(path.join(HERE, "notifications.js"), "utf8");

const event = (over = {}) => ({
	kind: "export:changed",
	conversation_id: "conv-a",
	export_job: "job-1",
	message_id: "msg-1",
	status: "ready",
	title: "Suppliers",
	initial: false,
	...over,
});

test("a ready export becomes one 'Export ready' entry naming the export", () => {
	const entry = exportFeedEntry(event(), 1000);
	assert.deepEqual(entry, {
		id: "exp:msg-1",
		kind: "export-finished",
		title: "Export ready",
		body: "Suppliers",
		conversation: "conv-a",
		at: 1000,
		read: false,
	});
});

test("an export that did not finish becomes an 'Export stopped' entry", () => {
	for (const status of ["failed", "too large", "too slow"]) {
		const entry = exportFeedEntry(event({ status }), 1);
		assert.equal(entry.title, "Export stopped", status);
		assert.equal(exportSignals(event({ status })), true, status);
	}
});

test("states that are not an outcome make no entry and no dot", () => {
	for (const status of ["queued", "running", "paused", "cancelled", "expired"]) {
		assert.equal(exportFeedEntry(event({ status }), 1), null, status);
		assert.equal(exportSignals(event({ status })), false, status);
	}
	assert.equal(exportFeedEntry(event({ initial: true }), 1), null);
	assert.equal(exportFeedEntry(event({ conversation_id: "" }), 1), null);
	assert.equal(exportFeedEntry({ kind: "import:finished", conversation_id: "c" }, 1), null);
});

test("the id is stable per message, so a replayed event cannot double-post", () => {
	assert.equal(exportFeedEntry(event(), 1).id, exportFeedEntry(event(), 99).id);
	assert.equal(exportFeedEntry(event({ message_id: "" }), 1).id, "exp:conv-a");
});

test("an export without a title still says something", () => {
	assert.equal(exportFeedEntry(event({ title: "" }), 1).body, "Your export changed state.");
});

test("notifications.js folds export events into the feed through exportFeedEntry", () => {
	assert.match(notificationsSrc, /from "\.\/exportFeed\.js"/);
	assert.match(notificationsSrc, /e\.kind === "export:changed"/);
	const at = notificationsSrc.indexOf('e.kind === "export:changed"');
	const rest = notificationsSrc.slice(at);
	const end = rest.indexOf("} else if (", 1);
	const branch = end === -1 ? rest : rest.slice(0, end);
	assert.match(branch, /exportFeedEntry\(e, at\)/);
	assert.match(branch, /push\(entry\)/);
});

test("App.vue marks the chat unread for an export outcome, with no push", () => {
	assert.match(appSrc, /exportSignals\(p\)/);
	const at = appSrc.indexOf("exportSignals(p)");
	const rest = appSrc.slice(at);
	const end = rest.indexOf("} else if (", 1);
	const branch = end === -1 ? rest : rest.slice(0, end);
	assert.match(branch, /store\.markUnread\(conv\)/);
	assert.match(branch, /conv !== openConversationId\(\)/);
	assert.doesNotMatch(branch, /\bnotify\(/);
});
