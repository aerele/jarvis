// A failed draft-panel save (round 2, R2-9): what the panel and the phone card do.
import { test } from "node:test";
import assert from "node:assert/strict";
import { draftFailureOf, FAILURE_FALLBACK } from "./draftFailure.js";

test("a fixable failure keeps the panel open with the server's words and field marks", () => {
	const r = {
		ok: false,
		error: {
			message: "ToDo needs a value for Description.",
			kind: "fixable",
			fields: [{ fieldname: "description" }],
		},
	};
	const f = draftFailureOf(r);
	assert.equal(f.closed, false);
	assert.equal(f.partial, false);
	assert.deepEqual(f.error, r.error);
	assert.equal(f.note, "");
});

test("a not fixable failure closes the panel and says the assistant will explain", () => {
	const f = draftFailureOf({
		ok: false,
		closed: true,
		error: { message: "5 units needed in Stores.", kind: "not_fixable" },
	});
	assert.equal(f.closed, true);
	assert.equal(
		f.note,
		"Couldn't save this: 5 units needed in Stores. The reply in the chat explains what to do."
	);
});

test("a partial failure never says nothing was saved", () => {
	const f = draftFailureOf({
		ok: false,
		closed: true,
		outcome: "partial",
		error: { message: "Second step failed.", hint: "Part of this may already be saved." },
	});
	assert.equal(f.partial, true);
	assert.match(f.note, /may already be saved/);
	assert.doesNotMatch(f.note, /nothing/i);
});

test("the person's words win, and an empty message falls back", () => {
	const f = draftFailureOf({
		ok: false,
		closed: true,
		error: { message: "model words", person_message: "person words" },
	});
	assert.match(f.note, /person words/);
	assert.equal(draftFailureOf({ ok: false, error: {} }).error.message, FAILURE_FALLBACK);
});

test("not a failure: null", () => {
	assert.equal(draftFailureOf({ ok: true }), null);
	assert.equal(draftFailureOf(null), null);
});

test("a closed failure the assistant could not be told about says to ask in the chat", () => {
	const f = draftFailureOf(
		{ ok: false, closed: true, assistant_told: false, error: { message: "No stock." } },
		"Ava"
	);
	assert.equal(f.told, false);
	assert.equal(
		f.note,
		"Couldn't save this: No stock. Ava could not be told about it, so ask in the chat."
	);
	assert.doesNotMatch(f.note, /reply in the chat explains/);
	const partial = draftFailureOf({
		ok: false,
		closed: true,
		outcome: "partial",
		assistant_told: false,
		error: { message: "Half." },
	});
	assert.match(partial.note, /Jarvis could not be told about it, so ask in the chat\.$/);
});

test("a message without a full stop still reads as two sentences", () => {
	const f = draftFailureOf({
		ok: false,
		closed: true,
		error: { message: "Posting is not allowed" },
	});
	assert.equal(
		f.note,
		"Couldn't save this: Posting is not allowed. The reply in the chat explains what to do."
	);
	const p = draftFailureOf({
		ok: false,
		closed: true,
		outcome: "partial",
		error: { message: "Half saved!" },
	});
	assert.equal(
		p.note,
		"Part of this may already be saved: Half saved! Check the record before trying again."
	);
});
