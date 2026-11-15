import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import { liveDraftRow, isDraftRow, MAX_USER_ROWS_AFTER_DRAFT } from "./liveDraft.js";

const CASES = JSON.parse(fs.readFileSync(path.resolve(__dirname, "liveDraftCases.json"), "utf8"));

describe("liveDraftRow shared cases", () => {
	it("has cases", () => {
		expect(CASES.length).toBeGreaterThan(0);
		expect(MAX_USER_ROWS_AFTER_DRAFT).toBe(5);
	});
	for (const c of CASES) {
		it(`${c.id}: ${c.note}`, () => {
			expect(liveDraftRow(c.rows)?.name ?? null).toBe(c.live);
		});
	}
});

describe("isDraftRow", () => {
	const row = (c) => CASES.find((x) => x.id === c).rows[1];
	it("is false for an email block and a gated verb block", () => {
		expect(isDraftRow(row("draft-then-email-block"))).toBe(false);
		expect(isDraftRow(row("draft-then-gated-verb-block"))).toBe(false);
	});
	it("is true for a create block", () => {
		expect(isDraftRow(row("draft-then-chat"))).toBe(true);
	});
});

describe("liveDraftRow input safety", () => {
	it("returns null for an empty or null list", () => {
		expect(liveDraftRow([])).toBeNull();
		expect(liveDraftRow(null)).toBeNull();
	});
});
