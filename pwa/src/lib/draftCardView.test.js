import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
// The same path the phone app will use: the desktop module, by relative import.
import { draftCardView } from "../../../frontend/src/lib/draftCardView.js";
import { draftCardCases } from "../../../frontend/src/lib/draftCardViewCases.js";

const FIXTURES = JSON.parse(
	fs.readFileSync(
		new URL("../../../frontend/src/lib/draftCardFixtures.json", import.meta.url),
		"utf8"
	)
);

const cases = draftCardCases(draftCardView, FIXTURES);
test("draftCardView: the shared case list is not empty", () => {
	assert.ok(cases.length > 40);
});
for (const c of cases) {
	test(c.name, () => assert.deepEqual(c.actual, c.expected));
}
