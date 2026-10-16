import { flushPromises, mount } from "@vue/test-utils";
import { compileStyle, parse } from "@vue/compiler-sfc";
import fs from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("vue-router", () => ({
	useRoute: () => ({ meta: { chat: true } }),
	useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("frappe-ui", () => ({
	Button: { template: "<button/>" },
	FeatherIcon: { template: "<i/>" },
	Dropdown: {
		props: ["options"],
		template: `<div><slot name="trigger"/><button v-for="option in options" :key="option.label" :disabled="option.disabled" @click="option.onClick">{{ option.label }}</button></div>`,
	},
	confirmDialog: vi.fn(),
	toast: { error: vi.fn() },
}));
vi.mock("@/stores/shell", () => ({
	useShellStore: () => ({
		currentConvId: null,
		unreadConvs: new Set(),
		conversationIndicator: () => null,
		toggleStar: vi.fn(),
		renameConversation: vi.fn(),
		archiveConversation: vi.fn(),
	}),
}));
vi.mock("@/api", () => ({ downloadConversation: vi.fn() }));
vi.mock("@/lib/errorReporter", () => ({ report: vi.fn() }));
import { downloadConversation } from "@/api";
import ConversationRow from "./ConversationRow.vue";

describe("ConversationRow export menu", () => {
	beforeEach(() => vi.clearAllMocks());
	it.each([
		["PDF", "pdf"],
		["Markdown", "md"],
	])(
		"downloads %s from the flat menu while retaining the existing actions",
		async (label, format) => {
			let finish;
			downloadConversation.mockImplementation(
				() => new Promise((resolve) => (finish = resolve))
			);
			const wrapper = mount(ConversationRow, {
				props: { conv: { name: "row-" + format, title: "Chat" } },
			});
			expect(wrapper.text()).toContain("Star");
			expect(wrapper.text()).toContain("Rename");
			expect(wrapper.text()).toContain("Delete");
			const button = wrapper
				.findAll("button")
				.find((item) => item.text() === "Download as " + label);
			expect(button).toBeTruthy();
			await button.trigger("click");
			expect(downloadConversation).toHaveBeenCalledWith("row-" + format, format);
			expect(wrapper.text()).toContain("Preparing " + label + "…");
			finish();
			await flushPromises();
			expect(wrapper.text()).toContain("Download as " + label);
			wrapper.unmount();
		}
	);
	it("matches the touch visibility rule across the Button component's renderless wrapper", () => {
		const source = fs.readFileSync("src/components/shell/ConversationRow.vue", "utf8");
		const { descriptor } = parse(source);
		const { code } = compileStyle({
			source: descriptor.styles[0].content,
			id: "data-v-touch",
			scoped: true,
		});
		expect(code).toContain("@media (hover: none)");
		const selector = code.match(/([^{}\n]+)\s*\{\s*opacity:\s*1/)[1].trim();
		// TooltipProvider is renderless; the real Button forwards attrs to a nested
		// button without inheriting the row's scoped attribute on that button.
		const row = document.createElement("div");
		row.setAttribute("data-v-touch", "");
		const button = document.createElement("button");
		button.className = "conversation-more";
		row.appendChild(button);
		expect(button.matches(selector)).toBe(true);
	});
});
