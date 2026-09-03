import { describe, it, expect } from "vitest";
import { isCustomerFacingTool, shouldHideActivityTool } from "./activityTools.js";

const row = (o) => ({ role: "tool", ...o });

describe("isCustomerFacingTool (live event names — prefixed)", () => {
	it("is true only for a jarvis__-prefixed name", () => {
		expect(isCustomerFacingTool("jarvis__find_skills")).toBe(true);
		expect(isCustomerFacingTool("jarvis__read_wiki")).toBe(true);
	});

	it("is false for built-ins and for missing names", () => {
		for (const name of ["read", "exec", "bash", "canvas", "", null, undefined]) {
			expect(isCustomerFacingTool(name)).toBe(false);
		}
	});
});

describe("shouldHideActivityTool (settled rows — keyed on I/O)", () => {
	it("hides an internal built-in (no args, no result) — any status incl. failure", () => {
		for (const tool_status of ["completed", "running", "error", "failed", null, undefined]) {
			expect(shouldHideActivityTool(row({ tool_name: "read", tool_status }))).toBe(true);
		}
	});

	it("keeps a jarvis tool that captured a result (persisted name has no prefix)", () => {
		expect(
			shouldHideActivityTool(row({ tool_name: "find_skills", tool_result: '{"ok":true}' }))
		).toBe(false);
		expect(
			shouldHideActivityTool(row({ tool_name: "read_wiki", tool_args: '{"q":"x"}' }))
		).toBe(false);
	});

	it("keeps any row that captured args OR result", () => {
		expect(shouldHideActivityTool(row({ tool_name: "x", tool_args: '{"a":1}' }))).toBe(false);
		expect(shouldHideActivityTool(row({ tool_name: "x", tool_result: "[]" }))).toBe(false);
	});

	it("treats empty-string args/result as no I/O (hidden)", () => {
		expect(
			shouldHideActivityTool(row({ tool_name: "read", tool_args: "", tool_result: "" }))
		).toBe(true);
	});

	it("returns false for a non-tool row (only tool rows are filtered)", () => {
		expect(shouldHideActivityTool({ role: "assistant", tool_name: "read" })).toBe(false);
		expect(shouldHideActivityTool(null)).toBe(false);
	});
});
