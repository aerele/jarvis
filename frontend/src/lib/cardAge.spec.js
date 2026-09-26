import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import { PROPOSED_LABEL_AFTER_S, proposedLabel } from "./cardAge";

const NOW = 1_800_000_000_000;
const ago = (s) => NOW / 1000 - s;

describe("proposedLabel", () => {
	it("stays quiet for the first hour", () => {
		expect(PROPOSED_LABEL_AFTER_S).toBe(3600);
		expect(proposedLabel(ago(0), NOW)).toBe("");
		expect(proposedLabel(ago(3599), NOW)).toBe("");
	});

	it("names the age in hours, then days", () => {
		expect(proposedLabel(ago(3600), NOW)).toBe("Proposed 1h ago");
		expect(proposedLabel(ago(5 * 3600 + 59), NOW)).toBe("Proposed 5h ago");
		expect(proposedLabel(ago(23 * 3600 + 3599), NOW)).toBe("Proposed 23h ago");
		expect(proposedLabel(ago(49 * 3600), NOW)).toBe("Proposed 2d ago");
	});

	it("says nothing without a usable time", () => {
		for (const bad of [null, undefined, 0, "", "soon", NaN])
			expect(proposedLabel(bad, NOW)).toBe("");
		// A client clock behind the server's: the card is not old.
		expect(proposedLabel(ago(-7200), NOW)).toBe("");
	});
});

describe("ChatView card age", () => {
	it("never parses a row's site-timezone creation as a local time", () => {
		const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
		expect(src).not.toContain("_rowExpiresEpoch(m.creation)");
		expect(src).toContain("created_at: m.created_at ?? null,");
	});
});
