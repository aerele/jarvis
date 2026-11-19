// The phone reads a failed draft save and a failed confirmation's reference with the
// desktop's helpers (@shared/lib/draftFailure.js, @shared/lib/actionSummary.js), so
// both surfaces say the same thing (round 2, R2-9).
import { test } from "node:test";
import assert from "node:assert/strict";
import { draftFailureOf } from "../../../frontend/src/lib/draftFailure.js";
import { failureReferenceOf } from "../../../frontend/src/lib/actionSummary.js";

test("the phone closes its card only when the server closed the panel", () => {
	assert.equal(draftFailureOf({ ok: false, error: { message: "Value missing" } }).closed, false);
	const closed = draftFailureOf({ ok: false, closed: true, error: { message: "No stock." } });
	assert.equal(closed.closed, true);
	assert.match(closed.note, /No stock\./);
});

test("the phone shows a failed confirmation's reference, and none without one", () => {
	assert.equal(failureReferenceOf({ ok: false, error: { reference: "pa-1" } }), "pa-1");
	assert.equal(failureReferenceOf({ ok: false, error: { message: "x" } }), "");
});
