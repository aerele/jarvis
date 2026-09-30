import { describe, it, expect } from "vitest";
import { autoModeView, AUTO_MODE_COPY } from "./autoMode.js";

describe("autoModeView", () => {
	it("locks on when the conversation is in auto mode, whatever else is set", () => {
		const locked = { visible: true, on: true, locked: true };
		expect(autoModeView({ messageCount: 0, convAutoMode: 1, armed: false })).toEqual(locked);
		expect(autoModeView({ messageCount: 5, convAutoMode: 1, armed: false })).toEqual(locked);
		expect(autoModeView({ messageCount: 5, convAutoMode: true, armed: true })).toEqual(locked);
	});

	it("is an editable toggle on an empty chat, following armed", () => {
		expect(autoModeView({ messageCount: 0, convAutoMode: 0, armed: false })).toEqual({
			visible: true,
			on: false,
			locked: false,
		});
		expect(autoModeView({ messageCount: 0, convAutoMode: 0, armed: true })).toEqual({
			visible: true,
			on: true,
			locked: false,
		});
	});

	it("hides once messages exist and auto mode is off, ignoring armed", () => {
		const hidden = { visible: false, on: false, locked: false };
		expect(autoModeView({ messageCount: 1, convAutoMode: 0, armed: false })).toEqual(hidden);
		expect(autoModeView({ messageCount: 3, convAutoMode: 0, armed: true })).toEqual(hidden);
	});

	it("uses no em or en dashes in the shared copy", () => {
		for (const v of Object.values(AUTO_MODE_COPY)) expect(v).not.toMatch(/[–—]/);
	});
});
