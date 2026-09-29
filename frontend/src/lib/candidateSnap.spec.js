import { describe, it, expect } from "vitest";
import { candidateTextFor } from "./candidateSnap.js";

describe("candidateTextFor", () => {
	it("returns the candidate's full text when msgId matches", () => {
		const c = { msgId: "a1", runId: "run-1", full: "Short answer." };
		expect(candidateTextFor(c, "a1", "run-1")).toBe("Short answer.");
		expect(candidateTextFor(c, "a1", "some-other-run")).toBe("Short answer.");
	});

	it("returns null when msgId is set but does not match", () => {
		const c = { msgId: "a1", runId: "run-1", full: "Short answer." };
		expect(candidateTextFor(c, "a2", "run-1")).toBeNull();
	});

	it("falls back to runId only when msgId was never set", () => {
		const c = { msgId: null, runId: "run-1", full: "Short answer." };
		expect(candidateTextFor(c, "a1", "run-1")).toBe("Short answer.");
		expect(candidateTextFor(c, "a1", "run-2")).toBeNull();
	});

	it("returns null with no messageId, no candidate, or no full text", () => {
		const c = { msgId: "a1", runId: "run-1", full: "Short answer." };
		expect(candidateTextFor(c, null, "run-1")).toBeNull();
		expect(candidateTextFor(c, undefined, "run-1")).toBeNull();
		expect(candidateTextFor(null, "a1", "run-1")).toBeNull();
		expect(candidateTextFor(undefined, "a1", "run-1")).toBeNull();
		expect(
			candidateTextFor({ msgId: "a1", runId: "run-1", full: "" }, "a1", "run-1")
		).toBeNull();
	});

	it("a stale candidate from a run/message that has moved on returns null even with matching runId but no msgId set on an unrelated row", () => {
		// The exact regression this guards: liveCandidate belongs to the row
		// that streamed it, never to whichever row happens to share the
		// currently-live run id by coincidence once msgId is tracked.
		const c = { msgId: "a1", runId: "run-1", full: "Short answer." };
		expect(candidateTextFor(c, "a-unrelated", "run-1")).toBeNull();
	});
});
