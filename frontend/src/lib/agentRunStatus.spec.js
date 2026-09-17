import { describe, it, expect } from "vitest";
import { STATUS_THEME, runReason, coverageWarned } from "./agentRunStatus";

describe("STATUS_THEME", () => {
	it("covers every Jarvis Agent Run status with the app's theme colours", () => {
		expect(STATUS_THEME).toEqual({
			running: "blue",
			completed: "green",
			partial: "orange",
			failed: "red",
			stopped: "gray",
		});
	});
});

// jarvis#1062 P1-7 (production-readiness audit): failed vs stopped must be
// distinguishable in the runs rail without opening the run.
describe("runReason", () => {
	it("returns the recorded error for a failed run, verbatim if 60 chars or under", () => {
		expect(runReason({ status: "failed", error: "LLM timed out." })).toBe("LLM timed out.");
	});

	it("truncates a longer error to 60 chars with an ellipsis", () => {
		const long = "A".repeat(80);
		const out = runReason({ status: "failed", error: long });
		expect(out).toBe("A".repeat(60) + "…");
		expect(out.length).toBe(61);
	});

	it("falls back to a short generic message for a failed run with no recorded error", () => {
		expect(runReason({ status: "failed", error: "" })).toBe("This run failed.");
		expect(runReason({ status: "failed" })).toBe("This run failed.");
	});

	it("is always the fixed operator message for a stopped run, ignoring any error text", () => {
		expect(runReason({ status: "stopped" })).toBe("Stopped by operator.");
		expect(runReason({ status: "stopped", error: "whatever" })).toBe("Stopped by operator.");
	});

	it("is empty for every other status", () => {
		expect(runReason({ status: "running" })).toBe("");
		expect(runReason({ status: "completed" })).toBe("");
		expect(runReason({ status: "partial" })).toBe("");
	});

	it("handles a missing row without throwing", () => {
		expect(runReason(null)).toBe("");
		expect(runReason(undefined)).toBe("");
	});
});

// A data-gap run reads status "completed" but must still show the "not clean" cue -
// the signal is the coverage_note, not the status, so board + panel agree.
describe("coverageWarned", () => {
	it("is true for a completed run that carries a coverage_note (the data-gap case)", () => {
		expect(
			coverageWarned({ status: "completed", coverage_note: "not evaluable: 2B stale" })
		).toBe(true);
	});

	it("is true for an execution-partial run (which always carries a coverage_note)", () => {
		expect(coverageWarned({ status: "partial", coverage_note: "scan truncated" })).toBe(true);
	});

	it("is false for a clean completed run with no coverage_note", () => {
		expect(coverageWarned({ status: "completed", coverage_note: "" })).toBe(false);
		expect(coverageWarned({ status: "completed" })).toBe(false);
	});

	it("is false for a failed run even with a coverage_note (its red banner owns the row)", () => {
		expect(coverageWarned({ status: "failed", coverage_note: "whatever" })).toBe(false);
	});

	it("handles a missing row without throwing", () => {
		expect(coverageWarned(null)).toBe(false);
		expect(coverageWarned(undefined)).toBe(false);
	});
});
