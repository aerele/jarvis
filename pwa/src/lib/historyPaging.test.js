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
	assert.match(load, /api\.getConversation\(cid, \{ refresh, after \}\)/);
	assert.match(load, /const after = heldNewestSeq\(messages\.value, historyPaging\.value\);/);
	assert.match(load, /if \(page\.stale\.length && staleRetry\) load\(false, false\);/);
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
	assert.match(fnBody("function onScroll("), /loadOlder\(\{ auto: true \}\)/);
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

test("a failed older page pauses the scroll trigger and says so", () => {
	const older = fnBody("async function loadOlder(");
	assert.match(
		older,
		/if \(auto && \(!olderArmed \|\| Date\.now\(\) - olderFailedAt < OLDER_RETRY_PAUSE_MS\)\) return;/
	);
	const fail = older.slice(older.indexOf("} catch (e) {"));
	assert.match(fail, /olderFailedAt = Date\.now\(\);/);
	assert.match(fail, /errorBanner\.value =\s+"Couldn't load earlier messages\./);
	assert.match(chat, /const OLDER_RETRY_PAUSE_MS = 10000;/);
});

test("the load button stays focusable while loading and the load is announced", () => {
	const at = chat.indexOf('v-if="historyPaging.hasMore && messages.length"');
	const block = chat.slice(at, chat.indexOf("</div>\n\t\t</div>", at));
	assert.doesNotMatch(block, /:disabled=/);
	assert.match(block, /:aria-disabled="loadingOlder \? 'true' : 'false'"/);
	assert.match(block, /@click="loadOlder\(\)"/);
	assert.match(block, /role="status" aria-live="polite"/);
	assert.match(block, /Loading earlier messages…/);
});

test("the scroll trigger waits for an open or a prepend to land", () => {
	const older = fnBody("async function loadOlder(");
	assert.ok(older.indexOf("olderArmed = false;") < older.indexOf("prependOlderPage("));
	assert.ok(
		older.indexOf("armOlderNextFrame();") > older.indexOf("requestAnimationFrame(hold)")
	);
	const load = fnBody("async function load(");
	assert.match(load, /if \(!messages\.value\.length\) olderArmed = false;/);
	assert.match(load.slice(load.indexOf("} finally {")), /armOlderNextFrame\(\);/);
});
