// Behavioural tests for the post-reply feedback throttle. jsdom gives us a real
// localStorage; Math.random is stubbed so the ~1-in-3 dice are deterministic.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FEEDBACK_MIN_MS, markIgnored, markRated, shouldOfferFeedback } from "./feedbackGate";

const LONG = FEEDBACK_MIN_MS + 5000;

function passDice() {
	vi.spyOn(Math, "random").mockReturnValue(0.05); // < 1/3
}
function failDice() {
	vi.spyOn(Math, "random").mockReturnValue(0.99); // >= 1/3
}

beforeEach(() => {
	localStorage.clear();
});
afterEach(() => {
	vi.restoreAllMocks();
});

describe("feedbackGate", () => {
	it("never shows on a reply shorter than the threshold", () => {
		passDice();
		expect(shouldOfferFeedback(FEEDBACK_MIN_MS - 1)).toBe(false);
	});

	it("shows on the first long reply when the dice pass", () => {
		passDice();
		expect(shouldOfferFeedback(LONG)).toBe(true);
	});

	it("stays hidden when the ~1-in-3 dice fail", () => {
		failDice();
		expect(shouldOfferFeedback(LONG)).toBe(false);
	});

	it("enforces a cooldown of several replies between asks", () => {
		passDice();
		expect(shouldOfferFeedback(LONG)).toBe(true); // shown at reply #1
		for (let i = 0; i < 4; i++) expect(shouldOfferFeedback(LONG)).toBe(false); // within cooldown
		expect(shouldOfferFeedback(LONG)).toBe(true); // cooldown elapsed -> 2nd show
	});

	it("caps at 2 shows per session", () => {
		passDice();
		let shows = 0;
		for (let i = 0; i < 40; i++) if (shouldOfferFeedback(LONG)) shows++;
		expect(shows).toBe(2);
	});

	it("stops asking once the user rates", () => {
		passDice();
		expect(shouldOfferFeedback(LONG)).toBe(true);
		markRated();
		for (let i = 0; i < 20; i++) expect(shouldOfferFeedback(LONG)).toBe(false);
	});

	it("backs off after two ignores", () => {
		passDice();
		expect(shouldOfferFeedback(LONG)).toBe(true); // show #1
		markIgnored();
		for (let i = 0; i < 4; i++) shouldOfferFeedback(LONG); // ride out the cooldown
		expect(shouldOfferFeedback(LONG)).toBe(true); // show #2
		markIgnored(); // second ignore -> stopped
		for (let i = 0; i < 20; i++) expect(shouldOfferFeedback(LONG)).toBe(false);
	});
});
