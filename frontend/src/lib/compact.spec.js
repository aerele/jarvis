import { describe, it, expect } from "vitest";
import { parseCompactCommand, compactFailureCopy } from "./compact.js";

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
		expect(compactFailureCopy("whatever")).toBe("Could not compact this chat");
	});
});
