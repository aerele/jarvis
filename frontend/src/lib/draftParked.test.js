// frontend/src/lib/draftParked.test.js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	PARKED_FALLBACK,
	markParked,
	parkedByCard,
	parkedNoteFor,
	parkedNoteOf,
} from "./draftParked.js";

test("a parked response yields its note, never a saved state", () => {
	assert.equal(parkedNoteOf({ ok: true, parked: true, note: "Waiting." }), "Waiting.");
	assert.equal(parkedNoteOf({ ok: true, parked: true }), PARKED_FALLBACK);
});

test("an ordinary save or a refusal is not parked", () => {
	assert.equal(parkedNoteOf({ ok: true, name: "TODO-1" }), "");
	assert.equal(parkedNoteOf({ ok: false, parked: true, error: { message: "x" } }), "");
	assert.equal(parkedNoteOf(null), "");
});

test("a parked draft stays marked and others stay applicable", () => {
	const before = new Map();
	const after = markParked(before, "msg-1", "Waiting.");
	assert.equal(parkedNoteFor(after, "msg-1"), "Waiting.");
	assert.equal(parkedNoteFor(after, "msg-2"), "");
	assert.equal(parkedNoteFor(before, "msg-1"), "", "the old map is not mutated");
	assert.equal(parkedNoteFor(after, ""), "");
});

test("a reload finds the waiting card that came from the draft", () => {
	const pending = [
		{ token: "t1", preview: { from_draft: "msg-1" } },
		{ token: "t2", preview: {} },
	];
	assert.equal(parkedByCard(pending, "msg-1"), PARKED_FALLBACK);
	assert.equal(parkedByCard(pending, "msg-2"), "");
	assert.equal(parkedByCard(null, "msg-1"), "");
	assert.equal(parkedByCard(pending, ""), "");
});

test("a reload seeded from the message row still finds the waiting draft", () => {
	// pendingActionFromRow (desktop and PWA) keeps only the row's card; the resync
	// then keeps that entry by token. The card itself names the draft.
	const seededFromRow = {
		token: "t1",
		preview: { card: { kind: "create", from_draft: "msg-1" } },
	};
	const keptByResync = [seededFromRow];
	assert.equal(parkedByCard(keptByResync, "msg-1"), PARKED_FALLBACK, "parked: no Apply offered");
	assert.equal(
		parkedByCard([{ token: "t2", preview: { card: { kind: "create" } } }], "msg-1"),
		""
	);
});
