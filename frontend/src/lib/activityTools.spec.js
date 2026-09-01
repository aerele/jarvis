import { describe, it, expect } from "vitest";
import { shouldHideActivityTool } from "./activityTools.js";

// A role=tool receipt row as it reaches activityByAssistant.
const row = (o) => ({ role: "tool", ...o });

describe("shouldHideActivityTool", () => {
	it("hides a completed built-in with no input/output (read/exec/bash/canvas)", () => {
		for (const name of ["read", "exec", "bash", "canvas"]) {
			expect(
				shouldHideActivityTool(row({ tool_name: name, tool_status: "completed" }))
			).toBe(true);
		}
	});

	it("hides a still-running built-in with no I/O (never shown; the live indicator covers it)", () => {
		expect(shouldHideActivityTool(row({ tool_name: "read", tool_status: "running" }))).toBe(
			true
		);
	});

	it("keeps a FAILED built-in so the failure surfaces — any status that is not completed/running", () => {
		for (const status of ["error", "failed", "cancelled"]) {
			expect(shouldHideActivityTool(row({ tool_name: "exec", tool_status: status }))).toBe(
				false
			);
		}
	});

	it("keeps a built-in with ambiguous (null/undefined) status — errs toward visible", () => {
		expect(shouldHideActivityTool(row({ tool_name: "exec", tool_status: null }))).toBe(false);
		expect(shouldHideActivityTool(row({ tool_name: "exec" }))).toBe(false);
	});

	it("never hides a jarvis__* platform tool, even with empty I/O", () => {
		expect(
			shouldHideActivityTool(
				row({
					tool_name: "jarvis__find_skills",
					tool_status: "completed",
					tool_result: '{"ok":true}',
				})
			)
		).toBe(false);
		expect(
			shouldHideActivityTool(
				row({ tool_name: "jarvis__read_wiki", tool_status: "completed" })
			)
		).toBe(false);
	});

	it("keeps a built-in that captured input OR output", () => {
		expect(
			shouldHideActivityTool(
				row({ tool_name: "exec", tool_status: "completed", tool_args: '{"cmd":"ls"}' })
			)
		).toBe(false);
		expect(
			shouldHideActivityTool(
				row({ tool_name: "read", tool_status: "completed", tool_result: '{"bytes":10}' })
			)
		).toBe(false);
	});

	it("treats empty-string args/result as no I/O", () => {
		expect(
			shouldHideActivityTool(
				row({
					tool_name: "read",
					tool_status: "completed",
					tool_args: "",
					tool_result: "",
				})
			)
		).toBe(true);
	});

	it("returns false for a non-tool row (only tool rows are filtered)", () => {
		expect(shouldHideActivityTool({ role: "assistant", tool_name: "read" })).toBe(false);
		expect(shouldHideActivityTool(null)).toBe(false);
	});
});
