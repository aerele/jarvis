// The PWA pages its history like the desktop chat (#715 F13), through the shared
// frontend/src/lib/historyPaging.js. There is no component harness in this app
// (see importFinishedRender.test.js), so the wiring is asserted on the source.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { mergeNewestPage, pendingSeedRows } from "../../../frontend/src/lib/historyPaging.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const read = (...p) => fs.readFileSync(path.join(HERE, "..", ...p), "utf8");
const chat = read("views", "ChatView.vue");
const fnBody = (head) => {
	const at = chat.indexOf(head);
	assert.notEqual(at, -1, `${head} must exist`);
	return chat.slice(at, chat.indexOf("\n}\n", at));
};

test("a reload re-reads only the newest page and keeps the older pages on screen", () => {
	const load = fnBody("async function load(");
	assert.match(load, /api\.getConversation\(cid, \{ refresh \}\)/);
	assert.match(load, /mergeNewestPage\(messages\.value, historyPaging\.value, d, refresh\)/);
	assert.match(load, /olderPending\.value = d\?\.older_pending \|\| \[\];/);
	const held = [
		{ name: "a", seq: 1 },
		{ name: "b", seq: 2 },
	];
	const d = {
		messages: [
			{ name: "b", seq: 2 },
			{ name: "c", seq: 3 },
		],
		paging: { has_more: true, before: 2 },
	};
	const merged = mergeNewestPage(held, { hasMore: false, before: null, newest: 2 }, d);
	assert.deepEqual(
		merged.messages.map((m) => m.name),
		["a", "b", "c"]
	);
});

test("an older page loads near the top, above the rows on screen", () => {
	assert.match(fnBody("function onScroll("), /loadOlder\(\)/);
	const older = fnBody("async function loadOlder(");
	assert.match(older, /api\.getConversation\(cid, \{ before: paging\.before \}\)/);
	assert.match(older, /prependOlderPage\(messages\.value, historyPaging\.value, d\)/);
	assert.match(older, /scrollHeight - fromBottom/);
	assert.match(chat, /v-if="historyPaging\.hasMore && messages\.length"/);
});

test("a card older than every loaded page still reaches the approval queue", () => {
	assert.match(
		fnBody("async function loadPending("),
		/\[\.\.\.olderPending\.value, \.\.\.\(messages\.value \|\| \[\]\)\]/
	);
	const rows = pendingSeedRows({ older_pending: [{ name: "old" }] }, [{ name: "new" }]);
	assert.deepEqual(
		rows.map((m) => m.name),
		["old", "new"]
	);
});

test("switching chats drops the paging state of the chat left behind", () => {
	const sw = chat.slice(chat.indexOf("() => props.id,"));
	const body = sw.slice(0, sw.indexOf("\n);"));
	assert.match(body, /historyPaging\.value = \{ hasMore: false, before: null, newest: null \};/);
	assert.match(body, /olderPending\.value = \[\];/);
});
