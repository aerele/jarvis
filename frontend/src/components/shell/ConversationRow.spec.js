import { mount } from "@vue/test-utils";
import { reactive } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("vue-router", () => ({
	useRoute: () => ({ meta: { chat: true } }),
	useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("frappe-ui", () => ({
	Button: { template: "<button/>" },
	FeatherIcon: { template: "<i/>" },
	Dropdown: { template: "<div><slot name='trigger'/></div>" },
	confirmDialog: vi.fn(),
}));
vi.mock("@/lib/errors", () => ({ escapeHtml: (s) => s }));
vi.mock("@/stores/shell", async () => {
	const { reactive } = await import("vue");
	const store = reactive({
		currentConvId: null,
		streamingConvId: null,
		unreadConvs: new Set(),
		runningConvs: new Set(),
		conversationIndicator: vi.fn(),
		conversationActivity: new Map(),
	});
	return { useShellStore: () => store };
});
import { useShellStore } from "@/stores/shell";
import ConversationRow from "./ConversationRow.vue";
const store = useShellStore();
const conv = reactive({
	name: "conv-b",
	title: "A deliberately long conversation title for the sidebar",
});

describe("ConversationRow server activity", () => {
	beforeEach(() => {
		store.currentConvId = null;
		store.streamingConvId = null;
		store.unreadConvs = new Set();
		store.runningConvs = new Set();
		store.conversationIndicator.mockReset().mockReturnValue(null);
	});
	it.each([
		["Replying", true, "streaming"],
		["Preparing", true, "preparing"],
		["Queued", false, "queued"],
		["Recovering", false, "recovering"],
	])("renders a named %s image with correct pulse", (label, pulse, state) => {
		store.conversationIndicator.mockReturnValue({ label, pulse, state });
		const w = mount(ConversationRow, { props: { conv } });
		const dot = w.get('[role="img"]');
		expect(dot.attributes("aria-label")).toBe(label);
		expect(dot.attributes("title")).toBe(label + "…");
		expect(dot.classes().includes("animate-pulse")).toBe(pulse);
		expect(store.conversationIndicator).toHaveBeenCalledWith("conv-b");
		w.unmount();
	});
	it("shows unread when authoritative activity is idle", () => {
		store.unreadConvs.add("conv-b");
		const w = mount(ConversationRow, { props: { conv } });
		expect(w.find('[aria-label="Replying"]').exists()).toBe(false);
		expect(w.get('[title="New reply"]').exists()).toBe(true);
		w.unmount();
	});
	it("does not override an active-chat idle decision with an old running marker", () => {
		store.currentConvId = "conv-b";
		store.runningConvs.add("conv-b");
		const w = mount(ConversationRow, { props: { conv } });
		expect(w.find('[aria-label="Replying"]').exists()).toBe(false);
		w.unmount();
	});
});
