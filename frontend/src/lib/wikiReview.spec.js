import { describe, it, expect } from "vitest";
import {
	isPermissionDenied,
	proposalHeadline,
	proposalBody,
	isLongBody,
	dropperLabel,
	landingMessage,
} from "./wikiReview";

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

	it("returns the FULL trimmed body (never truncated — the reviewer sees everything)", () => {
		expect(proposalBody({ preview: { append_md: "  short  " } })).toBe("short");
		const long = "x".repeat(5000);
		// The whole body comes back untouched — no ellipsis, no length cap.
		expect(proposalBody({ preview: { append_md: long } })).toBe(long);
		expect(proposalBody({})).toBe("");
	});

	it("flags a long body so the panel nudges the reviewer to scroll", () => {
		expect(isLongBody({ preview: { append_md: "x".repeat(700) } }, 600)).toBe(true);
		expect(isLongBody({ preview: { append_md: "x".repeat(100) } }, 600)).toBe(false);
		expect(isLongBody({})).toBe(false);
	});

	it("labels the dropper with a name preference and a safe fallback", () => {
		expect(dropperLabel({ dropper_name: "Alice", dropper: "a@x.com" })).toBe("Alice");
		expect(dropperLabel({ dropper: "a@x.com" })).toBe("a@x.com");
		expect(dropperLabel({})).toBe("unknown");
	});

	it("maps a known apply_reason code to human copy", () => {
		expect(landingMessage({ apply_reason: "page_full" })).toBe(
			"The wiki page is full, so nothing was changed. Retry will keep failing until the page has room: record the note on another page, or reject it."
		);
	});

	it("shows an unmapped apply_reason code as before (raw, in parens)", () => {
		expect(landingMessage({ apply_reason: "busy" })).toBe(
			"Approved, but the write did not land (busy). Retry to re-drive it."
		);
		expect(landingMessage({ apply_reason: null })).toBe(
			"Approved, but the write did not land. Retry to re-drive it."
		);
		expect(landingMessage({})).toBe(
			"Approved, but the write did not land. Retry to re-drive it."
		);
	});
});
