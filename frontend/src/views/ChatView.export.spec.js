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

// Mount the real header entry without starting the unrelated chat engine.
// Browser checks exercise the actual library popup, focus and keyboard behavior.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
const entry = source.match(
	/<Dropdown\s+:options="downloadMenuOption\.submenu"[\s\S]*?<\/Dropdown>/
)[0];
const escape = source.match(/@keydown\.esc\.stop="closeCreateMenu"/)[0];
const { code } = compile(`<div ${escape}>${entry}</div>`, {
	mode: "function",
	prefixIdentifiers: true,
});
const render = new Function("Vue", code)(Vue);
const Dropdown = {
	props: ["options"],
	data: () => ({ open: false }),
	template: `<div><div @click="open = !open"><slot name="trigger"/></div><div v-if="open"><button v-for="option in options" :key="option.key" :disabled="option.disabled" @click="option.onClick()">{{ option.label }}</button></div></div>`,
};

describe("ChatView grouped download menu", () => {
	it("disables its custom trigger when no conversation is selected", () => {
		const wrapper = mount(
			{
				setup: () => ({ ...useConversationExport(() => null), closeCreateMenu: vi.fn() }),
				render,
			},
			{ global: { components: { Dropdown } } }
		);
		expect(wrapper.find("button").attributes("disabled")).toBeDefined();
		wrapper.unmount();
	});
	it.each([
		["PDF", "pdf"],
		["Markdown", "md"],
	])(
		"reveals %s after Download and keeps the pending parent focusable for Escape",
		async (label, format) => {
			vi.clearAllMocks();
			let finish;
			downloadConversation.mockImplementation(
				() => new Promise((resolve) => (finish = resolve))
			);
			const closeCreateMenu = vi.fn();
			const wrapper = mount(
				{
					setup: () => ({
						...useConversationExport(() => "header-" + format),
						closeCreateMenu,
					}),
					render,
				},
				{ attachTo: document.body, global: { components: { Dropdown } } }
			);
			try {
				const trigger = wrapper.find("button");
				expect(trigger.find(".jv-create-item-t").text()).toBe("Download");
				expect(wrapper.findAll("button")).toHaveLength(1);
				await trigger.trigger("click");
				expect(downloadConversation).not.toHaveBeenCalled();
				const choice = wrapper.findAll("button").find((button) => button.text() === label);
				expect(choice).toBeTruthy();
				await choice.trigger("click");
				expect(downloadConversation).toHaveBeenCalledWith("header-" + format, format);
				expect(trigger.find(".jv-create-item-t").text()).toBe("Preparing " + label + "…");
				expect(trigger.attributes("disabled")).toBeUndefined();
				trigger.element.focus();
				expect(document.activeElement).toBe(trigger.element);
				await choice.trigger("click");
				expect(downloadConversation).toHaveBeenCalledTimes(1);
				await trigger.trigger("keydown", { key: "Escape" });
				expect(closeCreateMenu).toHaveBeenCalledTimes(1);
			} finally {
				finish?.();
				await flushPromises();
				wrapper.unmount();
			}
		}
	);
});
