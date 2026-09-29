import { describe, it, expect } from "vitest";

import { preConnectStatusLabel } from "./statusPhrase";

describe("preConnectStatusLabel", () => {
	it("renders the one-time device (re)pair state (agent 9.3 connect-first)", () => {
		expect(preConnectStatusLabel("pairing")).toBe("Setting up your assistant…");
	});

	it("returns null for every other phase so tool/thinking phrases win", () => {
		// "waking" is the steps box's "Preparing your session" (liveTurn.SESSION_PREP).
		expect(preConnectStatusLabel("waking")).toBeNull();
		expect(preConnectStatusLabel("model")).toBeNull();
		expect(preConnectStatusLabel("analyzing")).toBeNull();
		expect(preConnectStatusLabel("compacting")).toBeNull();
		expect(preConnectStatusLabel(null)).toBeNull();
		expect(preConnectStatusLabel(undefined)).toBeNull();
		expect(preConnectStatusLabel("")).toBeNull();
	});
});
