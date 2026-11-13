import { describe, it, expect, vi, afterEach } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { mount } from "@vue/test-utils";
import Message from "@/components/chat/Message.vue";
import { renderMarkdown } from "@/markdown";

/**
 * #2170: a code block in an assistant reply has a copy button. The body is v-html,
 * so Message.vue handles the click and copies that block's text exactly as shown
 * (unescaped, without the button), not the whole reply.
 */
describe("Message code block copy", () => {
	afterEach(() => vi.restoreAllMocks());

	it("copies only the clicked block's text and flags it copied", async () => {
		const writeText = vi.fn().mockResolvedValue(undefined);
		vi.stubGlobal("navigator", { clipboard: { writeText } });
		vi.stubGlobal("isSecureContext", true);
		const html = renderMarkdown(
			"Intro\n\n```\nfirst <block> & co\n```\n\n```text\nsecond\n```",
			{
				copyCode: true,
			}
		);
		const w = mount(Message, { props: { variant: "row", html }, attachTo: document.body });
		const buttons = w.findAll("button.jv-code-copy");
		expect(buttons).toHaveLength(2);

		await buttons[0].trigger("click");
		await vi.waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
		expect(writeText).toHaveBeenCalledWith("first <block> & co");
		await vi.waitFor(() => expect(buttons[0].classes()).toContain("is-copied"));
		expect(buttons[0].attributes("title")).toBe("Copied");
		expect(w.emitted("copy")).toBeUndefined();
		// the parent announces the result in its live region
		expect(w.emitted("code-copy")).toEqual([[true]]);

		await buttons[1].trigger("click");
		await vi.waitFor(() => expect(writeText).toHaveBeenLastCalledWith("second"));
		w.unmount();
		vi.unstubAllGlobals();
	});

	it("reports a failed copy so it can be announced", async () => {
		const writeText = vi.fn().mockRejectedValue(new Error("denied"));
		vi.stubGlobal("navigator", { clipboard: { writeText } });
		vi.stubGlobal("isSecureContext", true);
		const exec = vi.fn().mockReturnValue(false);
		document.execCommand = exec;
		const html = renderMarkdown("```\ncode\n```", { copyCode: true });
		const w = mount(Message, { props: { variant: "row", html } });
		await w.get("button.jv-code-copy").trigger("click");
		await vi.waitFor(() => expect(w.emitted("code-copy")).toEqual([[false]]));
		expect(w.get("button.jv-code-copy").attributes("title")).toBe("Copy failed");
		delete document.execCommand;
		vi.unstubAllGlobals();
	});

	it("a click elsewhere in the body copies nothing", async () => {
		const writeText = vi.fn().mockResolvedValue(undefined);
		vi.stubGlobal("navigator", { clipboard: { writeText } });
		vi.stubGlobal("isSecureContext", true);
		const html = renderMarkdown("Intro\n\n```\ncode\n```", { copyCode: true });
		const w = mount(Message, { props: { variant: "row", html } });
		await w.get("pre").trigger("click");
		expect(writeText).not.toHaveBeenCalled();
		expect(w.emitted("code-copy")).toBeUndefined();
		vi.unstubAllGlobals();
	});
});

describe("the chat announces a code copy", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../../views/ChatView.vue"), "utf8");
	it("wires the reply row's code-copy to its screen-reader live region", () => {
		expect(src).toContain('@code-copy="announceCodeCopy"');
		expect(src).toMatch(/function announceCodeCopy\(ok\) \{\n\tannounceSR\(/);
	});
});
