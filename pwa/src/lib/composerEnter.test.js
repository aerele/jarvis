import { test } from "node:test";
import assert from "node:assert/strict";
import { enterSends } from "./composerEnter.js";

const DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15";
const PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148";
const ANDROID = "Mozilla/5.0 (Linux; Android 14) Chrome/120 Mobile";
const enter = (over = {}) => ({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 13, ...over });

test("plain Enter on a desktop keyboard sends", () => {
	assert.equal(enterSends(enter(), DESKTOP), true);
});

test("Enter with isComposing does not send", () => {
	assert.equal(enterSends(enter({ isComposing: true }), DESKTOP), false);
});

test("Enter with keyCode 229 does not send (Safari confirm after compositionend)", () => {
	assert.equal(enterSends(enter({ keyCode: 229, isComposing: false }), DESKTOP), false);
});

test("Tamil/Hindi sequence: compose, confirm with Enter, then a later Enter sends once", () => {
	const events = [
		enter({ key: "a", isComposing: true, keyCode: 229 }),
		enter({ isComposing: true, keyCode: 229 }), // confirms the candidate
		enter({ isComposing: false, keyCode: 229 }), // Safari: compositionend already fired
		enter(), // explicit Enter
	];
	assert.deepEqual(
		events.map((e) => enterSends(e, DESKTOP)),
		[false, false, false, true]
	);
});

test("Shift+Enter is a newline, not a send", () => {
	assert.equal(enterSends(enter({ shiftKey: true }), DESKTOP), false);
});

test("phone soft keyboard: Enter never sends", () => {
	assert.equal(enterSends(enter(), PHONE), false);
	assert.equal(enterSends(enter(), ANDROID), false);
});

test("non-Enter keys never send", () => {
	assert.equal(enterSends(enter({ key: "a", keyCode: 65 }), DESKTOP), false);
});
