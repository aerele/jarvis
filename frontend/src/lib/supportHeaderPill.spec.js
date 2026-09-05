import { describe, it, expect } from "vitest";
import {
	supportPillLabel,
	supportAwaitingPhrase,
	supportAwaitingRoute,
} from "./supportHeaderPill";

describe("supportPillLabel", () => {
	it("is blank at zero - the pill is hidden, not '0'", () => {
		expect(supportPillLabel(0)).toBe("");
	});

	it("shows the exact count for a single digit", () => {
		expect(supportPillLabel(3)).toBe("3");
	});

	it("caps at 9+ once double digits would blow past the 17px pill", () => {
		expect(supportPillLabel(12)).toBe("9+");
		expect(supportPillLabel(10)).toBe("9+");
	});
});

describe("supportAwaitingPhrase", () => {
	it("uses singular ticket for exactly one", () => {
		expect(supportAwaitingPhrase(1)).toBe("1 ticket awaiting your reply");
	});

	it("uses plural tickets for zero and for more than one", () => {
		expect(supportAwaitingPhrase(0)).toBe("0 tickets awaiting your reply");
		expect(supportAwaitingPhrase(3)).toBe("3 tickets awaiting your reply");
	});
});

describe("supportAwaitingRoute", () => {
	// A shortcut here once routed straight to a single ticket's thread when
	// the awaiting_count total was 1 and the store's already-loaded ticket
	// list agreed. Dropped (hard review finding): awaitingCount is polled
	// every 60s while store.tickets is only populated by an actual visit to a
	// Support page and never re-fetched here, so the two numbers can drift
	// and the "confident" match could route to a ticket that is no longer the
	// one awaiting a reply. The list route is now the only route, always.
	it("always routes to the list with the awaiting quick filter", () => {
		expect(supportAwaitingRoute()).toEqual({
			name: "Support",
			query: { status: "awaiting" },
		});
	});
});
