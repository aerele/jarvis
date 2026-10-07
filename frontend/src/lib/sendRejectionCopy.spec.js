import { describe, it, expect } from "vitest";
import { sendRejectionCopy } from "./sendRejectionCopy";

const FALLBACK = "Couldn't send your message.";

describe("sendRejectionCopy", () => {
	it("maps usage_limit to the usage-limit copy", () => {
		const r = sendRejectionCopy("usage_limit", "Jarvis");
		expect(r.message).toContain("usage limit");
		expect(r.message).toContain("Jarvis admin");
		expect(r.type).toBe("error");
	});

	it("maps llm_not_configured to a warning that points at Settings", () => {
		const r = sendRejectionCopy("llm_not_configured", "Jarvis");
		expect(r.message).toContain("Settings");
		expect(r.type).toBe("warning");
	});

	it("maps workspace_resetting to a warning naming the agent", () => {
		const r = sendRejectionCopy("workspace_resetting", "Jarvis");
		expect(r.message).toContain("Jarvis is being reset");
		expect(r.type).toBe("warning");
	});

	it("maps release_update_required", () => {
		expect(sendRejectionCopy("release_update_required", "Jarvis").message).toContain(
			"being updated"
		);
	});

	it("maps subscription_suspended", () => {
		expect(sendRejectionCopy("subscription_suspended", "Jarvis").message).toContain(
			"subscription"
		);
	});

	it("shows a server sentence verbatim", () => {
		const sentence = "A reply is already in progress.";
		expect(sendRejectionCopy(sentence, "Jarvis").message).toBe(sentence);
	});

	it("never surfaces an unknown machine code", () => {
		expect(sendRejectionCopy("insufficient_workers", "Jarvis").message).toBe(FALLBACK);
	});

	it("falls back when the reason is empty", () => {
		expect(sendRejectionCopy("", "Jarvis").message).toBe(FALLBACK);
		expect(sendRejectionCopy(undefined, "Jarvis").message).toBe(FALLBACK);
	});

	it("uses the caller's fallback for an unknown code", () => {
		const retry = "Couldn't retry that.";
		expect(sendRejectionCopy("maintenance", "Jarvis", {}, retry).message).toBe(retry);
	});

	// Plain text: a caller that renders HTML escapes the whole message, so the
	// helper must not (a text caller would show the entities).
	it("keeps a server sentence as plain text", () => {
		const sentence = "Don't send <b>this</b> & that.";
		expect(sendRejectionCopy(sentence, "Jarvis").message).toBe(sentence);
	});
});

describe("sendRejectionCopy, usage limit window", () => {
	it("names the window and its reset when the envelope carries one", () => {
		expect(sendRejectionCopy("usage_limit", "Jarvis", { limit_period: "Daily" }).message).toBe(
			"You've reached today's usage limit. It resets at midnight."
		);
		expect(
			sendRejectionCopy("usage_limit", "Jarvis", { limit_period: "Weekly" }).message
		).toBe("You've reached this week's usage limit. It resets on Sunday.");
		expect(
			sendRejectionCopy("usage_limit", "Jarvis", { limit_period: "Monthly" }).message
		).toBe("You've reached this month's usage limit. It resets on the 1st.");
	});

	it("keeps the neutral copy without a window (all-time or per-model cap)", () => {
		for (const envelope of [undefined, {}, { limit_period: "All time" }]) {
			expect(sendRejectionCopy("usage_limit", "Jarvis", envelope).message).toBe(
				"You've reached your usage limit. Ask your Jarvis admin to raise it."
			);
		}
	});
});
