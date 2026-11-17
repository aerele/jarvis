import { test } from "node:test";
import assert from "node:assert/strict";
import { PRIVACY_NOTICE } from "../../../frontend/src/lib/privacyNotice.js";

test("PRIVACY_NOTICE: exact wording, no em-dash", () => {
	assert.equal(
		PRIVACY_NOTICE,
		"Your admin can see what you do in Jarvis: which records you read, export or change, and how much you use it. Not your messages."
	);
	assert.ok(!PRIVACY_NOTICE.includes("—"));
});
