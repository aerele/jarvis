import { describe, it, expect } from "vitest";
import { parseCompactCommand, compactFailureCopy, compactToastType } from "./compact.js";

describe("parseCompactCommand", () => {
	it("returns null for normal text", () => {
		expect(parseCompactCommand("compact the invoice")).toBeNull();
		expect(parseCompactCommand("/compactor")).toBeNull();
	});
	it("parses the bare command and a hint", () => {
		expect(parseCompactCommand("/compact")).toEqual({ hint: "" });
		expect(parseCompactCommand("  /compact keep the invoice inputs ")).toEqual({
			hint: "keep the invoice inputs",
		});
		expect(parseCompactCommand("/COMPACT: keep totals")).toEqual({ hint: "keep totals" });
	});
});

describe("compactFailureCopy", () => {
	it("maps reasons to copy", () => {
		expect(compactFailureCopy("runtime_declined")).toBe("Nothing to compact yet");
		expect(compactFailureCopy("runtime_failed")).toBe(
			"Could not compact this chat right now, try again later"
		);
		expect(compactFailureCopy("nothing_to_compact")).toBe("Nothing new to compact yet");
		expect(compactFailureCopy("conversation_busy")).toBe(
			"A reply is in progress, try again in a moment"
		);
		expect(compactFailureCopy("bad_hint")).toBe("The hint cannot start with a slash");
		expect(compactFailureCopy("directive_refused")).toBe(
			"Model and runtime changes are not available in chat."
		);
		expect(compactFailureCopy("whatever")).toBe("Could not compact this chat");
	});
});

describe("compactToastType", () => {
	it("shows no-op outcomes as info and everything else as error", () => {
		expect(compactToastType("runtime_declined")).toBe("info");
		expect(compactToastType("nothing_to_compact")).toBe("info");
		expect(compactToastType("runtime_failed")).toBe("error");
		expect(compactToastType(undefined)).toBe("error");
	});
});
