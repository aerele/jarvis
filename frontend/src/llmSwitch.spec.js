// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";

const raiseHoldMock = vi.fn();
const clearHoldMock = vi.fn();
const recheckMock = vi.fn();
vi.mock("@/maintenanceGate", () => ({
	raiseHold: (...a) => raiseHoldMock(...a),
	clearHold: (...a) => clearHoldMock(...a),
	recheck: (...a) => recheckMock(...a),
}));

import { onLlmSwitch, SWITCH_MESSAGE } from "./llmSwitch.js";

beforeEach(() => {
	raiseHoldMock.mockReset();
	clearHoldMock.mockReset();
	recheckMock.mockReset();
});

describe("onLlmSwitch", () => {
	it('raises the hold with the switch copy on state "switching"', () => {
		onLlmSwitch({ state: "switching" });
		expect(raiseHoldMock).toHaveBeenCalledWith(SWITCH_MESSAGE);
		expect(clearHoldMock).not.toHaveBeenCalled();
		expect(recheckMock).not.toHaveBeenCalled();
	});

	it('on state "done", clears the hold BEFORE rechecking (ignoring the outcome) - so the banner never waits on the 60s idle poll', () => {
		const order = [];
		clearHoldMock.mockImplementation(() => order.push("clearHold"));
		recheckMock.mockImplementation(() => order.push("recheck"));
		onLlmSwitch({ state: "done", outcome: "moved" });
		expect(order).toEqual(["clearHold", "recheck"]);
		expect(raiseHoldMock).not.toHaveBeenCalled();
	});

	it("ignores an unknown state", () => {
		onLlmSwitch({ state: "something-else" });
		expect(raiseHoldMock).not.toHaveBeenCalled();
		expect(clearHoldMock).not.toHaveBeenCalled();
		expect(recheckMock).not.toHaveBeenCalled();
	});

	it("ignores a malformed payload (no state, undefined, null)", () => {
		onLlmSwitch({});
		onLlmSwitch(undefined);
		onLlmSwitch(null);
		expect(raiseHoldMock).not.toHaveBeenCalled();
		expect(clearHoldMock).not.toHaveBeenCalled();
		expect(recheckMock).not.toHaveBeenCalled();
	});
});
