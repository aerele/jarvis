import { test } from "node:test";
import assert from "node:assert/strict";
import { reconcilePending } from "./pendingResync.js";

// P0c in-flight suppression: a Confirm/Discard RPC in flight for token T must
// not have its card re-added by a stale resync read racing the RPC's own
// removal.

test("drops a card no longer server-live, keeps one the server still lists", () => {
	const existing = [
		{ token: "gone", conversation: "c1", busy: false },
		{ token: "live", conversation: "c1", busy: false },
	];
	const { kept, toAdd } = reconcilePending(existing, "c1", [{ token: "live" }], new Set());
	assert.deepEqual(
		kept.map((c) => c.token),
		["live"]
	);
	assert.deepEqual(toAdd, []);
});

test("never drops a BUSY card for this conversation, even if the server no longer lists it", () => {
	const existing = [{ token: "busy-one", conversation: "c1", busy: true }];
	const { kept } = reconcilePending(existing, "c1", [], new Set());
	assert.deepEqual(
		kept.map((c) => c.token),
		["busy-one"]
	);
});

test("leaves other conversations' cards untouched", () => {
	const existing = [{ token: "other-conv", conversation: "c2", busy: false }];
	const { kept } = reconcilePending(existing, "c1", [], new Set());
	assert.deepEqual(
		kept.map((c) => c.token),
		["other-conv"]
	);
});

test("adds a server-live token the queue does not have yet", () => {
	const { kept, toAdd } = reconcilePending([], "c1", [{ token: "new" }], new Set());
	assert.deepEqual(kept, []);
	assert.deepEqual(
		toAdd.map((c) => c.token),
		["new"]
	);
});

test("an in-flight token is never added, even though the server still lists it live", () => {
	// The realistic P0c race: confirmPending already removed "x" from
	// pendingActions, but a stale resync response (issued before the confirm
	// committed) still carries it as pending.
	const { toAdd } = reconcilePending([], "c1", [{ token: "x" }], new Set(["x"]));
	assert.deepEqual(toAdd, []);
});

test("an in-flight token already kept (still on screen, busy) is not duplicated into toAdd", () => {
	const existing = [{ token: "x", conversation: "c1", busy: true }];
	const { kept, toAdd } = reconcilePending(existing, "c1", [{ token: "x" }], new Set(["x"]));
	assert.deepEqual(
		kept.map((c) => c.token),
		["x"]
	);
	assert.deepEqual(toAdd, []);
});

test("tolerates missing/empty inputs without throwing", () => {
	assert.deepEqual(reconcilePending(null, "c1", null, new Set()), { kept: [], toAdd: [] });
	assert.deepEqual(reconcilePending([], "c1", [{ token: "" }], new Set()), {
		kept: [],
		toAdd: [],
	});
});
