import { test } from "node:test";
import assert from "node:assert/strict";
import { makeOnLlmSwitch, SWITCH_MESSAGE } from "../llmSwitch.js";

// llmSwitch.js takes no import of maintenanceGate.js (it pulls in vue + frappe-ui,
// which node --test cannot load), so the handler is built from a fake gate here
// instead of mocking a "../maintenanceGate.js" import.
function fakeGate() {
	const calls = { raiseHold: [], clearHold: 0, recheck: 0, order: [] };
	return {
		calls,
		raiseHold: (msg) => calls.raiseHold.push(msg),
		clearHold: () => {
			calls.clearHold += 1;
			calls.order.push("clearHold");
		},
		recheck: () => {
			calls.recheck += 1;
			calls.order.push("recheck");
		},
	};
}

test('raises the hold with the switch copy on state "switching"', () => {
	const gate = fakeGate();
	makeOnLlmSwitch(gate)({ state: "switching" });
	assert.deepEqual(gate.calls.raiseHold, [SWITCH_MESSAGE]);
	assert.equal(gate.calls.clearHold, 0);
	assert.equal(gate.calls.recheck, 0);
});

test('on state "done", clears the hold BEFORE rechecking (ignoring the outcome) - so the strip never waits on the 60s idle poll', () => {
	const gate = fakeGate();
	makeOnLlmSwitch(gate)({ state: "done", outcome: "moved" });
	assert.deepEqual(gate.calls.order, ["clearHold", "recheck"]);
	assert.deepEqual(gate.calls.raiseHold, []);
});

test("ignores an unknown state", () => {
	const gate = fakeGate();
	makeOnLlmSwitch(gate)({ state: "something-else" });
	assert.deepEqual(gate.calls.raiseHold, []);
	assert.equal(gate.calls.clearHold, 0);
	assert.equal(gate.calls.recheck, 0);
});

test("ignores a malformed payload (no state, undefined, null)", () => {
	const gate = fakeGate();
	const onLlmSwitch = makeOnLlmSwitch(gate);
	onLlmSwitch({});
	onLlmSwitch(undefined);
	onLlmSwitch(null);
	assert.deepEqual(gate.calls.raiseHold, []);
	assert.equal(gate.calls.clearHold, 0);
	assert.equal(gate.calls.recheck, 0);
});
