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

const isAwaiting = (s) => s === "Replied" || s === "Resolved";

describe("supportAwaitingRoute", () => {
	it("routes to the list with the awaiting quick filter when count is not 1", () => {
		expect(supportAwaitingRoute(3, [], isAwaiting)).toEqual({
			name: "Support",
			query: { status: "awaiting" },
		});
	});

	it("routes straight to the single ticket when count is 1 and exactly one local row matches", () => {
		const tickets = [
			{ name: "T-1", status: "Open" },
			{ name: "T-2", status: "Replied" },
		];
		expect(supportAwaitingRoute(1, tickets, isAwaiting)).toEqual({
			name: "SupportTicket",
			params: { ticket: "T-2" },
		});
	});

	it("falls back to the list when count is 1 but the local ticket list has not loaded", () => {
		expect(supportAwaitingRoute(1, [], isAwaiting)).toEqual({
			name: "Support",
			query: { status: "awaiting" },
		});
	});

	it("falls back to the list when count is 1 but two local rows match (stale/mismatched cache)", () => {
		const tickets = [
			{ name: "T-1", status: "Resolved" },
			{ name: "T-2", status: "Replied" },
		];
		expect(supportAwaitingRoute(1, tickets, isAwaiting)).toEqual({
			name: "Support",
			query: { status: "awaiting" },
		});
	});
});
