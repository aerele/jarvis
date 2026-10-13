// History paging (#715 F13): how a chat holds its newest page plus older pages,
// and how a resync merges a fresh newest page without losing or duplicating rows.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
	mergeNewestPage,
	needsToolDetail,
	pagingOf,
	pendingSeedRows,
	prependOlderPage,
	refreshNames,
	withToolDetail,
} from "./historyPaging.js";

const row = (seq, extra = {}) => ({ name: `m${seq}`, seq, role: "assistant", ...extra });
const rows = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => row(from + i));
const page = (from, to, paging = {}, extra = {}) => ({
	messages: rows(from, to),
	paging: { limit: 80, before: from, has_more: true, ...paging },
	...extra,
});
const names = (list) => list.map((m) => m.name);

test("a whole-thread answer has no older page", () => {
	assert.deepEqual(pagingOf({ messages: rows(1, 3) }), {
		hasMore: false,
		before: null,
		newest: 3,
	});
	assert.deepEqual(pagingOf(page(5, 9)), { hasMore: true, before: 5, newest: 9 });
	assert.deepEqual(pagingOf(page(1, 9, { has_more: false, before: null })).hasMore, false);
});

test("a resync keeps the older pages already loaded and refreshes only the newest", () => {
	// Loaded: 20..29 (an older page) + 30..40 (the newest page). A turn added 41..42.
	const held = rows(20, 40);
	const heldPaging = { hasMore: true, before: 20, newest: 40 };
	const merged = mergeNewestPage(held, heldPaging, page(33, 42));
	assert.deepEqual(names(merged.messages), names(rows(20, 42)));
	assert.deepEqual(merged.paging, { hasMore: true, before: 20, newest: 42 });
});

test("a page that reaches the first row replaces everything held", () => {
	const merged = mergeNewestPage(
		rows(1, 40),
		{ hasMore: false, before: null, newest: 40 },
		{
			messages: rows(1, 2),
			paging: { has_more: false, before: null },
		}
	);
	assert.deepEqual(names(merged.messages), ["m1", "m2"], "a cleared chat keeps no ghosts");
});

test("a page that starts past the newest held row starts over instead of leaving a hole", () => {
	const held = rows(20, 40);
	const merged = mergeNewestPage(held, { hasMore: true, before: 20, newest: 40 }, page(60, 70));
	assert.deepEqual(names(merged.messages), names(rows(60, 70)));
	assert.deepEqual(merged.paging, { hasMore: true, before: 60, newest: 70 });
});

test("optimistic rows without a seq never survive as held rows", () => {
	const held = [...rows(20, 40), { name: "optimistic", role: "user" }];
	const merged = mergeNewestPage(held, { hasMore: true, before: 20, newest: 40 }, page(35, 41));
	assert.ok(!names(merged.messages).includes("optimistic"));
});

test("held pending cards are refreshed, resolved in place, or dropped when gone", () => {
	const held = [
		row(10, { role: "tool", tool_status: "pending" }),
		row(11, { role: "tool", tool_status: "pending" }),
		row(12, { role: "tool", tool_status: "pending" }),
		...rows(13, 40),
	];
	const asked = refreshNames(held);
	assert.deepEqual(asked, ["m10", "m11", "m12"]);
	const d = page(
		35,
		41,
		{},
		{
			older_pending: [row(12, { role: "tool", tool_status: "pending", recent: false })],
			refreshed: [
				row(10, { role: "tool", tool_status: "completed", action_outcome: "confirmed" }),
			],
		}
	);
	const merged = mergeNewestPage(held, { hasMore: true, before: 10, newest: 40 }, d, asked);
	const byName = Object.fromEntries(merged.messages.map((m) => [m.name, m]));
	assert.equal(byName.m10.action_outcome, "confirmed", "a resolved card becomes its receipt");
	assert.equal(byName.m11, undefined, "a row asked for but not returned is gone");
	assert.equal(byName.m12.recent, false, "a still-pending card takes the fresh copy");
});

