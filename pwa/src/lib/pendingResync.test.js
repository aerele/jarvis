import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { mergePendingSources, withExcluded } from "./pendingResync.js";

// P0c in-flight suppression: a Confirm/Discard RPC in flight for token T must
// not have its card re-added by a stale resync read racing the RPC's own
// removal.

test("merges base + fresh sources, first occurrence wins, in priority order", () => {
	const base = [{ token: "a", summary: "from base" }];
	const rows = [
		{ token: "a", summary: "from rows" },
		{ token: "b", summary: "row-only" },
	];
	const backstop = [
		{ token: "b", summary: "backstop" },
		{ token: "c", summary: "backstop-only" },
	];
	const out = mergePendingSources(base, [rows, backstop], new Set());
	assert.deepEqual(
		out.map((c) => c.token),
		["a", "b", "c"]
	);
	// base wins over a fresh source for the same token (base is passed first).
	assert.equal(out[0].summary, "from base");
	// rows (earlier fresh source) wins over backstop for the same token.
	assert.equal(out[1].summary, "row-only");
});

test("drops an in-flight token from every fresh source", () => {
	const rows = [{ token: "a" }, { token: "b" }];
	const backstop = [{ token: "b" }, { token: "c" }];
	const out = mergePendingSources([], [rows, backstop], new Set(["b"]));
	assert.deepEqual(
		out.map((c) => c.token),
		["a", "c"]
	);
});

test("base is NEVER filtered by in-flight tokens - it only preserves what is already on screen", () => {
	const base = [{ token: "a" }];
	const out = mergePendingSources(base, [[]], new Set(["a"]));
	assert.deepEqual(
		out.map((c) => c.token),
		["a"]
	);
});

test("an in-flight token already resolved (removed from base) is not resurrected by a stale read", () => {
	// The realistic P0c race: discardPending already removed "x" from base
	// (pending.value), but a stale resync response still carries it.
	const out = mergePendingSources([], [[{ token: "x" }]], new Set(["x"]));
	assert.deepEqual(out, []);
});

test("tolerates missing/empty inputs without throwing", () => {
	assert.deepEqual(mergePendingSources(null, [null, undefined], new Set()), []);
	assert.deepEqual(mergePendingSources([], [], new Set()), []);
});

// D1: a typed "no" only drops its cards from `pending.value` (ChatView.vue's
// send()); the transcript row (messages.value) stays tool_status "pending"
// until the next load(). A resync racing that window must not rebuild the
// card from that stale row - the discarded token has to survive in the
// exclusion set past the RPC that settled it, not just for the single RPC
// mergePendingSources already guards (inflightTokens, above).
test("withExcluded unions new tokens into the running exclusion set", () => {
	const out = withExcluded(new Set(["a"]), ["b", "b", null, undefined]);
	assert.deepEqual([...out].sort(), ["a", "b"]);
	assert.deepEqual([...withExcluded(null, ["x"])], ["x"]);
	assert.deepEqual([...withExcluded(new Set(["a"]), null)], ["a"]);
});

test("a token settled locally (typed no) is never resurrected by a stale transcript row", () => {
	// ChatView's send() folds discardedTokens(res) into settledTokens via
	// withExcluded; the next loadPending() must exclude it from fromRows even
	// though messages.value is still the pre-send snapshot.
	const settled = withExcluded(new Set(), ["x"]);
	const staleFromRows = [{ token: "x" }];
	const out = mergePendingSources([], [staleFromRows], settled);
	assert.deepEqual(out, []);
});

test("ChatView folds a typed no's discarded tokens into loadPending's exclusion set", () => {
	const src = fs.readFileSync(new URL("../views/ChatView.vue", import.meta.url), "utf8");
	const settle = src.indexOf(
		"settledTokens.value = withExcluded(settledTokens.value, discarded)"
	);
	assert.ok(settle > -1, "send() must fold discarded tokens into settledTokens");
	const load = src.indexOf("async function loadPending(");
	const inflight = src.indexOf("const inflight = withExcluded(", load);
	assert.ok(inflight > -1, "loadPending must build its exclusion set from settledTokens");
});
