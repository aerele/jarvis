import { describe, it, expect } from "vitest";
import {
	discardedTokens,
	isRecentCard,
	markCardsEarlier,
	typedApprovalHint,
} from "./typedCardReply";

describe("typedApprovalHint", () => {
	it("offers the bare go-ahead only for a card parked since the user last spoke", () => {
		expect(typedApprovalHint([{ token: "a", recent: true }])).toBe('or type "go ahead"');
		expect(typedApprovalHint([{ token: "a" }])).toBe('or type "go ahead"');
		expect(typedApprovalHint([{ token: "a", recent: false }])).toBe('or type "confirm 1"');
	});

	it("offers confirm all only when every card is recent", () => {
		const two = [{ token: "a" }, { token: "b" }];
		expect(typedApprovalHint(two)).toBe('or type "confirm all", or "confirm 1 and 2"');
		two[0].recent = false;
		expect(typedApprovalHint(two)).toBe('or type "confirm 1 and 2"');
	});

	it("is empty with no cards", () => {
		expect(typedApprovalHint([])).toBe("");
		expect(typedApprovalHint(null)).toBe("");
	});
});

describe("typed reply responses", () => {
	it("reads the discarded tokens and tolerates older servers", () => {
		const res = {
			typed_rejection: { discarded: [{ token: "a" }, { token: "b" }], skipped: [] },
		};
		expect([...discardedTokens(res)]).toEqual(["a", "b"]);
		expect(discardedTokens({ ok: true }).size).toBe(0);
		expect(discardedTokens(null).size).toBe(0);
	});

	it("ages only the cards of the conversation the user spoke in", () => {
		const cards = [
			{ token: "a", conversation: "c1", recent: true },
			{ token: "b", conversation: "c2", recent: true },
		];
		markCardsEarlier(cards, "c1");
		expect(cards.map(isRecentCard)).toEqual([false, true]);
	});
});
