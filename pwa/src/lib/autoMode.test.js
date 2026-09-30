import { test } from "node:test";
import assert from "node:assert/strict";
import { autoModeView } from "./autoMode.js";

test("autoModeView: a chat with auto mode is locked on, whatever else is true", () => {
	const locked = { visible: true, on: true, locked: true };
	assert.deepEqual(autoModeView({ messageCount: 0, convAutoMode: 1, armed: false }), locked);
	assert.deepEqual(autoModeView({ messageCount: 5, convAutoMode: 1, armed: false }), locked);
	assert.deepEqual(autoModeView({ messageCount: 3, convAutoMode: true, armed: true }), locked);
});

test("autoModeView: an empty chat shows the toggle, on only when armed", () => {
	assert.deepEqual(autoModeView({ messageCount: 0, convAutoMode: 0, armed: false }), {
		visible: true,
		on: false,
		locked: false,
	});
	assert.deepEqual(autoModeView({ messageCount: 0, convAutoMode: 0, armed: true }), {
		visible: true,
		on: true,
		locked: false,
	});
});

test("autoModeView: armed is ignored once the chat has messages", () => {
	const hidden = { visible: false, on: false, locked: false };
	assert.deepEqual(autoModeView({ messageCount: 1, convAutoMode: 0, armed: true }), hidden);
	assert.deepEqual(
		autoModeView({ messageCount: 4, convAutoMode: undefined, armed: false }),
		hidden
	);
});
