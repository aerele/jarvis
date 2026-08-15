import { describe, it, expect } from "vitest";
import { formatRecentMessagesForSupport } from "./supportCopyFormat.js";

describe("formatRecentMessagesForSupport", () => {
	it("labels user and assistant turns, in transcript order", () => {
		const out = formatRecentMessagesForSupport(
			[
				{ role: "user", content: "hi" },
				{ role: "assistant", content: "hello" },
			],
			"Jarvis"
		);
		expect(out).toEqual(["You: hi", "Jarvis: hello"]);
	});

	it("drops everything except user/assistant turns with real content", () => {
		const out = formatRecentMessagesForSupport(
			[
				{ role: "tool", content: "get_customer(...)" },
				{ role: "user", content: "   " }, // whitespace-only
				{ role: "assistant", content: "" },
				{ role: "user", content: "real question" },
				null, // defensive: a malformed entry must not throw
			],
			"Jarvis"
		);
		expect(out).toEqual(["You: real question"]);
	});

	it("keeps only the last N visible turns", () => {
		const messages = Array.from({ length: 6 }, (_, i) => ({
			role: i % 2 === 0 ? "user" : "assistant",
			content: `turn ${i}`,
		}));
		const out = formatRecentMessagesForSupport(messages, "Jarvis", 4);
		expect(out).toEqual(["You: turn 2", "Jarvis: turn 3", "You: turn 4", "Jarvis: turn 5"]);
	});

	it("leaves short text untouched, with no ellipsis", () => {
		const out = formatRecentMessagesForSupport([{ role: "user", content: "short" }], "Jarvis");
		expect(out).toEqual(["You: short"]);
	});

	it("clips long text to the code-point limit and appends an ellipsis", () => {
		const long = "a".repeat(500);
		const out = formatRecentMessagesForSupport(
			[{ role: "user", content: long }],
			"Jarvis",
			4,
			400
		);
		expect(out[0]).toBe(`You: ${"a".repeat(400)}…`);
	});

	it("clips by CODE POINT, not UTF-16 code unit, so an emoji straddling the limit stays whole", () => {
		// 🎉 is an astral character: two UTF-16 code units, one code point.
		// text.slice(0, N) on UTF-16 units can land mid-surrogate-pair and leave
		// an unpaired surrogate in the clipped output (review finding).
		const text = "a".repeat(399) + "🎉" + "b".repeat(50);
		const out = formatRecentMessagesForSupport(
			[{ role: "user", content: text }],
			"Jarvis",
			4,
			400
		);
		const body = out[0].slice("You: ".length);
		// The limit (400 code points) falls exactly on the emoji: it is either
		// whole in the clipped output or excluded entirely - never split.
		expect(body.endsWith("…")).toBe(true);
		expect(body).not.toMatch(/[\uD800-\uDBFF]$/); // no dangling high surrogate
		expect(body).not.toMatch(/^[\uDC00-\uDFFF]/); // no dangling low surrogate
	});

	it("trims surrounding whitespace before measuring/clipping", () => {
		const out = formatRecentMessagesForSupport(
			[{ role: "user", content: "  padded text  \n" }],
			"Jarvis"
		);
		expect(out).toEqual(["You: padded text"]);
	});

	it("returns an empty array for no messages or an empty/undefined list", () => {
		expect(formatRecentMessagesForSupport([], "Jarvis")).toEqual([]);
		expect(formatRecentMessagesForSupport(undefined, "Jarvis")).toEqual([]);
	});
});
