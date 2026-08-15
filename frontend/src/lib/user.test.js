import { test } from "node:test";
import assert from "node:assert/strict";
import { initialsOf } from "./user.js";

test("initialsOf: first letter of first two words, uppercased", () => {
	assert.equal(initialsOf("Ada Lovelace"), "AL");
	assert.equal(initialsOf("madonna"), "M");
});
test("initialsOf: falls back to U for empty/blank/null", () => {
	assert.equal(initialsOf(""), "U");
	assert.equal(initialsOf("   "), "U");
	assert.equal(initialsOf(null), "U");
});
test("initialsOf: keeps a whole emoji/astral glyph (no split surrogate pair)", () => {
	// regression for review #13: w[0] would return a lone surrogate half (�)
	assert.equal(initialsOf("🎉 Party"), "🎉P");
	assert.equal(initialsOf("🎉"), "🎉");
});
