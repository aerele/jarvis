import { test } from "node:test";
import assert from "node:assert/strict";
import { resultLine } from "./fileboxResult.js";

test("resultLine: every row waiting on an approval points at the desktop board", () => {
	assert.equal(
		resultLine({ status: "needs_approval", missing: "GST Category (+1 more)", result: "x" }),
		"Needs approval on desktop — missing: GST Category (+1 more)"
	);
	assert.equal(
		resultLine({
			status: "needs_approval",
			missing: "",
			result: "1 approval waiting: New supplier",
		}),
		"1 approval waiting: New supplier — resolve on desktop"
	);
	assert.equal(
		resultLine({ status: "needs_approval", result: "" }),
		"Needs approval — resolve on desktop"
	);
	assert.equal(
		resultLine({ status: "draft_created", result: "Draft PI-1 created" }),
		"Draft PI-1 created"
	);
	assert.equal(resultLine(null), "");
});
