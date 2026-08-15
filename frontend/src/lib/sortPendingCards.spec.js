import { describe, it, expect } from "vitest";
import { comparePendingCards, sortPendingCards } from "./sortPendingCards.js";

// Real behavioural tests for the card order a typed "confirm N" indexes into.
// This replaces a source-grep that passed even when a client dropped expires_at
// (the Desk-widget wrong-write bug): grepping the comparator text proves nothing
// if the data it runs on never carries the field.
describe("sortPendingCards", () => {
	it("orders by expires_at ascending, so the earliest-minted card is number 1", () => {
		const out = sortPendingCards([
			{ token: "z", expires_at: 200 },
			{ token: "a", expires_at: 100 },
		]);
		expect(out.map((c) => c.token)).toEqual(["a", "z"]);
	});

	it("tie-breaks equal expires_at by token in CODE-UNIT (byte) order, matching the server", () => {
		// 'A' (0x41) sorts before 'z' (0x7A) by code unit; a locale-aware compare
		// would disagree (case-folding 'a'/'A' together), which is the divergence
		// that renumbers a card between screen and server.
		const out = sortPendingCards([
			{ token: "z9", expires_at: 100 },
			{ token: "A0", expires_at: 100 },
		]);
		expect(out.map((c) => c.token)).toEqual(["A0", "z9"]);
	});

	it("treats a missing expires_at as 0 without throwing", () => {
		const out = sortPendingCards([{ token: "b", expires_at: 5 }, { token: "a" }]);
		expect(out.map((c) => c.token)).toEqual(["a", "b"]);
	});

	it("does not mutate its input", () => {
		const input = [
			{ token: "z", expires_at: 2 },
			{ token: "a", expires_at: 1 },
		];
		sortPendingCards(input);
		expect(input.map((c) => c.token)).toEqual(["z", "a"]);
	});

	it("comparePendingCards is a total order (transitive-safe: distinct tokens never tie)", () => {
		expect(
			comparePendingCards({ token: "a", expires_at: 1 }, { token: "b", expires_at: 1 })
		).toBeLessThan(0);
		expect(
			comparePendingCards({ token: "b", expires_at: 1 }, { token: "a", expires_at: 1 })
		).toBeGreaterThan(0);
		expect(
			comparePendingCards({ token: "a", expires_at: 1 }, { token: "a", expires_at: 1 })
		).toBe(0);
	});
});
