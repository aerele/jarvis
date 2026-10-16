import fs from "node:fs";
import { compile } from "@vue/compiler-dom";
import { flushPromises, mount } from "@vue/test-utils";
import * as Vue from "vue";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/api", () => ({ downloadConversation: vi.fn() }));
vi.mock("frappe-ui", () => ({ toast: { error: vi.fn() } }));
vi.mock("@/lib/errorReporter", () => ({ report: vi.fn() }));
import { downloadConversation } from "@/api";
import { useConversationExport } from "@/composables/useConversationExport";

// Compile the real new menu entries without mounting the unrelated chat engine.
// In a browser, disabling the focused button drops focus and loses Escape.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
const button = source.match(/<button\s+v-for="option in downloadOptions"[\s\S]*?<\/button>/)[0];
const escape = source.match(/@keydown\.esc\.stop="closeCreateMenu"/)[0];
const { code } = compile(`<div ${escape}>${button}</div>`, {
	mode: "function",
	prefixIdentifiers: true,
});
const render = new Function("Vue", code)(Vue);

describe("ChatView export menu", () => {
	it("keeps pending entries focusable for Escape and ignores their repeat clicks", async () => {
		let finish;
		downloadConversation.mockImplementation(
			() => new Promise((resolve) => (finish = resolve))
		);
		const closeCreateMenu = vi.fn();
		const wrapper = mount(
			{
				setup: () => ({ ...useConversationExport(() => "header-focus"), closeCreateMenu }),
				render,
			},
			{ attachTo: document.body }
		);
		try {
			const pdf = wrapper.findAll("button")[0];
			pdf.element.focus();
			await pdf.trigger("click");
			expect(pdf.text()).toBe("Preparing PDF…");
			expect(pdf.attributes("aria-disabled")).toBe("true");
			expect(pdf.attributes("disabled")).toBeUndefined();
			expect(document.activeElement).toBe(pdf.element);
			await pdf.trigger("click");
			expect(downloadConversation).toHaveBeenCalledTimes(1);
			await pdf.trigger("keydown", { key: "Escape" });
			expect(closeCreateMenu).toHaveBeenCalledTimes(1);
		} finally {
			finish?.();
			await flushPromises();
			wrapper.unmount();
		}
	});
});
