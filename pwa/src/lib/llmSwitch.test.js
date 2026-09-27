import { test } from "node:test";
import assert from "node:assert/strict";
import { makeOnLlmSwitch, SWITCH_MESSAGE } from "../llmSwitch.js";

// llmSwitch.js takes no import of maintenanceGate.js (it pulls in vue + frappe-ui,
// which node --test cannot load), so the handler is built from a fake gate here
// instead of mocking a "../maintenanceGate.js" import.
function fakeGate() {
	const calls = { raiseHold: [], recheck: 0 };
	return {
		calls,
		raiseHold: (msg) => calls.raiseHold.push(msg),
		recheck: () => {
			calls.recheck += 1;
		},
	};
}

test('raises the hold with the switch copy on state "switching"', () => {
	const gate = fakeGate();
	makeOnLlmSwitch(gate)({ state: "switching" });
	assert.deepEqual(gate.calls.raiseHold, [SWITCH_MESSAGE]);
	assert.equal(gate.calls.recheck, 0);
});

test('rechecks the hold on state "done", ignoring the outcome', () => {
	const gate = fakeGate();
	makeOnLlmSwitch(gate)({ state: "done", outcome: "moved" });
	assert.equal(gate.calls.recheck, 1);
	assert.deepEqual(gate.calls.raiseHold, []);
});

test("ignores an unknown state", () => {
	const gate = fakeGate();
	makeOnLlmSwitch(gate)({ state: "something-else" });
	assert.deepEqual(gate.calls.raiseHold, []);
	assert.equal(gate.calls.recheck, 0);
});

test("ignores a malformed payload (no state, undefined, null)", () => {
	const gate = fakeGate();
	const onLlmSwitch = makeOnLlmSwitch(gate);
	onLlmSwitch({});
	onLlmSwitch(undefined);
	onLlmSwitch(null);
	assert.deepEqual(gate.calls.raiseHold, []);
	assert.equal(gate.calls.recheck, 0);
});
