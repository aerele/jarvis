import { describe, it, expect } from "vitest";
import { isPermissionDenied, proposalHeadline, proposalExcerpt, dropperLabel } from "./wikiReview";

describe("wikiReview helpers", () => {
	it("treats a 403 / PermissionError as not-a-reviewer (panel hides)", () => {
		expect(isPermissionDenied({ status: 403 })).toBe(true);
		expect(isPermissionDenied({ exc_type: "PermissionError" })).toBe(true);
		expect(isPermissionDenied({ status: 500 })).toBe(false);
		expect(isPermissionDenied(null)).toBe(false);
	});

	it("headlines a proposal by slug/title + page type", () => {
		expect(proposalHeadline({ preview: { slug: "party-acme", page_type: "Reference" } })).toBe(
			"party-acme · Reference"
		);
		expect(proposalHeadline({ preview: { title: "Acme" } })).toBe("Acme");
		expect(proposalHeadline({ title: "row title" })).toBe("row title");
		expect(proposalHeadline({})).toBe("wiki page");
	});

	it("excerpts the append body and ellipsizes past the limit", () => {
		expect(proposalExcerpt({ preview: { append_md: "  short  " } })).toBe("short");
		const long = "x".repeat(500);
		const out = proposalExcerpt({ preview: { append_md: long } }, 400);
		expect(out.endsWith("…")).toBe(true);
		expect(out.length).toBeLessThanOrEqual(401);
		expect(proposalExcerpt({})).toBe("");
	});

	it("labels the dropper with a name preference and a safe fallback", () => {
		expect(dropperLabel({ dropper_name: "Alice", dropper: "a@x.com" })).toBe("Alice");
		expect(dropperLabel({ dropper: "a@x.com" })).toBe("a@x.com");
		expect(dropperLabel({})).toBe("unknown");
	});
});
