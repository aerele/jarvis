import { mount } from "@vue/test-utils";
import { describe, it, expect, vi } from "vitest";
vi.mock("vue-router", () => ({
	useRoute: () => ({ meta: { chat: true } }),
	useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("frappe-ui", async () => ({
	Button: (await import("../../../node_modules/frappe-ui/src/components/Button/Button.vue"))
		.default,
	FeatherIcon: { template: "<i/>" },
	Dropdown: {
		props: ["options"],
		template:
			'<div><button v-for="o in options" :key="o.label" @click.stop="o.onClick">{{o.label}}</button><slot name="trigger"/></div>',
	},
	confirmDialog: vi.fn(),
}));
vi.mock("@/stores/shell", () => ({
	useShellStore: () => ({
		currentConvId: null,
		unreadConvs: new Set(),
		conversationIndicator: () => null,
	}),
}));
vi.mock("@/composables/useConversationExport", () => ({
	useConversationExport: () => ({
		downloadMenuOption: { value: { label: "Download", submenu: [] } },
	}),
}));
import ConversationRow from "./ConversationRow.vue";
import { chatShareTarget } from "@/composables/useChatSharing";
describe("owner Share entry", () => {
	it("gives the real pinned row-menu button an accessible name", () => {
		const w = mount(ConversationRow, {
			props: { conv: { name: "C1", title: "Invoice review" } },
		});
		expect(w.find("button.conversation-more").attributes("aria-label")).toBe(
			"Options for Invoice review"
		);
		w.unmount();
	});
	it("opens sharing without navigating and announces the current recipient count", async () => {
		chatShareTarget.value = null;
		const w = mount(ConversationRow, {
			props: { conv: { name: "C1", title: "Invoice review", shared_count: 3 } },
		});
		expect(w.find('[aria-label="Shared with 3 people"]').exists()).toBe(true);
		await w
			.findAll("button")
			.find((b) => b.text() === "Share…")
			.trigger("click");
		expect(chatShareTarget.value).toEqual({ name: "C1", title: "Invoice review" });
		w.unmount();
	});
	it("never offers sharing for File Box chats", () => {
		const w = mount(ConversationRow, { props: { conv: { name: "C1", file_box: 1 } } });
		expect(w.findAll("button").some((b) => b.text() === "Share…")).toBe(false);
		w.unmount();
	});
});
