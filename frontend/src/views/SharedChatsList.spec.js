import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { describe, it, expect, vi, afterEach } from "vitest";
const { store } = await vi.hoisted(async () => {
	const { reactive } = await import("vue");
	return {
		store: reactive({
			sharedConversations: [
				{ name: "C1", title: "Saved review", owner_name: "Owner", unseen: false },
			],
			sharedConversationsLoading: false,
			sharedConversationsError: false,
			loadSharedConversations: vi.fn(),
		}),
	};
});
vi.mock("@/stores/shell", () => ({ useShellStore: () => store }));
import SharedChatsList from "./SharedChatsList.vue";
let wrapper;
afterEach(() => {
	wrapper?.unmount();
	store.sharedConversationsLoading = false;
	store.sharedConversationsError = false;
});
function open() {
	return (wrapper = mount(SharedChatsList, {
		attachTo: document.body,
		global: {
			stubs: { RouterLink: { props: ["to"], template: '<a :href="to"><slot/></a>' } },
		},
	}));
}
describe("shared index refresh recovery", () => {
	it("preserves the focused row during a background refresh", async () => {
		const w = open();
		const row = w.get("a").element;
		row.focus();
		store.sharedConversationsLoading = true;
		await nextTick();
		expect(w.find("a").exists()).toBe(true);
		expect(document.activeElement).toBe(row);
	});
	it("keeps cached rows visible after a refresh failure", async () => {
		const w = open();
		store.sharedConversationsError = true;
		await nextTick();
		expect(w.text()).toContain("Saved review");
		expect(w.text()).toContain("Couldn't load shared chats.");
	});
});
