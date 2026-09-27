// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";

const raiseHoldMock = vi.fn();
const recheckMock = vi.fn();
vi.mock("@/maintenanceGate", () => ({
	raiseHold: (...a) => raiseHoldMock(...a),
	recheck: (...a) => recheckMock(...a),
}));

import { onLlmSwitch, SWITCH_MESSAGE } from "./llmSwitch.js";

beforeEach(() => {
	raiseHoldMock.mockReset();
	recheckMock.mockReset();
});

describe("onLlmSwitch", () => {
	it('raises the hold with the switch copy on state "switching"', () => {
		onLlmSwitch({ state: "switching" });
		expect(raiseHoldMock).toHaveBeenCalledWith(SWITCH_MESSAGE);
		expect(recheckMock).not.toHaveBeenCalled();
	});

	it('rechecks the hold on state "done", ignoring the outcome', () => {
		onLlmSwitch({ state: "done", outcome: "moved" });
		expect(recheckMock).toHaveBeenCalledTimes(1);
		expect(raiseHoldMock).not.toHaveBeenCalled();
	});

	it("ignores an unknown state", () => {
		onLlmSwitch({ state: "something-else" });
		expect(raiseHoldMock).not.toHaveBeenCalled();
		expect(recheckMock).not.toHaveBeenCalled();
	});

	it("ignores a malformed payload (no state, undefined, null)", () => {
		onLlmSwitch({});
		onLlmSwitch(undefined);
		onLlmSwitch(null);
		expect(raiseHoldMock).not.toHaveBeenCalled();
		expect(recheckMock).not.toHaveBeenCalled();
	});
});
