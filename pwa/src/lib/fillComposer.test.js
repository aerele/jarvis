import { test } from "node:test";
import assert from "node:assert/strict";
import { pickStarterPrompt } from "./fillComposer.js";

test("pickStarterPrompt returns the prompt to FILL (never a send payload)", () => {
	assert.equal(
		pickStarterPrompt({ title: "Look up", prompt: "What is owed?" }),
		"What is owed?"
	);
});

test("pickStarterPrompt is defensive on junk", () => {
	assert.equal(pickStarterPrompt(null), "");
	assert.equal(pickStarterPrompt(undefined), "");
	assert.equal(pickStarterPrompt({ title: "x" }), "");
	assert.equal(pickStarterPrompt({ prompt: 42 }), "");
});
