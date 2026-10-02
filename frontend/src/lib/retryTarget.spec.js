import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { isMacroClosingMessage, retryTargetIndex } from "./retryTarget";

const user = { name: "u1", role: "user", content: "step 2" };
const failed = { name: "a1", role: "assistant", content: "", error: "The model is overloaded." };
const closing = {
	name: "a2",
	role: "assistant",
	content: "✗ Macro failed. Step 2 failed: The model is overloaded.",
	ref_doctype: "Jarvis Macro Run",
	ref_name: "run-1",
};

describe("which message carries Retry", () => {
	it("is the last message of an ordinary thread", () => {
		expect(retryTargetIndex([user, failed])).toBe(1);
	});

	it("is the failed reply, not the macro's closing message after it", () => {
		// The closing message explains the failure; it must not take away the one
		// control that acts on it.
		expect(retryTargetIndex([user, failed, closing])).toBe(1);
	});

	it("moves on once a real message follows the failed reply", () => {
		const next = { name: "u2", role: "user", content: "try again" };
		expect(retryTargetIndex([user, failed, closing, next])).toBe(3);
		expect(retryTargetIndex([user, failed, next])).toBe(2);
	});

	it("is nothing for an empty thread, or one that holds only a closing message", () => {
		expect(retryTargetIndex([])).toBe(-1);
		expect(retryTargetIndex(undefined)).toBe(-1);
		expect(retryTargetIndex([closing])).toBe(-1);
	});
});

describe("what counts as a macro's closing message", () => {
	it("is an assistant row that refers to a macro run", () => {
		expect(isMacroClosingMessage(closing)).toBe(true);
	});

	it("is not a reply, a reply about another record, or a tool row about a run", () => {
		expect(isMacroClosingMessage(failed)).toBe(false);
		expect(isMacroClosingMessage({ ...failed, ref_doctype: "Sales Invoice" })).toBe(false);
		expect(isMacroClosingMessage({ role: "tool", ref_doctype: "Jarvis Macro Run" })).toBe(
			false
		);
		expect(isMacroClosingMessage(null)).toBe(false);
	});
});

describe("ChatView uses the rule", () => {
	// ChatView is too large to mount; the rule is unit-tested above, and this pins
	// the one place that must call it.
	const src = readFileSync(resolve(__dirname, "../views/ChatView.vue"), "utf8");

	it("gates the Retry button on the retry target, not on the last visible row", () => {
		const button = src.slice(src.lastIndexOf("<button", src.indexOf('class="jv-retry"')));
		const gate = button.slice(0, button.indexOf('class="jv-retry"'));
		expect(gate).toContain("mi === retryIdx");
		expect(gate).not.toContain("visibleMessages.length - 1");
		expect(src).toMatch(
			/const retryIdx = computed\(\(\) => retryTargetIndex\(visibleMessages\.value\)\)/
		);
	});
});
