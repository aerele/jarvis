// History paging (#715 F13): how a chat holds its newest page plus older pages,
// and how a resync merges a fresh newest page without losing or duplicating rows.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
	heldNewestSeq,
	mergeNewestPage,
	needsToolDetail,
	pagingOf,
	pendingSeedRows,
	prependOlderPage,
	promptRows,
	refreshNames,
	threadCounts,
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
const NONE_BELOW = { user: 0, assistant: 0, receipt: 0 };

test("a whole-thread answer has no older page", () => {
	assert.deepEqual(pagingOf({ messages: rows(1, 3) }), {
		hasMore: false,
		before: null,
		newest: 3,
		older: NONE_BELOW,
	});
	assert.deepEqual(pagingOf(page(5, 9)), {
		hasMore: true,
		before: 5,
		newest: 9,
		older: NONE_BELOW,
	});
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
	assert.deepEqual(merged.paging, { hasMore: true, before: 60, newest: 70, older: NONE_BELOW });
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
	assert.deepEqual(out.paging, { hasMore: true, before: 20, newest: 40, older: NONE_BELOW });
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

test("a long live session keeps the older pages: the resync page reaches back to the held rows", () => {
	// Loaded: an older page 20..29 and the newest page 30..40. Then 300 rows arrived
	// through realtime events only (no seq on screen), so paging.newest is still 40.
	const realtime = Array.from({ length: 300 }, (_, i) => ({
		name: `live${i}`,
		role: "assistant",
	}));
	const held = [...rows(20, 40), ...realtime];
	const heldPaging = { hasMore: true, before: 20, newest: 40 };
	assert.equal(heldNewestSeq(held, heldPaging), 40, "realtime rows never count");
	// The resync passes after=40 and the server's page starts there (paging.start).
	const d = page(40, 340, { start: 40, before: 40 });
	const merged = mergeNewestPage(held, heldPaging, d);
	assert.deepEqual(names(merged.messages), names(rows(20, 340)));
	assert.deepEqual(merged.paging, { hasMore: true, before: 20, newest: 340 });
});

test("the newest seq held counts server rows merged after the last page", () => {
	const held = rows(20, 60);
	assert.equal(heldNewestSeq(held, { newest: 40 }), 60);
	const merged = mergeNewestPage(held, { hasMore: true, before: 20, newest: 40 }, page(55, 70));
	assert.deepEqual(names(merged.messages), names(rows(20, 70)), "no false gap");
	assert.equal(heldNewestSeq(rows(1, 3), null), 3);
});

test("pending cards an older page brought in mid-read are named for one more refresh", () => {
	const late = row(12, { role: "tool", tool_status: "pending" });
	const held = [late, ...rows(13, 40)];
	const merged = mergeNewestPage(
		held,
		{ hasMore: true, before: 12, newest: 40 },
		page(35, 41),
		[]
	);
	assert.deepEqual(merged.stale, ["m12"]);
	const again = mergeNewestPage(held, { hasMore: true, before: 12, newest: 40 }, page(35, 41), [
		"m12",
	]);
	assert.deepEqual(again.stale, [], "asked for: refreshed or dropped, never stale");
});

test("prompt recall walks the earlier prompts plus loaded user rows, once each, oldest first", () => {
	const recent = [
		{ name: "u1", seq: 1, role: "user", content: "first" },
		{ name: "u5", seq: 5, role: "user", content: "second" },
	];
	const loaded = [
		{ name: "u5", seq: 5, role: "user", content: "second" },
		{ name: "a6", seq: 6, role: "assistant", content: "reply" },
		{ name: "u9", seq: 9, role: "user", content: "third" },
		{ name: "opt", role: "user", content: "unsent bubble" },
	];
	assert.deepEqual(
		promptRows(recent, loaded).map((m) => m.content),
		["first", "second", "third"]
	);
});

test("message totals cover the whole chat while only some pages are loaded", () => {
	const shown = [
		{ name: "u1", role: "user" },
		{ name: "a1", role: "assistant" },
		{ name: "r1", role: "tool", action_outcome: "confirmed" },
	];
	// The newest page of a 500-message chat: the server counted what sits below it.
	const d = page(480, 482, { older_counts: { user: 240, assistant: 238, receipt: 3 } });
	const paging = pagingOf(d);
	assert.deepEqual(threadCounts(shown, paging), { total: 484, user: 241, assistant: 239 });
	// Loading the page above moves the count below it; the total stays the same.
	const older = prependOlderPage(
		rows(480, 482),
		paging,
		page(470, 479, { older_counts: { user: 235, assistant: 233, receipt: 3 } })
	);
	const shownMore = [
		...shown,
		...Array.from({ length: 5 }, (_, i) => ({ name: `u${i + 2}`, role: "user" })),
		...Array.from({ length: 5 }, (_, i) => ({ name: `a${i + 2}`, role: "assistant" })),
	];
	assert.equal(threadCounts(shownMore, older.paging).total, 484);
	// A resync that keeps the older pages keeps their count; a whole thread counts only what is shown.
	const merged = mergeNewestPage(rows(470, 482), older.paging, page(481, 483));
	assert.deepEqual(merged.paging.older, older.paging.older);
	assert.equal(threadCounts(shown, pagingOf({ messages: rows(1, 3) })).total, 3);
	assert.equal(threadCounts(shown, null).total, 3);
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
	// The header's "N messages" and the usage pane count the whole chat, not the loaded rows.
	assert.match(chat, /const msgCount = computed\(\(\) => threadTotals\.value\.total\);/);
	assert.match(fnBody(chat, "const headerSub = computed("), /const n = msgCount\.value;/);
	// Up-arrow recall reaches the prompts older than every loaded page.
	assert.match(load, /recentPrompts = d\?\.recent_prompts \|\| \[\];/);
	assert.match(
		load,
		/promptHistory\.value = promptsOf\(promptRows\(recentPrompts, page\.messages\)\);/
	);
	assert.match(
		older,
		/promptHistory\.value = promptsOf\(promptRows\(recentPrompts, messages\.value\)\);/
	);
	// A resync reaches back to the newest seq held; a mid-read older page gets one retry.
	assert.match(
		load,
		/const after = holding \? heldNewestSeq\(messages\.value, historyPaging\.value\) : null;/
	);
	assert.match(
		load,
		/if \(page\.stale\.length && staleRetry\)\s+loadConversation\(id, \{ staleRetry: false \}\)/
	);
	// a chat switch drops the paging state that belonged to the chat left behind
	const sw = chat.slice(chat.indexOf("watch(currentId, (id) => {"));
	assert.match(sw.slice(0, sw.indexOf("\n});")), /historyPaging\.value = \{ conv: null/);
});

test("the desktop scroll trigger waits for an open or a prepend to land", () => {
	const chat = src("../views/ChatView.vue");
	assert.match(
		fnBody(chat, "function onThreadScroll("),
		/if \(_olderArmed && el && el\.scrollTop < OLDER_PAGE_TRIGGER_PX\) loadOlder\(\{ auto: true \}\);/
	);
	const load = fnBody(chat, "async function loadConversation(");
	assert.ok(
		load.indexOf("if (!_sameConv) _olderArmed = false;") <
			load.indexOf("await api.getConversation")
	);
	assert.ok(load.indexOf("armOlderNextFrame();") > load.indexOf("scrollBottom();"));
	const older = fnBody(chat, "async function loadOlder(");
	assert.ok(older.indexOf("_olderArmed = false;") < older.indexOf("prependOlderPage("));
	assert.ok(older.indexOf("armOlderNextFrame();") > older.indexOf("holdFromBottom(fromBottom)"));
});
