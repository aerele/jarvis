// The phone card shares the desktop's helper (@shared/lib/draftParked.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { parkedByCard, parkedNoteOf } from "../../../frontend/src/lib/draftParked.js";

test("the phone card treats a parked apply as waiting, not done", () => {
	assert.ok(parkedNoteOf({ ok: true, parked: true, note: "Waiting." }));
	assert.equal(parkedNoteOf({ ok: true, name: "TODO-1" }), "", "an ordinary save is done");
});

test("the phone keeps a parked draft waiting after a reload", () => {
	assert.ok(parkedByCard([{ preview: { from_draft: "m1" } }], "m1"));
	assert.equal(parkedByCard([{ preview: {} }], "m1"), "");
});

test("the phone keeps a draft waiting when the card was seeded from its row", () => {
	assert.ok(parkedByCard([{ preview: { card: { from_draft: "m1" } } }], "m1"));
});
