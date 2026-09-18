import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_STARTERS, starterTint, normalizeStarters } from "./starters.js";

test("DEFAULT_STARTERS is a non-empty {title,prompt} list", () => {
	assert.ok(DEFAULT_STARTERS.length >= 3);
	for (const s of DEFAULT_STARTERS) {
		assert.equal(typeof s.title, "string");
		assert.equal(typeof s.prompt, "string");
	}
});

test("starterTint cycles over exactly 3 tints", () => {
	const tints = [0, 1, 2, 3, 4].map(starterTint);
	assert.equal(new Set(tints).size, 3);
	assert.equal(tints[0], tints[3]); // wraps at 3
});

test("normalizeStarters coerces junk to the default list", () => {
	assert.deepEqual(normalizeStarters(null), DEFAULT_STARTERS);
	assert.deepEqual(normalizeStarters([]), DEFAULT_STARTERS);
	assert.deepEqual(normalizeStarters("nope"), DEFAULT_STARTERS);
	const rows = [
		{ title: "A", prompt: "a" },
		{ title: "B", prompt: "b", extra: 1 },
		{ title: "bad-no-prompt" },
	];
	assert.deepEqual(normalizeStarters(rows), [
		{ title: "A", prompt: "a" },
		{ title: "B", prompt: "b" },
	]);
});
