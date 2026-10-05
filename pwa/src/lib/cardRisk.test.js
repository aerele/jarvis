// The phone card renders the risk banner, the trial warning and a sensitive update's
// line diff from the desktop's helpers (@shared/lib/actionSummary.js), so the chat,
// the phone and the Approval Board say the same thing.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	cardBannerOf,
	cardWarningOf,
	diffLineView,
	diffTablesOf,
	personError,
} from "../../../frontend/src/lib/actionSummary.js";

test("the phone leads a sensitive card with its risk line", () => {
	assert.deepEqual(
		cardBannerOf({
			kind: "update",
			risk: "sensitive",
			risk_line: "This sends data outside the site.",
		}),
		{ risk: "sensitive", text: "This sends data outside the site." }
	);
	assert.equal(cardBannerOf({ kind: "update" }), null, "an ordinary or old card has no banner");
});

test("the phone shows one warning line with the real job count", () => {
	assert.equal(
		cardWarningOf({ warning: { jobs: 500, rolled_back: false } }),
		"This will also start 500 background jobs."
	);
	assert.equal(cardWarningOf({ kind: "create" }), "", "no warning on a clean trial");
});

test("the phone marks each line of a sensitive update's diff", () => {
	assert.deepEqual(
		[{ op: "-", text: "a" }, { op: "+", text: "b" }, { op: "gap" }].map(diffLineView),
		[
			{ kind: "del", text: "- a" },
			{ kind: "add", text: "+ b" },
			{ kind: "gap", text: "…" },
		]
	);
});

test("the phone shows a sensitive child-table change as both tables", () => {
	const t = { rows: [{ cells: ["Authorization", "Bearer abc"] }], count: 1 };
	assert.deepEqual(
		diffTablesOf({ from_table: t }).map((s) => s.heading),
		["Current · 1 row", "New · none"]
	);
});

test("the phone's decision sheet shows a refusal in the person's words", () => {
	const err = {
		message: "... Do not retry it with another tool.",
		person_message: "Make it in Desk.",
	};
	assert.equal(personError(err).message, "Make it in Desk.");
});
