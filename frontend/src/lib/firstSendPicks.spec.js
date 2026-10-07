import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import { firstSendPicks } from "./firstSendPicks.js";

describe("firstSendPicks", () => {
	it("carries both picks when the chat has no conversation yet", () => {
		expect(firstSendPicks("", "claude-sonnet-5", "low")).toEqual({
			model: "claude-sonnet-5",
			thinking: "low",
		});
	});

	it("carries only the pick that was made", () => {
		expect(firstSendPicks("", "claude-sonnet-5", "")).toEqual({
			model: "claude-sonnet-5",
			thinking: undefined,
		});
		expect(firstSendPicks("", "", "high")).toEqual({ model: undefined, thinking: "high" });
	});

	it("carries nothing when nothing was picked (Auto, default thinking)", () => {
		expect(firstSendPicks("", "", "")).toEqual({ model: undefined, thinking: undefined });
		expect(firstSendPicks(undefined, undefined, undefined)).toEqual({
			model: undefined,
			thinking: undefined,
		});
	});

	it("never re-sends a pick for an existing conversation", () => {
		// The pick was validated and saved when it was made. Re-sending it would make
		// send_message re-validate a pin loaded from the server on every message.
		expect(firstSendPicks("CONV-1", "claude-sonnet-5", "low")).toEqual({
			model: undefined,
			thinking: undefined,
		});
	});
});

// The actual SFC send boundary and wire arguments are exercised in
// utils/desktopSendRecovery.test.js, including first-send model/effort picks.
const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

describe("newChat() starts a chat with no pick", () => {
	// The server hands a new chat out with no pick (a reused empty one is cleared),
	// so the pill must not keep showing the previous chat's.
	const at = src.indexOf("async function newChat() {");
	const body = src.slice(at, src.indexOf("\nasync function ", at + 1));
	const adopted = body.indexOf("currentId.value = conv?.name || conv;");

	it("resets the model and effort picks once the new chat is adopted", () => {
		expect(adopted).toBeGreaterThan(-1);
		expect(body.indexOf('modelOverride.value = "";')).toBeGreaterThan(adopted);
		expect(body.indexOf('thinkingOverride.value = "";')).toBeGreaterThan(adopted);
	});
});