test("pending cards older than everything loaded still seed the confirm queue", () => {
	const d = { older_pending: [row(3, { role: "tool", tool_status: "pending" })] };
	assert.deepEqual(names(pendingSeedRows(d, rows(50, 51))), ["m3", "m50", "m51"]);
	assert.deepEqual(names(pendingSeedRows({}, rows(1, 1))), ["m1"]);
});

test("an older page goes above the held rows without duplicates and moves the cursor", () => {
	const held = rows(30, 40);
	const out = prependOlderPage(held, { hasMore: true, before: 30, newest: 40 }, page(20, 30));
	assert.deepEqual(names(out.messages), names(rows(20, 40)));
	assert.deepEqual(out.paging, { hasMore: true, before: 20, newest: 40 });
	const first = prependOlderPage(
		held,
		{ hasMore: true, before: 30, newest: 40 },
		{
			messages: rows(1, 29),
			paging: { has_more: false, before: null },
		}
	);
	assert.equal(first.paging.hasMore, false);
});

test("a compact tool row is fetched once, then shown whole", () => {
	const compact = row(7, { role: "tool", tool_result: '{"ok":true}', tool_result_compact: 1 });
	assert.equal(needsToolDetail(compact, {}), true);
	const details = { m7: { tool_args: '{"a":1}', tool_result: '{"ok":true,"data":[1,2]}' } };
	assert.equal(needsToolDetail(compact, details), false);
	assert.equal(withToolDetail(compact, details).tool_result, details.m7.tool_result);
	assert.equal(
		needsToolDetail(row(8, { role: "tool" }), {}),
		false,
		"a whole row needs nothing"
	);
	assert.equal(withToolDetail(row(8), details).name, "m8");
});

// ---- wiring: who reads pages, who keeps whole threads ----
const src = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
const fnBody = (text, head) => {
	const at = text.indexOf(head);
	assert.notEqual(at, -1, `${head} must exist`);
	return text.slice(at, text.indexOf("\n}\n", at));
};

test("the open chat asks for pages; Dashboards, Triggers and the desk widget keep whole threads", () => {
	assert.match(
		fnBody(src("../api.js"), "export const getConversation"),
		/limit: HISTORY_PAGE_SIZE/
	);
	assert.match(src("../../../pwa/src/api.js"), /limit: HISTORY_PAGE_SIZE/);
	for (const rel of [
		"../api/dashboards.js",
		"../api/triggers.js",
		"../../../jarvis/public/js/jarvis_chat/widget/panel_api.mjs",
	])
		assert.match(src(rel), /get_conversation", \{ conversation \}\)/, rel);
});

test("the desktop chat merges the newest page, seeds old cards and loads older pages in place", () => {
	const chat = src("../views/ChatView.vue");
	const load = fnBody(chat, "async function loadConversation(");
	assert.match(load, /mergeNewestPage\(/);
	assert.match(load, /seedPendingFromRows\(pendingSeedRows\(d, messages\.value\), id\)/);
	const older = fnBody(chat, "async function loadOlder(");
	assert.match(older, /api\.getConversation\(id, \{ before: paging\.before \}\)/);
	assert.match(older, /prependOlderPage\(/);
	assert.match(older, /holdFromBottom\(fromBottom\)/);
	assert.match(fnBody(chat, "function onThreadScroll("), /loadOlder\(\{ auto: true \}\)/);
	assert.match(fnBody(chat, "function toggleTool("), /loadToolDetail\(name\)/);
	assert.match(chat, /prettyJson\(\s*toolShown\(item\.row\)\.tool_result\s*\)/);
	assert.match(chat, /<Button\s+variant="subtle"\s+label="Load earlier messages"/);
	// a chat switch drops the paging state that belonged to the chat left behind
	const sw = chat.slice(chat.indexOf("watch(currentId, (id) => {"));
	assert.match(sw.slice(0, sw.indexOf("\n});")), /historyPaging\.value = \{ conv: null/);
});
