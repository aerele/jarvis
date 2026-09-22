import { describe, it, expect } from "vitest";
import { isShowCardRequest } from "./showCardRequest";

describe("isShowCardRequest (layered re-check phase 2 — typed 'show it' fast-path)", () => {
	// Whole-message equality: only these exact normalised phrases fire the client
	// re-surface. Everything else falls through to a normal send (the persona backstop
	// still handles looser phrasings server-side).
	const positives = [
		"show it",
		"show the card",
		"show the confirmation",
		"i can't see it",
		"i can't see the card",
		"i can't see the confirmation",
		"the confirmation didn't appear",
		"the card didn't appear",
	];

	it("matches each exact phrase", () => {
		for (const p of positives) expect(isShowCardRequest(p)).toBe(true);
	});

	it("normalises case, surrounding whitespace, trailing punctuation, and curly apostrophes", () => {
		expect(isShowCardRequest("Show It")).toBe(true);
		expect(isShowCardRequest("  show it  ")).toBe(true);
		expect(isShowCardRequest("show it.")).toBe(true);
		expect(isShowCardRequest("show it!!")).toBe(true);
		expect(isShowCardRequest("show   it")).toBe(true); // collapsed internal whitespace
		expect(isShowCardRequest("show it")).toBe(true); // non-breaking space collapses
		expect(isShowCardRequest("show it .")).toBe(true); // space before trailing punctuation
		expect(isShowCardRequest("I can’t see the card")).toBe(true); // curly apostrophe (mobile)
		expect(isShowCardRequest("The confirmation didn’t appear?")).toBe(true);
	});

	// The whole point of whole-message equality: never fire on a prefix of a real
	// request, a negation, an unrelated sentence, or pasted prose.
	const negatives = [
		"show me the sales orders",
		"can you show it",
		"show it to me later",
		"don't show it",
		"do not show it",
		"don’t show it", // negation with a curly apostrophe (mobile)
		"i can't see the chart",
		// one substring-superset per phrase: a substring/prefix regression would swallow these
		"the confirmation didn't appear in the report",
		"please show the confirmation now",
		"i can't see it anymore",
		"i can't see the card in the list",
		"i can't see the confirmation dialog",
		"the card didn't appear yet",
		"show", // partial
		"it", // partial
		"please show the card now", // extra words
		"show it show it show it", // repeated
		"", // empty
		"   ", // whitespace only
	];

	it("does NOT match prefixes, negations, extra words, or unrelated text", () => {
		for (const n of negatives) expect(isShowCardRequest(n)).toBe(false);
	});

	it("does NOT match pasted prose that merely contains a phrase", () => {
		expect(
			isShowCardRequest("Here is my report. Please show it to the finance team by Friday.")
		).toBe(false);
	});

	it("caps length so a long message can never match", () => {
		expect(isShowCardRequest("show it " + "x".repeat(60))).toBe(false);
	});

	it("is safe for non-string input", () => {
		expect(isShowCardRequest(null)).toBe(false);
		expect(isShowCardRequest(undefined)).toBe(false);
		expect(isShowCardRequest(42)).toBe(false);
		expect(isShowCardRequest(["show it"])).toBe(false);
	});
});
