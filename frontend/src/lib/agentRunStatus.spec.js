import { describe, it, expect } from "vitest";
import { STATUS_THEME, runReason } from "./agentRunStatus";

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
