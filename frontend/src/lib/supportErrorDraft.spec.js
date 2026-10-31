import { describe, expect, it } from "vitest";
import rules from "../../../jarvis/public/js/turn_error_rules.mjs";
import { supportErrorDraft } from "./supportErrorDraft";
const context = {
	code: "timeout",
	conversation: "conv788fixture",
	message: "reply788fixture",
	at: "2026-10-29 12:01:07.123456",
};
describe("safe support error draft", () => {
	it("includes only selected diagnostic references and the shared taxonomy summary", () => {
		const d = supportErrorDraft(context);
		expect(d.subject).toBe("Chat error: " + rules.find((r) => r.code === "timeout").headline);
		expect(d.body).toContain("Error type: timeout");
		expect(d.body).toContain("Conversation: conv788fixture");
		expect(d.body).toContain("Message: reply788fixture");
		expect(d.body).toContain("Saved (server time): 2026-10-29 12:01:07.123456");
	});
	it("never reads raw errors, transcripts, tools, files, URLs or caller headlines", () => {
		const input = { ...context };
		for (const key of [
			"error",
			"content",
			"messages",
			"tool_args",
			"file_url",
			"headline",
			"url",
		])
			Object.defineProperty(input, key, {
				get: () => {
					throw new Error("PRIVATE-SECRET");
				},
			});
		expect(supportErrorDraft(input)).toEqual(supportErrorDraft(context));
	});
	it.each([
		"__proto__",
		"constructor",
		"toString",
		"api_key=SECRET\nInjected",
		"x".repeat(10000),
		null,
		{},
	])("rejects unrecognised code %s", (code) => {
		const d = supportErrorDraft({ ...context, code });
		expect(d.body).toContain("Error type: internal");
		expect(d.subject).toBe("Chat error: " + rules.find((r) => r.code === "internal").headline);
		expect(d.body).not.toContain("SECRET");
	});
	it("uses known subscription upstream labels", () =>
		expect(
			supportErrorDraft({ ...context, code: "subscription-expired", upstream: "anthropic" })
				.subject
		).toBe("Chat error: Anthropic sign-in expired"));
	it.each([undefined, "constructor", "__proto__", "Bearer SECRET", {}])(
		"uses authentication for unknown upstream %s",
		(upstream) => {
			const d = supportErrorDraft({ ...context, code: "subscription-expired", upstream });
			expect(d.body).toContain("Error type: authentication");
			expect(d.body).not.toMatch(/SECRET|undefined|\{provider\}/);
		}
	);
	it("does not draft a cancelled turn", () =>
		expect(supportErrorDraft({ ...context, code: "cancelled" })).toBeNull());
	it.each(["<script>SECRET</script>", "Bearer SECRET", "a".repeat(141), "id\nSECRET", 123, {}])(
		"drops invalid references %s",
		(id) => {
			const d = supportErrorDraft({ ...context, conversation: id, message: id, at: id });
			expect(d.body).toContain("Conversation: Unavailable");
			expect(d.body).toContain("Message: Unavailable");
			expect(d.body).toContain("Saved (server time): Unavailable");
			expect(d.body).not.toContain("SECRET");
		}
	);
	it("handles missing metadata and bounds every taxonomy draft", () => {
		expect(supportErrorDraft().body).toContain("Unavailable");
		for (const rule of rules.filter((r) => r.code !== "cancelled")) {
			const d = supportErrorDraft({ ...context, code: rule.code, upstream: "openai" });
			expect([...d.subject].length).toBeLessThanOrEqual(140);
			expect(d.body.length).toBeLessThan(1000);
			expect(d.body).not.toMatch(/undefined|\{provider\}/);
		}
	});
});
