import { test } from "node:test";
import assert from "node:assert/strict";
import { makeDraftUi } from "./draftCardUi.js";

// Break: the busy kind or the error line is kept on the item object (or any key but the token).
// loadPending replaces the objects on every read, so the card would lose its spinner mid-Create.
test("busy kind and error are found by token, whatever object the item is now", () => {
	const ui = makeDraftUi();
	const before = { token: "t1", conversation: "c" };
	ui.where.setKind(before, "create");
	ui.setError("t1", "Try again");
	const after = { token: "t1", conversation: "c", preview: {} }; // a later read's new object
	assert.deepEqual(ui.where.read(after), { busy: false, busyKind: "create" });
	assert.equal(ui.error("t1"), "Try again");
	assert.deepEqual(ui.busyTokens(), ["t1"]);
	assert.equal(ui.busyKind("t2"), "", "another card is untouched");
});

// Break: an entry with nothing in it is kept (the store grows with every card ever pressed), or a
// card that still has an error loses it when its busy kind is cleared.
test("clearing the kind keeps the error; clearing both removes the entry", () => {
	const ui = makeDraftUi();
	ui.setKind("t1", "cancel");
	ui.setError("t1", "Couldn't discard this action.");
	ui.setKind("t1", "");
	assert.equal(ui.error("t1"), "Couldn't discard this action.");
	assert.deepEqual(ui.busyTokens(), []);
	ui.setError("t1", "");
	const seen = [];
	const spy = makeDraftUi(
		(x) => new Proxy(x, { deleteProperty: (o, k) => (seen.push(k), delete o[k]) })
	);
	spy.setKind("a", "create");
	spy.setKind("a", "");
	assert.deepEqual(seen, ["a"], "the empty entry was deleted");
});

// Break: a switch to another chat leaves the previous chat's busy cards behind.
test("reset drops every card", () => {
	const ui = makeDraftUi();
	ui.setKind("a", "create");
	ui.setError("b", "x");
	ui.reset();
	assert.deepEqual(ui.busyTokens(), []);
	assert.equal(ui.error("b"), "");
});
