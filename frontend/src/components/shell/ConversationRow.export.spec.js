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
		emits: ["update:open"],
		data: () => ({ open: true }),
		methods: {
			select(option, event) {
				option.onClick?.(event);
				if (!event.defaultPrevented) {
					this.open = false;
					this.$emit("update:open", false);
				}
			},
		},
		// Native submenu focus is verified in Chrome. Here the formats must be
		// revealed in this same popup, where its focus trap contains them.
		template: `<div><div @click.capture="open = true; $emit('update:open', true)"><slot name="trigger"/></div><div v-if="open"><template v-for="option in options" :key="option.value || option.label"><component v-if="option.component" :is="option.component" :disabled="option.disabled" @click.stop="select(option, $event)"/><button v-else :disabled="option.disabled" @click.stop="select(option, $event)">{{ option.label }}</button></template></div></div>`,
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
import { useConversationExport } from "@/composables/useConversationExport";

describe("ConversationRow export menu", () => {
	beforeEach(() => vi.clearAllMocks());
	it.each([
		["PDF", "pdf"],
		["Markdown", "md"],
	])(
		"downloads %s through Download while retaining the existing actions",
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
			expect(wrapper.findAll("button").some((item) => item.text() === label)).toBe(false);
			const download = wrapper.findAll("button").find((item) => item.text() === "Download");
			expect(download).toBeTruthy();
			await download.trigger("click");
			expect(downloadConversation).not.toHaveBeenCalled();
			const button = wrapper.findAll("button").find((item) => item.text() === label);
			expect(button).toBeTruthy();
			await button.trigger("click");
			expect(downloadConversation).toHaveBeenCalledWith("row-" + format, format);
			await wrapper.find("button").trigger("click");
			expect(wrapper.text()).toContain("Preparing " + label + "…");
			finish();
			await flushPromises();
			expect(wrapper.text()).toContain("Download");
			wrapper.unmount();
		}
	);
	it("keeps the disclosure button mounted while expansion and shared progress change", async () => {
		let finish;
		downloadConversation.mockImplementation(
			() => new Promise((resolve) => (finish = resolve))
		);
		const wrapper = mount(ConversationRow, {
			props: { conv: { name: "stable-disclosure", title: "Chat" } },
		});
		const disclosure = wrapper.findAll("button").find((item) => item.text() === "Download");
		const element = disclosure.element;
		expect(disclosure.attributes("aria-expanded")).toBe("false");
		await disclosure.trigger("click");
		expect(disclosure.attributes("aria-expanded")).toBe("true");
		const request = useConversationExport(
			() => "stable-disclosure"
		).downloadMenuOption.value.submenu[0].onClick();
		try {
			await flushPromises();
			expect(wrapper.find("[aria-expanded]").element).toBe(element);
			expect(disclosure.text()).toBe("Preparing PDF…");
		} finally {
			finish();
			await request;
			await flushPromises();
		}
		expect(wrapper.find("[aria-expanded]").element).toBe(element);
		expect(disclosure.text()).toBe("Download");
		wrapper.unmount();
	});

	it("collapses format choices on another Download click and resets after closing the menu", async () => {
		const wrapper = mount(ConversationRow, {
			props: { conv: { name: "reset-disclosure", title: "Chat" } },
		});
		const action = (label) => wrapper.findAll("button").find((item) => item.text() === label);
		await action("Download").trigger("click");
		expect(action("PDF")).toBeTruthy();
		await action("Download").trigger("click");
		expect(action("PDF")).toBeUndefined();
		await action("Download").trigger("click");
		await action("Star").trigger("click");
		await wrapper.find("button").trigger("click");
		expect(action("Download")).toBeTruthy();
		expect(action("PDF")).toBeUndefined();
		expect(downloadConversation).not.toHaveBeenCalled();
		wrapper.unmount();
	});

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
