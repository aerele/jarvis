import fs from "node:fs";
import { describe, expect, it } from "vitest";

// Source-level guards: the chat surfaces never route a raw reason code, exception
// text or browser network text to the user; each failure names its own action.
const chat = fs.readFileSync("src/views/ChatView.vue", "utf8");
const dash = fs.readFileSync("src/pages/dashboards/DashboardChatPane.vue", "utf8");

describe("chat error copy wiring", () => {
	it("send and retry refusals go through rejectionCopy, never r.reason", () => {
		expect(chat).not.toMatch(/r\.message \|\| r\.reason/);
		expect(chat).not.toContain('notify(r.reason || "Couldn\'t retry that."');
		expect(chat).toContain('rejectionCopy(r, brand.agentName, "Couldn\'t retry that.")');
		expect(dash).toContain("rejectionCopy(r, brand.agentName, fallback)");
	});
	it("action failures use a per-action sentence", () => {
		for (const what of [
			"Couldn't run that again",
			"Couldn't stop that run",
			"Couldn't compact this chat",
			"Couldn't open that step",
		]) {
			expect(chat).toContain(`actionErrorCopy("${what}", e)`);
		}
		expect(chat).toContain("notify(actionErrorCopy(prefix, e)");
	});
	it("model and effort toasts carry the refusal cause", () => {
		expect(dash).toContain("Couldn't change the model for this chat");
		expect(dash).toContain("Couldn't change the effort for this chat");
		expect(dash).not.toContain("throw new Error(result.reason ||");
	});
	it("attach and mic failures use the plain-copy mappers", () => {
		expect(chat).toContain("uploadErrorCopy(err)");
		expect(chat).not.toContain('micRec.error || "Recording failed. Try again."');
		expect(chat).toContain("micErrorCopy(micRec.error)");
	});
	it("failed action cards offer Report error through the shared ticket draft", () => {
		expect(chat).toContain('@report="openActionErrorSupport"');
		expect(chat).toContain('code: "internal"');
	});
});
