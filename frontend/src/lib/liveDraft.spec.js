import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import {
	liveDraftRow,
	isDraftRow,
	retiresDraft,
	MAX_USER_ROWS_AFTER_DRAFT,
	MAX_ROWS_AFTER_DRAFT,
} from "./liveDraft.js";

const CASES = JSON.parse(fs.readFileSync(path.resolve(__dirname, "liveDraftCases.json"), "utf8"));

describe("liveDraftRow shared cases", () => {
	it("has cases", () => {
		expect(CASES.length).toBeGreaterThan(0);
		expect(MAX_USER_ROWS_AFTER_DRAFT).toBe(5);
		expect(MAX_ROWS_AFTER_DRAFT).toBe(60);
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

describe("retiresDraft", () => {
	it("is a receipt, or a create / update call that completed", () => {
		expect(retiresDraft({ role: "tool", action_outcome: "discarded" })).toBe(true);
		for (const tool_name of ["create_doc", "update_doc"])
			expect(retiresDraft({ role: "tool", tool_name, tool_status: "completed" })).toBe(true);
	});
	it("is not a read, a parked card, a failed call or a padded outcome", () => {
		for (const row of [
			{ role: "tool", action_outcome: "  " },
			{ role: "tool", tool_name: "create_doc", tool_status: "pending" },
			{ role: "tool", tool_name: "create_doc", tool_status: "error" },
			{ role: "tool", tool_name: "get_doc", tool_status: "completed" },
			{ role: "assistant", action_outcome: "confirmed" },
			null,
		])
			expect(retiresDraft(row)).toBe(false);
	});
});

describe("liveDraftRow input safety", () => {
	it("returns null for an empty or null list", () => {
		expect(liveDraftRow([])).toBeNull();
		expect(liveDraftRow(null)).toBeNull();
	});
});
