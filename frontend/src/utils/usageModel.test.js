import { test } from "node:test";
import assert from "node:assert/strict";
import { POOL_VIRTUAL_MODEL, modelDisplayLabel } from "./usageModel.js";

test("POOL_VIRTUAL_MODEL matches the bench sentinel (jarvis.chat.turn_handler.POOL_VIRTUAL_MODEL)", () => {
	assert.equal(POOL_VIRTUAL_MODEL, "jarvis-pool");
});

test("modelDisplayLabel: maps the pool auto-routed sentinel to a human label", () => {
	assert.equal(modelDisplayLabel(POOL_VIRTUAL_MODEL), "Pool (auto-routed)");
});

test("modelDisplayLabel: any other model id renders verbatim", () => {
	assert.equal(modelDisplayLabel("gpt-4o"), "gpt-4o");
	assert.equal(modelDisplayLabel("claude-sonnet"), "claude-sonnet");
});

test("modelDisplayLabel: falsy input passes through unchanged", () => {
	assert.equal(modelDisplayLabel(""), "");
	assert.equal(modelDisplayLabel(null), null);
	assert.equal(modelDisplayLabel(undefined), undefined);
});
