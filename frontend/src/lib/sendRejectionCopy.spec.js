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
});
