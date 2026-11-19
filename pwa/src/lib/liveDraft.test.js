import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { liveDraftRow, isDraftRow } from "../../../frontend/src/lib/liveDraft.js";

const CASES = JSON.parse(
	fs.readFileSync(
		new URL("../../../frontend/src/lib/liveDraftCases.json", import.meta.url),
		"utf8"
	)
);

test("liveDraft: the case list is not empty", () => {
	assert.ok(CASES.length > 0);
});

for (const c of CASES) {
	test(`liveDraft ${c.id}: ${c.note}`, () => {
		assert.equal(
			liveDraftRow(c.rows, { parkedDraft: !!c.parked_draft })?.name ?? null,
			c.live
		);
	});
}

test("isDraftRow is false for an email block and a gated verb block", () => {
	const row = (id) => CASES.find((x) => x.id === id).rows[1];
	assert.equal(isDraftRow(row("draft-then-email-block")), false);
	assert.equal(isDraftRow(row("draft-then-gated-verb-block")), false);
});

test("liveDraftRow tolerates an empty or null list", () => {
	assert.equal(liveDraftRow([]), null);
	assert.equal(liveDraftRow(null), null);
});
