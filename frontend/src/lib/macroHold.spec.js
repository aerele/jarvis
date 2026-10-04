import { describe, it, expect } from "vitest";
import { macroHold, holdMessage } from "./macroHold";

describe("macroHold", () => {
	it("is null for a macro that is not held, or a payload without the fields", () => {
		expect(macroHold({ admin_hold: 0, admin_hold_reason: "" })).toBeNull();
		expect(macroHold({ name: "m1" })).toBeNull();
		expect(macroHold(null)).toBeNull();
	});

	it("carries the reason of a held macro", () => {
		expect(macroHold({ admin_hold: 1, admin_hold_reason: "  Too many emails " })).toEqual({
			reason: "Too many emails",
		});
	});
});

describe("holdMessage", () => {
	it("says who, why and until when, in the server's words", () => {
		expect(holdMessage({ reason: "Sends too many emails" })).toBe(
			"An admin has put this macro on hold: Sends too many emails. It will not run until an admin releases it."
		);
	});

	it("does not double the reason's own full stop", () => {
		expect(holdMessage({ reason: "Paused." })).toBe(
			"An admin has put this macro on hold: Paused. It will not run until an admin releases it."
		);
	});

	it("still says it without a reason, and says nothing when not held", () => {
		expect(holdMessage({ reason: "" })).toBe(
			"An admin has put this macro on hold. It will not run until an admin releases it."
		);
		expect(holdMessage(null)).toBe("");
	});
});
