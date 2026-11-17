import { test } from "node:test";
import assert from "node:assert/strict";
import { makeDraftUi, olderCardNotes, rowsHoldPendingDraft } from "./draftCardUi.js";

// Break: the busy kind or the error line is kept on the item object (or any key but the token).
// loadPending replaces the objects on every read, so the card would lose its spinner mid-Create.
test("busy kind and error are found by token, whatever object the item is now", () => {
	const ui = makeDraftUi();
	const before = { token: "t1", conversation: "c" };
	ui.where.setKind(before, "create");
	ui.setError("t1", "Try again");
	const after = { token: "t1", conversation: "c", preview: {} }; // a later read's new object
	assert.deepEqual(ui.where.read(after), {
		busy: false,
		busyKind: "create",
		error: "Try again",
	});
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

// Break: an error line stays behind for a card that left the list (it would come back with a later
// card of the same token), or a card in the middle of its own Create loses its entry.
test("prune drops the entries of cards that left the list, never one in flight", () => {
	const ui = makeDraftUi();
	ui.setError("gone", "Old error");
	ui.setError("here", "Kept error");
	ui.setKind("busy", "create");
	ui.setError("busy", "Reply decides");
	ui.prune(["here"]);
	assert.equal(ui.error("gone"), "");
	assert.equal(ui.error("here"), "Kept error");
	assert.equal(ui.error("busy"), "Reply decides");
	assert.deepEqual(ui.busyTokens(), ["busy"]);
});

// Break: a typed draft card flashes on screen between the thread arriving and the pending list
// arriving (the thread row already says a draft card is pending), or flag-off threads (no row has
// draft_card) are touched.
test("a pending draft row in the thread is known before the pending list arrives", () => {
	const row = (extra) => ({ role: "tool", tool_call_id: "t1", ...extra });
	assert.equal(rowsHoldPendingDraft([row({ draft_card: true })]), true);
	assert.equal(rowsHoldPendingDraft([], [row({ draft_card: true })]), true);
	// every row of a flag-off thread, a sensitive card, a decided draft and a non-tool row
	for (const rows of [
		[row({})],
		[row({ draft_card: false })],
		[row({ draft_receipt: true, action_outcome: "confirmed" })],
		[{ role: "assistant", draft_card: true }],
		[],
		null,
	])
		assert.equal(rowsHoldPendingDraft(rows), false);
});

// Break: the tile's "Tap it to approve or discard" shows for an Earlier DRAFT card (it has its own
// buttons), the draft's own line is missing, or a legacy tile loses its note.
test("the older-cards notes: the tile's line for tiles, the draft's line for drafts", () => {
	const draft = { token: "d", isDraft: true, recent: false };
	const tile = { token: "t", isDraft: false, recent: false };
	const fresh = { token: "f", isDraft: true, recent: true };
	const args = (note) => ({
		note,
		isDraft: (p) => p.isDraft,
		isRecent: (p) => p.recent,
	});
	assert.deepEqual(olderCardNotes([draft], args(true)), { tile: false, draft: true });
	assert.deepEqual(olderCardNotes([tile], args(true)), { tile: true, draft: false });
	assert.deepEqual(olderCardNotes([tile, draft], args(true)), { tile: true, draft: true });
	// a card parked after the person's last message needs no note; no note without the server's flag
	assert.deepEqual(olderCardNotes([fresh], args(true)), { tile: false, draft: false });
	assert.deepEqual(olderCardNotes([tile, draft], args(false)), { tile: false, draft: false });
});
