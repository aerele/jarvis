import { test } from "node:test";
import assert from "node:assert/strict";
import { effortOffered, sendThinking, thinkingOf } from "./prefs.js";

test("thinkingOf: Balanced is the default, so it sends no level", () => {
	assert.equal(thinkingOf("Balanced"), "");
});

test("thinkingOf: an unknown or missing effort sends no level", () => {
	assert.equal(thinkingOf(undefined), "");
	assert.equal(thinkingOf("Turbo"), "");
});

test("thinkingOf: Fast and Thorough map to the backend's low and high", () => {
	assert.equal(thinkingOf("Fast"), "low");
	assert.equal(thinkingOf("Thorough"), "high");
});

test("effortOffered: only when the server lists thinking levels", () => {
	assert.equal(effortOffered({ thinking_levels: ["low", "medium", "high"] }), true);
	assert.equal(effortOffered({ thinking_levels: [] }), false);
	assert.equal(effortOffered({}), false);
	assert.equal(effortOffered(null), false);
});

test("sendThinking: drops the pick only when the server lists no levels", () => {
	assert.equal(sendThinking({ thinking_levels: ["low", "medium", "high"] }, "Thorough"), "high");
	assert.equal(sendThinking({ thinking_levels: [] }, "Thorough"), "");
	assert.equal(sendThinking({ thinking_levels: [] }, "Balanced"), "");
});

test("sendThinking: keeps the pick while settings are loading or failed", () => {
	assert.equal(sendThinking(null, "Thorough"), "high");
	assert.equal(sendThinking(null, "Balanced"), "");
});
